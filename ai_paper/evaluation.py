"""Recorded-book replay and immutable, per-segment model promotion evidence."""

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from ai_paper.execution import PaperExecution
from ai_paper.learning import Ollama, complete_sessions, file_hash, forecast_metrics, read_dataset
from ai_paper.market import (
    AIError,
    BAR,
    DIRECTIONS,
    SEGMENTS,
    Contract,
    Store,
    calendars_match,
    canonical,
    charges,
    config_id,
    digest,
    instant,
    session_window,
)
from smartapi_data import IST


def model_artifact(directory, check_runtime=True):
    directory = Path(directory)
    artifact = json.loads((directory / "model.json").read_text(encoding="utf-8"))
    training = {
        k: v for k, v in artifact.items() if k not in ("id", "gguf_hash", "ollama_name", "ollama_digest")
    }
    if digest(training) != artifact["id"] or artifact["benchmark_only"]:
        raise AIError("model artifact is invalid or benchmark-only")
    for name, value in artifact["adapter_hashes"].items():
        if Path(name).name != name or file_hash(directory / "adapter" / name) != value:
            raise AIError("model weights were modified")
    if file_hash(directory / "model.f16.gguf") != artifact["gguf_hash"]:
        raise AIError("GGUF weights were modified")
    local = Ollama(artifact["ollama_name"]) if check_runtime else None
    if local and local.model_digest() != artifact["ollama_digest"]:
        raise AIError("installed Ollama model does not match the trained artifact")
    return artifact, local


def replay(source, config, rows, predictions, model, latencies=None):
    """Book events become available at receipt time; candles never execute orders."""
    if len(rows) != len(predictions):
        raise AIError("prediction count mismatch")
    latencies = latencies or [0] * len(rows)
    if len(latencies) != len(rows):
        raise AIError("latency count mismatch")
    temporary = Store(":memory:")
    try:
        for c in source.contracts().values():
            temporary.add_contract(c)
        execution = PaperExecution(temporary, config)
        if not rows:
            return {"complete": False, "reason": "no replay examples"}
        start = min(instant(r["bar"]) + BAR for r in rows)
        end = max(instant(r["target_end"]) for r in rows) + timedelta(seconds=30)
        # shortcut: replay retains its window in RAM; stream to a scratch journal if recordings exceed memory.
        events, covered, rejected_inputs = [], set(), 0
        instruments = {r["instrument"] for r in rows}
        for row in source.db.execute(
            "SELECT * FROM quotes WHERE received_at>=? AND received_at<=? ORDER BY received_at,rowid",
            (start.isoformat(), end.isoformat()),
        ):
            if row["instrument"] in instruments:
                events.append((instant(row["received_at"]), 1, "quote", json.loads(row["payload"])))
        for row in source.db.execute(
            "SELECT * FROM health_events WHERE at>=? AND at<=? ORDER BY at",
            (start.isoformat(), end.isoformat()),
        ):
            if row["instrument"] in instruments:
                events.append((instant(row["at"]), 0, "health", dict(row)))
        for index, (row, direction, latency) in enumerate(zip(rows, predictions, latencies)):
            if direction not in DIRECTIONS or not 0 <= latency <= 10:
                rejected_inputs += 1
                continue
            at = instant(row["bar"]) + BAR + timedelta(seconds=latency)
            events.append((at, 2, "decision", (index, row, direction)))
        for now, _, kind, value in sorted(events, key=lambda e: (e[0], e[1])):
            if kind == "quote":
                try:
                    if "contract_spec" in value:
                        temporary.add_contract(Contract(**value["contract_spec"]))
                    temporary.add_quote({**value, "received_at": now.isoformat()})
                except ValueError:
                    rejected_inputs += 1
                    temporary.health(value["instrument"], "UNAVAILABLE", now)
            elif kind == "health":
                temporary.health(value["instrument"], value["status"], now)
            execution.exits(now)
            if kind == "decision":
                index, row, direction = value
                try:
                    execution.mark(row["instrument"], now)
                    covered.add(index)
                    if direction == "UP":
                        quantity = execution.size(row["instrument"], now)
                        execution.submit(
                            [model, row["instrument"], row["bar"], "BUY"],
                            row["instrument"],
                            "BUY",
                            quantity,
                            now,
                            model,
                            row["bar"],
                            forecast=direction,
                            latency=latencies[index],
                        )
                except (ValueError, KeyError, TypeError):
                    continue
            equity = execution.equity(now)
            with temporary.db:
                temporary.db.execute(
                    "INSERT OR REPLACE INTO equity VALUES (?,?,?)", (now.isoformat(), equity, model)
                )
        report = execution.report(end)
        initial = report["account"]["initial_cash_paise"]
        return {
            "complete": len(covered) == len(rows)
            and not report["unresolved_positions"]
            and rejected_inputs == 0,
            "coverage": len(covered),
            "requested": len(rows),
            "invalid_events": rejected_inputs,
            "net_pnl_paise": None if report["equity_paise"] is None else report["equity_paise"] - initial,
            "max_drawdown_paise": report["max_drawdown_paise"],
            "fees_paise": report["fees_paise"],
            "turnover_paise": report["turnover_paise"],
            "fills": len(report["fills"]),
            "unresolved": report["unresolved_positions"],
            "rejections": len(report["rejections"]),
        }
    finally:
        temporary.close()


