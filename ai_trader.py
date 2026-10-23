"""Local fine-tuning and strictly virtual, gated SmartAPI paper trading."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from queue import Empty, Full, Queue
import sqlite3
import subprocess
import sys
from threading import Event, Thread
import time

from ai_paper.evaluation import evaluate, finish_shadow, model_artifact, readiness
from ai_paper.execution import PaperExecution
from ai_paper.learning import ContextPending, context_contracts, input_features, prepare, train, write_json, Ollama
from ai_paper.market import (
    AIError,
    BAR,
    DIRECTIONS,
    BarBuilder,
    MarketData,
    Store,
    canonical,
    digest,
    instant,
    load_config,
    resolve_contracts,
    session_window,
)
from paper_trader import readable
from smartapi_data import (
    HISTORY,
    IST,
    MASTER,
    SmartAPIError,
    _ArgumentParser,
    _http,
    _local_login,
    _local_settings,
)


def now():
    return datetime.now(timezone.utc)


def emit(value):
    print(canonical(readable(value)), flush=True)


def paced_history(client, contract, start, end):
    delay = client._next_allowed.get(HISTORY, 0) - time.monotonic()
    if delay > 0:
        time.sleep(delay)
    return client.history(contract, start, end)


def stream_recordings(client, contracts, seconds, windows):
    """One bounded queue; SQLite remains on the main thread during inference."""
    events, stopped = Queue(maxsize=4096), Event()
    events.live = Event()
    wire_keys = {(c.exchange, c.token): c.key for c in contracts}
    events.blocked = frozenset(wire_keys.values())

    def worker():
        try:
            with closing(
                client.stream(contracts, max_quotes=1_000_000, duration=seconds, windows=windows)
            ) as source:
                for event in source:
                    if stopped.is_set():
                        break
                    if event["event"] == "quote":
                        events.live.set()
                        events.blocked = events.blocked - {wire_keys[event["exchange"], event["token"]]}
                    elif "exchange" in event and "token" in event:
                        events.blocked = events.blocked | {wire_keys[event["exchange"], event["token"]]}
                    else:
                        events.live.clear()
                        events.blocked = frozenset(wire_keys.values())
                    try:
                        events.put(event, timeout=1)
                    except Full:
                        events.live.clear()
                        while True:
                            try:
                                events.get_nowait()
                            except Empty:
                                break
                        events.put_nowait(
                            {"event": "status", "status": "UNAVAILABLE", "reason": "RECORDING_OVERFLOW"}
                        )
                        break
        except (SmartAPIError, ValueError):
            events.live.clear()
            try:
                events.put(
                    {"event": "status", "status": "UNAVAILABLE", "reason": "STREAM_FAILURE"}, timeout=1
                )
            except Full:
                pass
        finally:
            events.live.clear()
            while not stopped.is_set():
                try:
                    events.put(None, timeout=0.1)
                    break
                except Full:
                    continue

    thread = Thread(target=worker, name="market-data", daemon=True)
    thread.start()
    try:
        yield events
    finally:
        stopped.set()
        client.close()
        thread.join(timeout=12)


def quote_event(event, lookup):
    c = lookup[event["exchange"], event["token"]]
    return {
        "instrument": c.key,
        "timestamp": event["timestamp"],
        "received_at": event["received_at"],
        "sequence": event["sequence"],
        "bid": event["bid_units"],
        "ask": event["ask_units"],
        "ltp": event["ltp_units"],
        "bid_qty": event["bid_qty"],
        "ask_qty": event["ask_qty"],
        "source": "WS_SNAP_QUOTE",
        "volume_total": event["volume_total"],
    }


def record_decision(store, artifact, row, mode, forecast=None, reason=None, latency=0):
    identity = digest([artifact["id"], row["instrument"], row["bar"], mode])
    payload = {
        "request": {
            "instrument": row["instrument"],
            "bar": row["bar"],
            "mode": mode,
            "model": artifact["id"],
            "side": "NONE",
            "quantity": 0,
        },
        "status": "REJECTED" if reason else "PREDICTED",
        "reason": reason,
        "forecast": forecast,
        "latency_seconds": latency,
    }
    with store.db:
        store.db.execute(
            "INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?)",
            (identity, row["instrument"], row["bar"], now().isoformat(), artifact["id"], canonical(payload)),
        )
    return payload


def run_loop(
    store,
    config,
    client,
    selected,
    seconds,
    artifact=None,
    predictor=None,
    mode="shadow",
    entry_eligible=None,
):
    contracts = store.contracts()
    contexts = (
        context_contracts(store, config, {contracts[k].exchange for k in selected})
        if predictor is not None or artifact is None
        else {}
    )
    feed_keys = list(dict.fromkeys(selected + [c.key for c in contexts.values()]))
    active = [contracts[k] for k in feed_keys]
    lookup = {(c.exchange, c.token): c for c in active}
    builders = {c.key: BarBuilder(c, config) for c in active}
    execution = PaperExecution(store, config)
    jobs, awaiting, failures = {}, {}, 0
    entry_eligible = set(selected if entry_eligible is None else entry_eligible)
    if artifact and mode == "shadow":
        day = now().astimezone(IST).date().isoformat()
        cohort = {"day": day, "started_at": now().isoformat(), "segments": {}}
        for key in sorted(selected):
            cohort["segments"].setdefault(contracts[key].exchange, []).append(key)
        name = "shadow-cohort:" + artifact["id"] + ":" + day
        with store.db:
            store.db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (name, canonical(cohort)))
            old = store.meta(name)
        if old["segments"] != cohort["segments"]:
            raise AIError("shadow cohort is already frozen for this model/session")

    def completed(bar):
        store.add_bar(bar)
        key = bar["instrument"]
        if predictor is None or key not in entry_eligible:
            return
        window = store.window(key, bar["timestamp"])
        row = {"instrument": key, "bar": bar["timestamp"]}
        identity = digest([artifact["id"], key, row["bar"], mode])
        awaiting.pop(identity, None)
        if store.db.execute("SELECT 1 FROM decisions WHERE id=?", (identity,)).fetchone():
            return
        try:
            if now() >= instant(bar["timestamp"]) + 2 * BAR:
                raise AIError("expired target")
            value = input_features(store, contracts[key], window, config)

            def predict():
                if now() >= instant(row["bar"]) + 2 * BAR:
                    raise AIError("expired target")
                return predictor.predict(value)

            submitted = time.monotonic()
            jobs[identity] = (pool.submit(predict), row, submitted)
        except ContextPending:
            awaiting[identity] = bar
        except ValueError:
            record_decision(store, artifact, row, mode, reason="INCOMPLETE_FEATURES")

    day = now().astimezone(IST).date().isoformat()
    halt_key = "entry-halt:" + artifact["id"] + ":" + day if artifact else None
    windows = {(c.exchange_type, c.token): session_window(config, c, day) for c in active}
    source = stream_recordings(client, active, seconds, windows)
    queue = next(source)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-inference")
    ended = False
    last_equity = None
    try:
        while not ended:
            try:
                event = queue.get(timeout=0.1)
            except Empty:
                event = ...
            if event is None:
                ended = True
            elif event is not ...:
                if event["event"] != "quote":
                    if event["status"] in (
                        "UNAVAILABLE",
                        "RECONNECTING",
                        "STOPPED",
                        "CONNECTING",
                        "SUBSCRIBED",
                    ):
                        keys = (
                            [lookup[event["exchange"], event["token"]].key]
                            if "exchange" in event and "token" in event
                            else feed_keys
                        )
                        for key in keys:
                            store.health(key, "UNAVAILABLE", now())
                    emit(event)
                else:
                    q = quote_event(event, lookup)
                    store.add_quote(q)
                    bar = builders[q["instrument"]].push(q)
                    if bar:
                        completed(bar)
            for builder in builders.values():
                bar = builder.flush(now())
                if bar:
                    completed(bar)
            for bar in list(awaiting.values()):
                completed(bar)
            for key in feed_keys:
                if not queue.live.is_set() or key in getattr(queue, "blocked", frozenset()):
                    current = store.db.execute(
                        "SELECT status FROM health WHERE instrument=?", (key,)
                    ).fetchone()
                    if current and current[0] == "LIVE":
                        store.health(key, "UNAVAILABLE", now())
            for identity, (job, row, submitted) in list(jobs.items()):
                if not job.done():
                    continue
                del jobs[identity]
                try:
                    direction = job.result()
                    latency = time.monotonic() - submitted
                    if direction not in DIRECTIONS or latency > 10 or now() >= instant(row["bar"]) + 2 * BAR:
                        raise AIError("invalid or expired model output")
                    if direction == "UP":
                        try:
                            if store.meta(halt_key):
                                raise AIError("entries stopped after a model failure this session")
                            quantity = execution.size(row["instrument"], now())
                            if not queue.live.is_set() or row["instrument"] in queue.blocked:
                                raise AIError("feed became unavailable before execution")
                            outcome = execution.submit(
                                [artifact["id"], row["instrument"], row["bar"], mode],
                                row["instrument"],
                                "BUY",
                                quantity,
                                now(),
                                artifact["id"],
                                row["bar"],
                                mode=mode,
                                forecast=direction,
                                latency=latency,
                            )
                        except (ValueError, KeyError, TypeError):
                            outcome = record_decision(
                                store,
                                artifact,
                                row,
                                mode,
                                forecast=direction,
                                reason="RISK_REJECTED",
                                latency=latency,
                            )
                    else:
                        outcome = record_decision(
                            store, artifact, row, mode, forecast=direction, latency=latency
                        )
                    emit(outcome)
                except (ValueError, KeyError, OSError):
                    failures += 1
                    store.set_meta(halt_key, {"reason": "MODEL_FAILURE", "at": now().isoformat()})
                    emit(
                        record_decision(
                            store,
                            artifact,
                            row,
                            mode,
                            reason="MODEL_FAILURE",
                            latency=time.monotonic() - submitted,
                        )
                    )
            if artifact and mode == "paper":
                for outcome in execution.exits(now()):
                    emit(outcome)
            if artifact:
                observation = (now().astimezone(IST).date(), execution.equity(now()))
                if observation != last_equity:
                    with store.db:
                        store.db.execute(
                            "INSERT OR REPLACE INTO equity VALUES (?,?,?)",
                            (now().isoformat(), observation[1], artifact["id"]),
                        )
                    last_equity = observation
    finally:
        try:
            for builder in builders.values():
                bar = builder.flush(now(), final=True)
                if bar:
                    store.add_bar(bar)
            for _, row, submitted in jobs.values():
                if artifact:
                    record_decision(
                        store,
                        artifact,
                        row,
                        mode,
                        reason="MODEL_FAILURE",
                        latency=time.monotonic() - submitted,
                    )
            for bar in awaiting.values():
                record_decision(
                    store,
                    artifact,
                    {"instrument": bar["instrument"], "bar": bar["timestamp"]},
                    mode,
                    reason="INCOMPLETE_FEATURES",
                )
            for key in feed_keys:
                store.health(key, "UNAVAILABLE", now())
        finally:
            source.close()
            pool.shutdown(wait=True, cancel_futures=True)
        if artifact and mode == "shadow":
            finish_shadow(store, config, artifact["id"])
    return {
        "status": "STOPPED",
        "model_failures": failures,
        "positions": execution.report(now())["unresolved_positions"],
    }


def collect(args, store, config):
    master = _http(MASTER)
    contracts, blocked = resolve_contracts(master, config, now())
    for c in contracts:
        store.add_contract(c)
    day = now().astimezone(IST).date().isoformat()
    checksum = store.archive_master(day, master)
    store.set_meta(
        "master:" + day, {"hash": checksum, "contracts": [asdict(c) for c in contracts], "blocked": blocked}
    )
    if args.metadata_only:
        return {"contracts": len(contracts), "blocked": blocked, "account_access": "UNVERIFIED"}
    if not contracts:
        raise AIError("no verified metadata; populate the private market configuration first")
    if bool(args.history_from) != bool(args.history_to):
        raise AIError("supply both history boundaries")
    if args.history_only and not args.history_from:
        raise AIError("--history-only requires both history boundaries")
    if args.history_from:
        start, end = instant(args.history_from), instant(args.history_to)
        if start >= end or end > now():
            raise AIError("history requires increasing past timestamps")
    settings = _local_settings(args.env_file)
    client = _local_login(settings, streaming=not args.history_only, client_type=MarketData)
    try:
        if args.history_from:
            for c in contracts:
                cursor = start
                while cursor < end:
                    boundary = min(end, cursor + timedelta(days=100))
                    range_key = "history:" + digest([c.key, cursor.isoformat(), boundary.isoformat()])
                    if not store.meta(range_key):
                        bars = paced_history(client, c, cursor, boundary)
                        for bar in bars:
                            store.add_bar(bar)
                        store.set_meta(range_key, {"rows": len(bars), "downloaded_at": now().isoformat()})
                    cursor = boundary
        if args.history_only:
            return {
                "status": "HISTORY_COLLECTED",
                "contracts": len(contracts),
                "account_access": "LOGIN_ONLY_NOT_LIVE_VERIFIED",
            }
        day = now().astimezone(IST).date().isoformat()
        selected = store.shortlist(config, day, [c.key for c in contracts], purpose="recording")
        if not selected:
            raise AIError(
                "no recording shortlist; download prior-session history and configure today's sessions"
            )
        return run_loop(store, config, client, selected, args.seconds)
    finally:
        client.close()


def run(args, store, config):
    if args.exit_only:
        if args.mode != "paper":
            raise AIError("--exit-only requires --mode paper")
        artifact, predictor, states = {"id": "EXIT_ONLY"}, None, {}
    else:
        if not args.model_dir:
            raise AIError("run requires --model-dir or --exit-only")
        artifact, predictor = model_artifact(args.model_dir)
        states = readiness(store, config, artifact, artifact["dataset"])
    allowed = ("PAPER_ENABLED",) if args.mode == "paper" else ("SHADOW_READY", "PAPER_ENABLED")
    eligible = [
        key
        for key, c in store.contracts().items()
        if c.exchange in states and states[c.exchange]["status"] in allowed
    ]
    positions = [r[0] for r in store.db.execute("SELECT instrument FROM positions")]
    if not eligible and not (positions and args.mode == "paper"):
        raise AIError(
            "no segment has passed the required evaluation and shadow gates; use report for reasons"
        )
    day = now().astimezone(IST).date().isoformat()
    selected = store.shortlist(config, day, eligible) if eligible else []
    selected = list(dict.fromkeys(positions + selected))[:14]
    if not selected:
        raise AIError("no eligible liquid contracts in today's frozen shortlist")
    # Rebind against today's authoritative master before authentication/execution.
    actual, _ = resolve_contracts(_http(MASTER), config, now())
    actual = {c.key: c for c in actual}
    for key in selected:
        old = asdict(store.contract(key))
        fresh = asdict(actual[key]) if key in actual else {}
        if {k: v for k, v in old.items() if k != "verified_on"} != {
            k: v for k, v in fresh.items() if k != "verified_on"
        }:
            raise AIError("selected contract no longer matches today's verified master")
        store.add_contract(actual[key])
    settings = _local_settings(args.env_file)
    client = _local_login(settings, streaming=True, client_type=MarketData)
    try:
        return run_loop(
            store, config, client, selected, args.seconds, artifact, predictor, args.mode, eligible
        )
    finally:
        client.close()


def main(argv=None):
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/ai.sqlite3", help="separate private AI journal")
    parser.add_argument("--config", default="market.local.json", help="private verified market configuration")
    parser.add_argument("--env-file", default=".env")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("collect", help="record authorised data and incrementally fetch history")
    p.add_argument("--metadata-only", action="store_true")
    p.add_argument("--history-from")
    p.add_argument("--history-to")
    p.add_argument(
        "--history-only", action="store_true", help="download past candles without opening a live feed"
    )
    p.add_argument("--seconds", type=int, default=60)
    p = commands.add_parser("prepare", help="create an immutable chronological 60/20/20 dataset")
    p.add_argument("--output", required=True)
    p = commands.add_parser("train", help="fine-tune Qwen3-0.6B locally with CPU or CUDA")
    p.add_argument("--dataset", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    p.add_argument("--benchmark-only", action="store_true")
    p.add_argument("--gguf-tools", help="reviewed llama.cpp checkout containing convert_hf_to_gguf.py")
    p.add_argument("--import-ollama", action="store_true")
    p = commands.add_parser(
        "evaluate", help="evaluate validation first, then consume the untouched holdout once"
    )
    p.add_argument("--dataset", required=True)
    p.add_argument("--model-dir", required=True)
    p.add_argument("--base-model", default="qwen3:0.6b", help="installed, unfine-tuned local baseline")
    p.add_argument("--split", choices=("validation", "test"), default="validation")
    p = commands.add_parser("run", help="run a performance-gated shadow or paper session")
    p.add_argument("--model-dir")
    p.add_argument(
        "--exit-only",
        action="store_true",
        help="manage existing virtual exits without a working LLM; requires --mode paper",
    )
    p.add_argument("--mode", choices=("shadow", "paper"), default="shadow")
    p.add_argument("--seconds", type=int, default=60)
    p = commands.add_parser("report", help="export private coverage, gates, decisions and account results")
    p.add_argument("--model-dir")
    p.add_argument("--output")
    args = parser.parse_args(argv)
    store = None
    try:
        if args.command == "train":
            result = train(
                args.dataset,
                args.output,
                args.device,
                args.benchmark_only,
                args.gguf_tools,
                args.import_ollama,
            )
        else:
            config = load_config(args.config)
            store = Store(args.db)
            if args.command == "collect":
                result = collect(args, store, config)
            elif args.command == "prepare":
                result = prepare(store, config, args.output)
            elif args.command == "evaluate":
                artifact, predictor = model_artifact(args.model_dir)
                baseline = Ollama(args.base_model)
                result = evaluate(store, config, args.dataset, artifact, predictor, baseline, args.split)
            elif args.command == "run":
                result = run(args, store, config)
            else:
                artifact = model_artifact(args.model_dir, check_runtime=False)[0] if args.model_dir else None
                result = {
                    "segments": readiness(store, config, artifact, artifact["dataset"] if artifact else None),
                    "journal": PaperExecution(store, config).report(now()),
                    "evaluations": [
                        json.loads(r[0])
                        for r in store.db.execute(
                            "SELECT payload FROM metadata WHERE key LIKE 'evaluation:%' AND key NOT LIKE '%:lock'"
                        )
                    ],
                }
                if args.output:
                    write_json(args.output, result)
        emit(result)
        return 0
    except SmartAPIError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (
        ValueError,
        OSError,
        sqlite3.Error,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.CalledProcessError,
    ):
        # Configuration and transport details may contain private values; output controlled guidance.
        print(
            "Error: operation stopped. Verify local configuration, dependencies, data coverage and model gates; no broker order was sent.",
            file=sys.stderr,
        )
        return 1
    except (KeyboardInterrupt, EOFError):
        print(
            "Cancelled; local sockets and sessions are closing. Inspect unresolved virtual positions with report.",
            file=sys.stderr,
        )
        return 130
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