def evaluate(store, config, dataset, artifact, predictor, baseline, split="validation"):
    import time

    manifest, values = read_dataset(dataset)
    if (
        artifact["dataset"] != manifest["id"]
        or artifact["config_hash"] != config_id(config)
        or not calendars_match(config, manifest["calendars"])
    ):
        raise AIError("model, dataset and market configuration do not match")
    if split not in ("validation", "test"):
        raise AIError("evaluate validation or untouched test only")
    report_key = (
        "evaluation:" + manifest["id"] + ":" + split + (":" + artifact["id"] if split == "validation" else "")
    )
    old = store.meta(report_key)
    if old:
        if old["model_id"] != artifact["id"]:
            raise AIError("holdout is locked; use a later untouched dataset for a different model")
        return old
    if split == "test":
        validation = store.meta("evaluation:" + manifest["id"] + ":validation:" + artifact["id"])
        if (
            not validation
            or validation["model_id"] != artifact["id"]
            or not any(r["passed"] for r in validation["segments"].values())
        ):
            raise AIError("validation gates must pass before consuming the final holdout")
    # Reserve before inference so interrupted/failed attempts cannot quietly reuse a holdout.
    lock_key = report_key + ":lock"
    baseline_digest = baseline.model_digest()
    with store.db:
        store.db.execute(
            "INSERT OR IGNORE INTO metadata VALUES (?,?)",
            (
                lock_key,
                canonical(
                    {
                        "model_id": artifact["id"],
                        "baseline_digest": baseline_digest,
                        "dataset": manifest["id"],
                        "predictions": [],
                        "bases": [],
                        "latencies": [],
                    }
                ),
            ),
        )
        lock = store.meta(lock_key)
    if lock["model_id"] != artifact["id"] or lock["baseline_digest"] != baseline_digest:
        raise AIError("holdout already reserved for a different model or baseline")
    predictions, bases, latencies = lock["predictions"], lock["bases"], lock["latencies"]
    for row in values[split][len(predictions) :]:
        previous = canonical(lock)
        started = time.monotonic()
        prediction = predictor.predict(row["features"])
        latency = time.monotonic() - started
        base = baseline.predict(row["features"])
        predictions.append(prediction)
        latencies.append(latency)
        bases.append(base)
        with store.db:
            updated = store.db.execute(
                "UPDATE metadata SET payload=? WHERE key=? AND payload=?",
                (canonical(lock), lock_key, previous),
            ).rowcount
            if updated != 1:
                raise AIError("evaluation advanced in another process; resume its saved results")
    segments = {}
    for segment in SEGMENTS:
        indexes = [i for i, row in enumerate(values[split]) if row["segment"] == segment]
        if not indexes:
            segments[segment] = {"passed": False, "reason": "no eligible holdout data"}
            continue
        rows = [values[split][i] for i in indexes]
        selected = [predictions[i] for i in indexes]
        metrics = forecast_metrics(
            [r for r in values["train"] if r["segment"] == segment],
            rows,
            selected,
            [bases[i] for i in indexes],
        )
        result = replay(store, config, rows, selected, artifact["id"], [latencies[i] for i in indexes])
        momentum = replay(store, config, rows, [r["momentum"] for r in rows], "momentum")
        cover = manifest["coverage"][segment]
        coverage_pass = cover["train"] >= 60 and cover["validation"] >= 20 and cover["test"] >= 20
        better = metrics["model"] > max(metrics[k] for k in ("majority", "momentum", "base_model"))
        net = result["net_pnl_paise"]
        returns_pass = (
            result["complete"]
            and momentum["complete"]
            and net is not None
            and net > max(0, momentum["net_pnl_paise"])
        )
        sorted_latencies = sorted(latencies[i] for i in indexes)
        p95 = sorted_latencies[min(len(sorted_latencies) - 1, int(0.95 * len(sorted_latencies)))]
        segments[segment] = {
            "passed": coverage_pass and better and returns_pass and p95 <= 10,
            "coverage": cover,
            "metrics": metrics,
            "replay": result,
            "momentum_replay": momentum,
            "latency_p95_seconds": p95,
        }
    report = {
        "version": 1,
        "model_id": artifact["id"],
        "model_digest": artifact["ollama_digest"],
        "dataset": manifest["id"],
        "config_hash": config_id(config),
        "calendars": manifest["calendars"],
        "split": split,
        "segments": segments,
        "baseline_digest": baseline_digest,
        "prediction_hash": digest([predictions, bases]),
        "test_end": max(r["target_end"] for r in values[split]),
    }
    store.set_meta(report_key, report)
    return report


def readiness(store, config, model=None, dataset=None, now=None):
    now = now or datetime.now(timezone.utc)
    report = store.meta("evaluation:" + dataset + ":test") if dataset else None
    contracts = store.contracts()
    days = complete_sessions(store, config)
    result = {}
    for segment in SEGMENTS:
        counts = sum(c.exchange == segment for c in contracts.values())
        complete = set().union(*(days[k] for k, c in contracts.items() if c.exchange == segment))
        state, reasons = "COLLECTING", []
        if not counts:
            reasons.append("no verified contracts")
        if (
            report
            and model
            and report["model_id"] == model["id"]
            and report["model_digest"] == model["ollama_digest"]
            and report["config_hash"] == config_id(config)
            and calendars_match(config, report["calendars"])
        ):
            state = "EVALUATING"
            if report["segments"][segment].get("passed"):
                state = "SHADOW_READY"
                sessions = store.db.execute(
                    "SELECT COUNT(*) FROM shadow WHERE model=? AND segment=? AND complete=1 AND failures=0 AND day>?",
                    (model["id"], segment, instant(report["test_end"]).astimezone(IST).date().isoformat()),
                ).fetchone()[0]
                verified = config["segments"][segment].get("live_verified_on")
                try:
                    fresh_verification = (
                        verified is not None
                        and 0 <= (now.astimezone(IST).date() - date.fromisoformat(verified)).days <= 7
                    )
                except (ValueError, TypeError):
                    fresh_verification = False
                usable = False
                for c in contracts.values():
                    if c.exchange != segment or c.kind == "INDEX" or not c.verified(now):
                        continue
                    try:
                        opened, closed = session_window(config, c, now.astimezone(IST).date().isoformat())
                        charges(config, c, now, 1, "BUY")
                        charges(config, c, now, 1, "SELL")
                        usable |= closed - opened >= 2 * BAR and now < closed
                    except (ValueError, KeyError, TypeError):
                        continue
                if sessions >= 2 and fresh_verification and usable:
                    state = "PAPER_ENABLED"
                else:
                    reasons.append(
                        "two subsequent complete shadow sessions, fresh local verification and current tradable metadata/calendar/costs required"
                    )
            else:
                reasons.append("holdout performance/recorded-book coverage gate failed")
        else:
            reasons.append("locked evaluation for this model/configuration is unavailable")
        result[segment] = {
            "status": state,
            "contracts": counts,
            "complete_sessions": len(complete),
            "reasons": reasons,
        }
    return result


def finish_shadow(store, config, model):
    """Award evidence only for complete recorded sessions and complete predictions."""
    contracts = store.contracts()
    grouped = {}
    for row in store.db.execute(
        "SELECT payload FROM metadata WHERE key LIKE ?", ("shadow-cohort:" + model + ":%",)
    ):
        cohort = json.loads(row[0])
        for segment, keys in cohort["segments"].items():
            grouped[segment, cohort["day"]] = (keys, instant(cohort["started_at"]))
    for (segment, day), (keys, started_at) in grouped.items():
        complete, failures = True, 0
        for key in keys:
            opened, closed = session_window(config, contracts[key], day)
            complete &= started_at <= opened
            times = [
                instant(r[0])
                for r in store.db.execute(
                    "SELECT received_at FROM quotes WHERE instrument=? AND received_at>=? AND received_at<? ORDER BY received_at",
                    (key, opened.isoformat(), closed.isoformat()),
                )
            ]
            complete &= (
                bool(times)
                and times[0] <= opened + timedelta(seconds=30)
                and times[-1] >= closed - timedelta(seconds=30)
            )
            complete &= all(b - a <= timedelta(seconds=30) for a, b in zip(times, times[1:]))
            failures += store.db.execute(
                "SELECT COUNT(*) FROM health_events WHERE instrument=? AND status!='LIVE' AND at>=? AND at<?",
                (key, opened.isoformat(), closed.isoformat()),
            ).fetchone()[0]
            decisions = [
                json.loads(r[0])
                for r in store.db.execute(
                    "SELECT payload FROM decisions WHERE instrument=? AND model=? AND bar>=? AND bar<?",
                    (key, model, opened.isoformat(), closed.isoformat()),
                )
            ]
            expected = {
                b["timestamp"]
                for b in store.bars(key)
                if opened + BAR * 23 <= instant(b["timestamp"]) < closed - BAR
            }
            actual = {
                d["request"]["bar"]
                for d in decisions
                if d.get("forecast") in DIRECTIONS and d["request"]["mode"] == "shadow"
            }
            complete &= bool(expected) and expected <= actual
            failures += sum(d.get("reason") == "MODEL_FAILURE" for d in decisions)
        with store.db:
            store.db.execute(
                "INSERT OR REPLACE INTO shadow VALUES (?,?,?,?,?)",
                (model, segment, day, int(complete), failures),
            )
