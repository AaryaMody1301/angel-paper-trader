"""Fictional market/account fixtures; no credentials or live connections."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
import unittest
from threading import Event
from queue import Empty
from unittest.mock import Mock, patch
from contextlib import redirect_stderr, redirect_stdout

from ai_paper.evaluation import evaluate, model_artifact, readiness, replay, finish_shadow
from ai_paper.execution import PaperExecution
from ai_paper.learning import (
    ContextPending,
    features,
    file_hash,
    input_features,
    macro_f1,
    prepare,
    read_dataset,
    resolve_device,
    Ollama,
)
from ai_paper.market import (
    BAR,
    SEGMENTS,
    BarBuilder,
    Contract,
    Store,
    canonical,
    config_id,
    digest,
    exact,
    parse_full,
    resolve_contracts,
)
from ai_trader import main, quote_event, run_loop
from paper_trader import PaperAccount, amount
from smartapi_stream import parse_snap_quote
from smartapi_data import SmartAPIData, Settings

NOW = datetime(2026, 1, 5, 6, tzinfo=timezone.utc)


def configuration(days=1):
    sessions = {}
    for offset in range(days):
        at = NOW - timedelta(days=days - 1 - offset)
        sessions[at.date().isoformat()] = {
            "open": at.replace(hour=3, minute=45).isoformat(),
            "close": at.replace(hour=6, minute=25).isoformat(),
        }
    spec = {
        "verified_on": NOW.date().isoformat(),
        "live_verified_on": None,
        "scale": 100,
        "multiplier": "1",
        "master_tick_factor": "1",
        "sessions": sessions,
        "costs": {
            "valid_from": "2020-01-01",
            "valid_until": "2030-01-01",
            "buy_bps": 1,
            "sell_bps": 1,
            "flat_paise": 1,
            "slippage_bps": 1,
        },
    }
    return {"version": 1, "segments": {s: deepcopy(spec) for s in SEGMENTS}, "contracts": {}}


def contract(exchange="NSE", **kwargs):
    return Contract(
        exchange, "TEST&CO-EQ", "123456", "CASH", 100, 5, 1, "1", verified_on=NOW.date().isoformat(), **kwargs
    )


def quote(c, at=NOW, sequence=1, bid=10000, ask=10010, qty=1000000):
    return {
        "instrument": c.key,
        "timestamp": at.isoformat(),
        "received_at": at.isoformat(),
        "sequence": sequence,
        "bid": bid,
        "ask": ask,
        "ltp": bid,
        "bid_qty": qty,
        "ask_qty": qty,
        "source": "WS_SNAP_QUOTE",
        "volume_total": sequence * 100,
    }


def add_session(store, c, spec, day, base=10000):
    opened = datetime.fromisoformat(spec["sessions"][day]["open"])
    for index in range(32):
        price = base + index * 20
        store.add_bar(
            {
                "instrument": c.key,
                "timestamp": (opened + index * BAR).isoformat(),
                "ohlc": [price, price + 10, price - 5, price + 5],
                "volume": 100 + index,
                "origin": "SMARTAPI_HISTORY",
                "flags": [],
            }
        )


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / "ai.sqlite3"))
        self.addCleanup(self.store.close)
        self.config = configuration()
        self.c = contract()
        self.store.add_contract(self.c)
        self.execution = PaperExecution(self.store, self.config)
        self.store.add_quote(quote(self.c))
        self.bar = (NOW - BAR).isoformat()

    def submit(self, identity="entry", c=None, **kwargs):
        c = c or self.c
        return self.execution.submit(
            identity,
            c.key,
            kwargs.pop("side", "BUY"),
            kwargs.pop("quantity", 10),
            kwargs.pop("at", NOW),
            "model-1",
            self.bar,
            **kwargs,
        )

    def test_exact_cash_roundtrip_and_restart_idempotency(self):
        first = self.submit()
        self.assertEqual(first["status"], "FILLED")
        reopened = Store(str(Path(self.temp.name) / "ai.sqlite3"))
        try:
            engine = PaperExecution(reopened, self.config)
            self.assertEqual(
                engine.submit("entry", self.c.key, "BUY", 10, NOW + BAR, "model-1", self.bar), first
            )
        finally:
            reopened.close()
        self.store.add_quote(quote(self.c, NOW + BAR, 2, 11000, 11010))
        exits = self.execution.exits(NOW + BAR)
        self.assertEqual(exits[0]["status"], "FILLED")
        report = self.execution.report(NOW + BAR)
        self.assertFalse(report["unresolved_positions"])
        self.assertGreater(report["realized_paise"], 0)
        self.assertEqual(report["equity_paise"], 1000000000 + report["realized_paise"])

    def test_new_native_currency_units_and_whole_lots(self):
        c = replace(
            self.c,
            exchange="CDS",
            kind="FUTURE",
            scale=10_000_000,
            tick=25000,
            lot=1000,
            expiry=(NOW + timedelta(days=10)).isoformat(),
        )
        self.store.add_contract(c)
        self.store.add_quote(quote(c, bid=830000000, ask=830025000, qty=10000))
        rejected = self.submit("badlot", c, quantity=1)
        self.assertEqual(rejected["status"], "REJECTED")
        filled = self.submit("currency", c, quantity=1000)
        self.assertEqual(filled["status"], "FILLED")
        self.assertEqual(filled["price_units"] % c.tick, 0)
        self.assertEqual(filled["price_scale"], 10_000_000)
        self.assertEqual(c.notional(25000, 1000), 250)

    def test_futures_use_full_notional_and_options_use_premium(self):
        for kind in ("FUTURE", "OPTION"):
            c = replace(self.c, symbol=kind, kind=kind, lot=25, expiry=(NOW + timedelta(days=2)).isoformat())
            self.store.add_contract(c)
            self.store.add_quote(quote(c))
            filled = self.submit(kind, c, quantity=25)
            self.assertEqual(filled["status"], "FILLED")
            self.assertGreater(filled["notional_paise"], 250000)

    def test_liquidity_cannot_be_reused_after_roundtrip(self):
        q = quote(self.c, sequence=2, qty=10)
        self.store.add_quote(q)
        self.assertEqual(self.submit()["status"], "FILLED")
        self.assertEqual(self.submit("exit", side="SELL")["status"], "FILLED")
        before = self.execution.report(NOW)["account"]["cash_paise"]
        self.assertEqual(self.submit("again")["status"], "REJECTED")
        self.assertEqual(self.execution.report(NOW)["account"]["cash_paise"], before)
        self.store.add_quote({**q, "sequence": 3})
        self.assertEqual(self.submit("new-sequence-same-book")["status"], "REJECTED")
        self.store.add_quote({**q, "sequence": 4, "bid": 10005})
        self.assertEqual(self.submit("bid-only-change")["status"], "REJECTED")
        self.store.add_quote({**q, "sequence": 5, "bid": 10005, "ask": 10015})
        self.assertEqual(self.submit("new-ask-book")["status"], "FILLED")

    def test_analytical_index_with_spaces_and_zero_tick_is_never_executable(self):
        c = replace(self.c, symbol="Nifty 50", token="99926000", kind="INDEX", tick=0)
        self.store.add_contract(c)
        self.store.add_quote({**quote(c, bid=0, ask=0, qty=0), "ltp": 2500000})
        self.assertEqual(self.submit("index", c)["status"], "REJECTED")
        with self.assertRaises(ValueError):
            self.execution.size(c.key, NOW)

    def test_quiet_risk_polls_and_repeated_exit_rejections_do_not_rewrite_the_journal(self):
        self.assertEqual(self.submit()["status"], "FILLED")
        before = self.store.db.total_changes
        with self.store.db:
            for _ in range(3):
                self.assertFalse(self.execution.risk(NOW)["halted"])
        self.assertEqual(self.store.db.total_changes, before)
        self.store.health(self.c.key, "UNAVAILABLE", NOW + BAR)
        self.assertEqual(self.execution.exits(NOW + BAR)[0]["status"], "REJECTED")
        before = self.store.db.total_changes
        self.assertEqual(self.execution.exits(NOW + BAR), [])
        self.assertEqual(self.store.db.total_changes, before)
        self.assertEqual(len(self.execution.report(NOW + BAR)["unresolved_positions"]), 1)
        self.store.add_quote(quote(self.c, NOW + BAR + timedelta(seconds=1), 2, 11000, 11010))
        self.assertEqual(self.execution.exits(NOW + BAR + timedelta(seconds=1))[0]["status"], "FILLED")

    def test_invalid_quote_and_new_sequence_same_millisecond(self):
        self.store.add_quote(quote(self.c, sequence=2, bid=10005, ask=10015))
        self.assertEqual(self.store.latest(self.c.key)["payload"]["bid"], 10005)
        with self.assertRaises(ValueError):
            self.store.add_quote(quote(self.c, sequence=2, bid=10010, ask=10020))
        with self.assertRaises(ValueError):
            self.store.add_quote(quote(self.c, NOW - timedelta(milliseconds=1), 3))

    def test_rejected_fills_preserve_cash_and_positions_and_journal_trigger_rolls_back(self):
        before = self.execution.report(NOW)["account"]
        self.assertEqual(self.submit("huge", quantity=10000000)["status"], "REJECTED")
        self.assertEqual(before, self.execution.report(NOW)["account"])
        self.store.db.execute(
            "CREATE TRIGGER fail_fill BEFORE INSERT ON fills BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit()
        self.assertFalse(self.store.db.execute("SELECT 1 FROM positions").fetchone())
        self.assertFalse(
            self.store.db.execute("SELECT 1 FROM decisions WHERE id=?", (digest("entry"),)).fetchone()
        )
        self.assertEqual(before, self.execution.report(NOW)["account"])

    def test_unavailable_feed_stale_quote_and_expired_model_stop_entries(self):
        self.store.health(self.c.key, "UNAVAILABLE", NOW)
        self.assertEqual(self.submit("disconnected")["status"], "REJECTED")
        self.store.health(self.c.key, "LIVE", NOW)
        self.assertEqual(self.submit("late", at=NOW + timedelta(seconds=31))["status"], "REJECTED")
        self.store.add_quote(quote(self.c, NOW + BAR, 2))
        self.assertEqual(self.submit("expired", at=NOW + BAR)["status"], "REJECTED")

    def test_missing_marks_hide_equity_and_daily_loss_stop_persists(self):
        self.submit()
        self.store.health(self.c.key, "UNAVAILABLE", NOW)
        self.assertIsNone(self.execution.equity(NOW))
        with self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            self.store.db.execute("UPDATE account SET cash=cash-20000000")
        self.store.add_quote(quote(self.c, sequence=2))
        with self.store.db:
            self.assertTrue(self.execution.risk(NOW)["halted"])
        self.assertEqual(self.execution.exits(NOW)[0]["status"], "FILLED")

    def test_offline_journals_cannot_be_opened_or_reset(self):
        path = Path(self.temp.name) / "old.sqlite3"
        old = PaperAccount(path)
        old.initialize(amount("100000"))
        old.close()
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            Store(str(path))
        self.assertEqual(path.read_bytes(), original)

    def test_exchange_collisions_and_special_character_symbol(self):
        other = replace(self.c, exchange="BSE")
        self.store.add_contract(other)
        self.store.add_quote(quote(other))
        self.assertNotEqual(other.key, self.c.key)
        self.assertEqual(self.submit("bse", other)["status"], "FILLED")
        self.assertEqual(self.submit()["status"], "FILLED")
        old = PaperAccount(":memory:")
        try:
            old.initialize(100000)
            old.quote("M&M-EQ", 100, 105)
            self.assertEqual(old.trade("ampersand", "M&M-EQ", "BUY", 1)["symbol"], "M&M-EQ")
        finally:
            old.close()

    def test_shadow_never_changes_balances_and_request_id_conflicts_fail(self):
        before = self.execution.report(NOW)["account"]
        self.assertEqual(self.submit(mode="shadow")["status"], "SHADOW")
        self.assertEqual(self.execution.report(NOW)["account"], before)
        with self.assertRaises(ValueError):
            self.submit(quantity=2, mode="shadow")

    def test_sizing_includes_tick_slippage_and_costs_before_submit(self):
        quantity = self.execution.size(self.c.key, NOW)
        result = self.submit(quantity=quantity)
        self.assertEqual(result["status"], "FILLED")
        self.assertLessEqual(result["notional_paise"], 250000000)

    def test_fourth_position_is_rejected(self):
        for index in range(4):
            c = replace(self.c, symbol="STOCK" + str(index))
            self.store.add_contract(c)
            self.store.add_quote(quote(c))
            result = self.submit(str(index), c)
            self.assertEqual(result["status"], "FILLED" if index < 3 else "REJECTED")


class DataTests(unittest.TestCase):
    def test_native_precision_and_cash_conversion(self):
        self.assertEqual(exact("0.0025", 10000000), 25000)
        for value in ("0.00000001", "NaN", True, "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                exact(value, 10000000)

    def test_master_requires_verified_multiplier_and_exact_expiry(self):
        row = {
            "exch_seg": "NSE",
            "token": "123456",
            "symbol": "TEST&CO-EQ",
            "lotsize": "1",
            "tick_size": "5.000",
            "instrumenttype": "",
            "expiry": "",
        }
        config = configuration()
        found, blocked = resolve_contracts([row], config, NOW)
        self.assertEqual(len(found), 1)
        self.assertFalse(blocked)
        config["segments"]["NSE"]["verified_on"] = None
        self.assertFalse(resolve_contracts([row], config, NOW)[0])
        row.update(exch_seg="NFO", instrumenttype="FUTIDX", expiry="29JAN2026")
        self.assertFalse(resolve_contracts([row], configuration(), NOW)[0])

    def test_all_segment_wire_codes_and_native_scale(self):
        for segment, wire in SEGMENTS.items():
            c = contract(segment)
            packet = bytearray(379)
            struct.pack_into(
                "<BB25sqqq", packet, 0, 3, wire, c.token.encode(), 1, int(NOW.timestamp() * 1000), 10005
            )
            struct.pack_into("<d", packet, 67, 1000)
            for index in range(10):
                struct.pack_into(
                    "<hqqh", packet, 147 + 20 * index, int(index < 5), 10, 10000 if index < 5 else 10010, 1
                )
            event = parse_snap_quote(bytes(packet), c, NOW)
            normal = quote_event(event, {(c.exchange, c.token): c})
            self.assertEqual((normal["bid"], normal["ask"], normal["volume_total"]), (10000, 10010, 1000))
            self.assertEqual(normal["bid_qty"], 50)

    def test_bse_cash_without_nse_suffix_requires_verified_security_kind(self):
        config = configuration()
        row = {
            "exch_seg": "BSE",
            "symbol": "ANDHRAPET",
            "token": "500012",
            "lotsize": "1",
            "tick_size": "1.000000",
            "instrumenttype": "",
            "expiry": "",
        }
        self.assertFalse(resolve_contracts([row], config, NOW)[0])
        config["contracts"]["BSE:500012"] = {"kind": "CASH", "verified_on": NOW.date().isoformat()}
        contracts, blocked = resolve_contracts([row], config, NOW)
        self.assertEqual(contracts[0].kind, "CASH")
        self.assertFalse(blocked)

    def test_rest_depth_scaling_and_quantity(self):
        c = replace(contract("CDS"), scale=10000000, tick=25000)
        data = {
            "unfetched": [],
            "fetched": [
                {
                    "exchange": "CDS",
                    "tradingSymbol": c.symbol,
                    "symbolToken": c.token,
                    "exchFeedTime": "05-Jan-2026 11:30:00",
                    "ltp": Decimal("83.0025"),
                    "depth": {
                        "buy": [{"quantity": 20, "orders": 1, "price": Decimal("83.0000")}],
                        "sell": [{"quantity": 30, "orders": 1, "price": Decimal("83.0025")}],
                    },
                }
            ],
        }
        q = parse_full(data, c, NOW)
        self.assertEqual(q["ask"], 830025000)
        self.assertEqual(q["ask_qty"], 30)

    def test_index_snapshots_use_ltp_without_inventing_depth(self):
        c = replace(contract(), symbol="Nifty 50", token="99926000", kind="INDEX", tick=0)
        packet = bytearray(379)
        struct.pack_into(
            "<BB25sqqq", packet, 0, 3, 1, c.token.encode(), 1, int(NOW.timestamp() * 1000), 2500000
        )
        event = parse_snap_quote(bytes(packet), c, NOW)
        self.assertEqual(event["ltp_units"], 2500000)
        self.assertEqual(
            (event["bid_units"], event["ask_units"], event["bid_qty"], event["ask_qty"]), (0, 0, 0, 0)
        )

    def test_recorded_bar_volume_and_gaps_are_not_invented(self):
        c, config = contract(), configuration()
        builder = BarBuilder(c, config)
        start = NOW.replace(hour=3, minute=45)
        q = quote(c, start)
        q["volume_total"] = 100
        builder.push(q)
        for seconds in range(20, 301, 20):
            q = quote(c, start + timedelta(seconds=seconds), sequence=seconds)
            q["volume_total"] = 100 + seconds
            bar = builder.push(q)
        self.assertFalse(bar["flags"])
        self.assertEqual(bar["volume"], 380)
        q = quote(c, start + timedelta(seconds=700), sequence=999)
        q["volume_total"] = 1000
        broken = builder.push(q)
        self.assertIn("FEED_GAP", broken["flags"])

    def test_last_bar_flushes_without_a_tick_after_market_close(self):
        c, config = contract(), configuration()
        builder = BarBuilder(c, config)
        end = NOW.replace(hour=6, minute=25)
        start = end - BAR
        builder.last_volume = 100
        for seconds in range(0, 300, 20):
            q = quote(c, start + timedelta(seconds=seconds), sequence=seconds + 1)
            builder.push(q)
        bar = builder.flush(end + timedelta(seconds=2))
        self.assertIsNotNone(bar)
        self.assertFalse(bar["flags"])
        self.assertIsNone(builder.flush(end + timedelta(seconds=3)))

    def test_future_target_cannot_change_features(self):
        store = Store(":memory:")
        try:
            c, config = contract(), configuration()
            store.add_contract(c)
            add_session(store, c, config["segments"]["NSE"], NOW.date().isoformat())
            bars = store.bars(c.key)
            first = features(c, bars[:24], config)
            bars[24]["ohlc"] = [1, 999999999, 1, 999999999]
            self.assertEqual(features(c, bars[:24], config), first)
            bars[1]["timestamp"] = bars[0]["timestamp"]
            with self.assertRaises(ValueError):
                features(c, bars[:24], config)
        finally:
            store.close()

    def test_global_day_splits_and_dataset_hash_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            store, config = Store(":memory:"), configuration(100)
            try:
                for segment in ("NSE", "BSE"):
                    c = contract(segment)
                    store.add_contract(c)
                    for day in config["segments"][segment]["sessions"]:
                        add_session(store, c, config["segments"][segment], day)
                target = Path(directory) / "dataset"
                manifest = prepare(store, config, target)
                self.assertEqual(manifest["coverage"]["NSE"], {"train": 60, "validation": 20, "test": 20})
                _, rows = read_dataset(target)
                self.assertLess(
                    max(r["target_end"] for r in rows["train"]), min(r["bar"] for r in rows["validation"])
                )
                self.assertLess(
                    max(r["target_end"] for r in rows["validation"]), min(r["bar"] for r in rows["test"])
                )
                (target / "test.jsonl").write_text("tampered", encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_dataset(target)
            finally:
                store.close()

    def test_shortlist_uses_only_previous_session_and_freezes(self):
        store, config = Store(":memory:"), configuration(2)
        try:
            c = contract()
            store.add_contract(c)
            previous = (NOW - timedelta(days=1)).date().isoformat()
            add_session(store, c, config["segments"]["NSE"], previous)
            chosen = store.shortlist(config, NOW.date().isoformat(), [c.key])
            self.assertEqual(chosen, [c.key])
            self.assertEqual(store.shortlist(config, NOW.date().isoformat(), []), chosen)
        finally:
            store.close()

    def test_shortlist_does_not_substitute_an_older_complete_session(self):
        store, config = Store(":memory:"), configuration(3)
        try:
            c = contract()
            store.add_contract(c)
            days = sorted(config["segments"]["NSE"]["sessions"])
            add_session(store, c, config["segments"]["NSE"], days[0])
            self.assertEqual(store.shortlist(config, days[2], [c.key]), [])
            add_session(store, c, config["segments"]["NSE"], days[1])
            self.assertEqual(store.shortlist(config, days[2], [c.key]), [c.key])
        finally:
            store.close()

    def test_index_context_requires_the_exact_completed_window(self):
        store, config = Store(":memory:"), configuration()
        try:
            c = contract()
            index = replace(c, kind="INDEX", symbol="Nifty 50", token="99926000", tick=0)
            config["contexts"] = {"NSE": {"exchange": "NSE", "token": index.token}}
            for instrument in (c, index):
                store.add_contract(instrument)
            add_session(store, c, config["segments"]["NSE"], NOW.date().isoformat())
            bars = store.bars(c.key)[:24]
            with self.assertRaises(ContextPending):
                input_features(store, c, bars, config)
            add_session(store, index, config["segments"]["NSE"], NOW.date().isoformat(), base=30000)
            value = input_features(store, c, bars, config)
            self.assertEqual(value["index_context"]["instrument"], index.key)
            with store.db:
                store.db.execute(
                    "DELETE FROM bars WHERE instrument=? AND timestamp=?", (index.key, bars[-1]["timestamp"])
                )
            with self.assertRaises(ContextPending):
                input_features(store, c, bars, config)
        finally:
            store.close()

    def test_candles_without_recorded_depth_cannot_pass_replay(self):
        store, config = Store(":memory:"), configuration()
        try:
            c = contract()
            store.add_contract(c)
            row = {"instrument": c.key, "bar": (NOW - BAR).isoformat(), "target_end": (NOW + BAR).isoformat()}
            result = replay(store, config, [row], ["UP"], "model")
            self.assertFalse(result["complete"])
            self.assertEqual(result["coverage"], 0)
            self.assertEqual(result["fills"], 0)
        finally:
            store.close()

    def test_recorded_replay_reconciles_profit_and_never_uses_future_entry_quotes(self):
        store, config = Store(":memory:"), configuration()
        try:
            c = contract()
            store.add_contract(c)
            store.add_quote(quote(c))
            store.add_quote(quote(c, NOW + BAR, 2, 11000, 11010))
            row = {"instrument": c.key, "bar": (NOW - BAR).isoformat(), "target_end": (NOW + BAR).isoformat()}
            result = replay(store, config, [row], ["UP"], "model")
            self.assertTrue(result["complete"])
            self.assertEqual(result["fills"], 2)
            self.assertGreater(result["net_pnl_paise"], 0)
            row["bar"] = (NOW - BAR - timedelta(seconds=1)).isoformat()
            result = replay(store, config, [row], ["UP"], "model")
            self.assertFalse(result["complete"])
            self.assertEqual(result["fills"], 0)
        finally:
            store.close()

    def test_corporate_action_and_intraday_gap_are_flagged(self):
        store, config = Store(":memory:"), configuration(2)
        try:
            c = contract()
            store.add_contract(c)
            days = sorted(config["segments"]["NSE"]["sessions"])
            add_session(store, c, config["segments"]["NSE"], days[0])
            add_session(store, c, config["segments"]["NSE"], days[1], base=5000)
            first = [b for b in store.bars(c.key) if b["timestamp"].startswith(days[1])][0]
            self.assertIn("CORPORATE_ACTION_REVIEW", first["flags"])
        finally:
            store.close()


class OrchestrationTests(unittest.TestCase):
    def test_inference_queue_time_is_reported_and_can_stop_entries(self):
        store, config = Store(":memory:"), configuration()
        try:
            c = contract()
            store.add_contract(c)
            frame = {
                "event": "quote",
                "exchange": c.exchange,
                "token": c.token,
                "timestamp": NOW.isoformat(),
                "received_at": NOW.isoformat(),
                "sequence": 1,
                "bid_units": 10000,
                "ask_units": 10010,
                "ltp_units": 10005,
                "bid_qty": 100,
                "ask_qty": 100,
                "volume_total": 100,
            }
            bar = {
                "instrument": c.key,
                "timestamp": (NOW - BAR).isoformat(),
                "ohlc": [10000, 10010, 10000, 10005],
                "volume": 100,
                "origin": "RECORDED",
                "flags": [],
            }
            events = Mock()
            events.live = Event()
            events.live.set()
            events.blocked = frozenset()
            events.get.side_effect = [frame, None]

            def source(*args):
                yield events

            elapsed = 0

            def submit(function):
                future = Mock()
                future.done.return_value = True

                def result():
                    nonlocal elapsed
                    elapsed = 12
                    return function()

                future.result.side_effect = result
                return future

            executor, builder = Mock(), Mock()
            executor.submit.side_effect = submit
            builder.push.return_value = bar
            builder.flush.return_value = None
            with (
                patch("ai_trader.stream_recordings", source),
                patch("ai_trader.ThreadPoolExecutor", return_value=executor),
                patch("ai_trader.BarBuilder", return_value=builder),
                patch("ai_trader.input_features", return_value={}),
                patch("ai_trader.now", return_value=NOW),
                patch("ai_trader.time.monotonic", side_effect=lambda: elapsed),
                redirect_stdout(io.StringIO()),
            ):
                result = run_loop(
                    store,
                    config,
                    Mock(),
                    [c.key],
                    60,
                    {"id": "model"},
                    Mock(predict=Mock(return_value="FLAT")),
                    "paper",
                )
            self.assertEqual(result["model_failures"], 1)
            self.assertEqual(
                store.meta("entry-halt:model:" + NOW.date().isoformat())["reason"], "MODEL_FAILURE"
            )
            self.assertEqual(PaperExecution(store, config).report(NOW)["latency_p95_seconds"], 12)
        finally:
            store.close()

    def test_exit_only_ignores_missing_index_context_and_idle_equity_is_recorded_once(self):
        store, config = Store(":memory:"), configuration()
        try:
            c = contract()
            store.add_contract(c)
            store.add_quote(quote(c))
            engine = PaperExecution(store, config)
            self.assertEqual(
                engine.submit("entry", c.key, "BUY", 10, NOW, "model", (NOW - BAR).isoformat())["status"],
                "FILLED",
            )
            store.add_quote(quote(c, NOW + BAR, 2, 11000, 11010))
            config["contexts"] = {"NSE": {"exchange": "NSE", "token": "99926000"}}
            events = Mock()
            events.live = Event()
            events.live.set()
            events.blocked = frozenset()
            events.get.side_effect = [Empty(), Empty(), None]

            def source(*args):
                yield events

            tick = NOW + BAR

            def clock():
                nonlocal tick
                tick += timedelta(milliseconds=1)
                return tick

            with (
                patch("ai_trader.stream_recordings", source),
                patch("ai_trader.now", side_effect=clock),
                redirect_stdout(io.StringIO()),
            ):
                result = run_loop(
                    store, config, Mock(), [c.key], 60, {"id": "EXIT_ONLY"}, mode="paper", entry_eligible=[]
                )
            self.assertFalse(result["positions"])
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 2)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM equity").fetchone()[0], 1)
        finally:
            store.close()

    def exercise(self, disconnect=False, invalid=False, drop_after_size=False):
        store, config = Store(":memory:"), configuration()
        self.addCleanup(store.close)
        c = contract()
        store.add_contract(c)
        bar = {
            "instrument": c.key,
            "timestamp": (NOW - BAR).isoformat(),
            "ohlc": [10000, 10010, 9990, 10000],
            "volume": 100,
            "origin": "RECORDED",
            "flags": [],
        }
        entered, released, done = Event(), Event(), Event()
        live = Event()
        live.set()

        class Predictor:
            def predict(self, value):
                entered.set()
                if not released.wait(2):
                    raise ValueError("fixture stalled")
                done.set()
                return "INVALID" if invalid else "UP"

        def event(sequence):
            return {
                "event": "quote",
                "exchange": c.exchange,
                "token": c.token,
                "timestamp": NOW.isoformat(),
                "received_at": NOW.isoformat(),
                "sequence": sequence,
                "bid_units": 10000,
                "ask_units": 10010,
                "ltp_units": 10005,
                "bid_qty": 1000000,
                "ask_qty": 1000000,
                "volume_total": sequence * 100,
            }

        case = self

        class Queue:
            def __init__(self):
                self.n = 0
                self.live = live
                self.blocked = frozenset()

            def get(self, timeout):
                self.n += 1
                if self.n == 1:
                    return event(1)
                if self.n == 2:
                    case.assertTrue(entered.wait(2))
                    if disconnect:
                        self.live.clear()
                        return {"event": "status", "status": "UNAVAILABLE"}
                    return event(2)
                if self.n == 3:
                    case.assertEqual(
                        store.db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 1 if disconnect else 2
                    )
                    released.set()
                    case.assertTrue(done.wait(2))
                if self.n < 8:
                    time.sleep(0.001)
                    raise Empty
                return None

        def source(*args):
            yield Queue()

        builder = Mock()
        builder.push.side_effect = [bar, None]
        builder.flush.return_value = None
        original_size = PaperExecution.size

        def sized(engine, instrument, at):
            quantity = original_size(engine, instrument, at)
            if drop_after_size:
                live.clear()
            return quantity

        with (
            patch("ai_trader.stream_recordings", source),
            patch("ai_trader.BarBuilder", return_value=builder),
            patch("ai_trader.input_features", return_value={"bars": []}),
            patch("ai_trader.now", return_value=NOW),
            patch.object(PaperExecution, "size", sized),
            redirect_stdout(io.StringIO()),
        ):
            run_loop(store, config, Mock(), [c.key], 60, {"id": "fixture"}, Predictor(), "paper")
        return store

    def test_feed_is_recorded_while_inference_is_pending(self):
        store = self.exercise()
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 1)

    def test_disconnect_during_inference_prevents_late_entry(self):
        store = self.exercise(True)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)

    def test_feed_loss_during_sizing_prevents_entry(self):
        store = self.exercise(drop_after_size=True)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)

    def test_invalid_output_durably_halts_entries_for_the_model_session(self):
        store = self.exercise(invalid=True)
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0], 0)
        self.assertEqual(
            store.meta("entry-halt:fixture:" + NOW.date().isoformat())["reason"], "MODEL_FAILURE"
        )


class GateTests(unittest.TestCase):
    def test_concurrent_progress_cannot_overwrite_a_saved_holdout(self):
        store, config = Store(":memory:"), configuration()
        try:
            row = {"features": {}}
            manifest = {"id": "dataset", "calendars": {}}
            artifact = {"id": "model", "dataset": "dataset", "config_hash": config_id(config)}
            key = "evaluation:dataset:validation:model:lock"
            baseline, predictor = Mock(), Mock()
            baseline.model_digest.return_value = "b" * 64
            baseline.predict.return_value = "FLAT"

            def concurrent(value):
                saved = store.meta(key)
                saved["another_writer"] = True
                store.set_meta(key, saved)
                return "UP"

            predictor.predict.side_effect = concurrent
            with (
                patch("ai_paper.evaluation.read_dataset", return_value=(manifest, {"validation": [row]})),
                self.assertRaises(ValueError),
            ):
                evaluate(store, config, "fixture", artifact, predictor, baseline)
            self.assertTrue(store.meta(key)["another_writer"])
            self.assertEqual(store.meta(key)["predictions"], [])
        finally:
            store.close()

    def test_holdout_resumes_same_model_and_refuses_a_different_model(self):
        store, config = Store(":memory:"), configuration()
        try:
            rows = [
                {
                    "instrument": "fixture",
                    "segment": "NSE",
                    "bar": NOW.isoformat(),
                    "day": "2026-01-05",
                    "target_end": (NOW + BAR).isoformat(),
                    "features": {},
                    "label": label,
                    "momentum": "DOWN",
                }
                for label in ("UP", "DOWN", "FLAT")
            ]
            manifest = {
                "id": "dataset",
                "coverage": {"NSE": {"train": 60, "validation": 20, "test": 20}},
                "calendars": {},
            }
            values = {"train": rows, "validation": rows, "test": rows}
            artifact = {
                "id": "model",
                "dataset": "dataset",
                "config_hash": config_id(config),
                "ollama_digest": "a" * 64,
            }

            def replayed(*args):
                return {"complete": True, "net_pnl_paise": 1 if args[4] == "momentum" else 2}

            predictor, baseline = Mock(), Mock()
            baseline.model_digest.return_value = "b" * 64
            baseline.predict.return_value = "FLAT"
            with (
                patch("ai_paper.evaluation.read_dataset", return_value=(manifest, values)),
                patch("ai_paper.evaluation.replay", side_effect=replayed),
            ):
                predictor.predict.side_effect = ["UP", "DOWN", "FLAT"]
                self.assertTrue(
                    evaluate(store, config, "fixture", artifact, predictor, baseline)["segments"]["NSE"][
                        "passed"
                    ]
                )
                predictor.predict.side_effect = ["UP", ValueError("interruption")]
                with self.assertRaises(ValueError):
                    evaluate(store, config, "fixture", artifact, predictor, baseline, "test")
                baseline.model_digest.return_value = "c" * 64
                with self.assertRaises(ValueError):
                    evaluate(store, config, "fixture", artifact, predictor, baseline, "test")
                baseline.model_digest.return_value = "b" * 64
                predictor.predict.side_effect = ["DOWN", "FLAT"]
                result = evaluate(store, config, "fixture", artifact, predictor, baseline, "test")
                self.assertTrue(result["segments"]["NSE"]["passed"])
                with self.assertRaises(ValueError):
                    evaluate(
                        store, config, "fixture", {**artifact, "id": "different"}, predictor, baseline, "test"
                    )
        finally:
            store.close()

    def test_refreshing_verification_dates_and_adding_future_calendar_preserves_economic_identity(self):
        config = configuration()
        original = config_id(config)
        config["segments"]["NSE"]["verified_on"] = "2026-01-06"
        config["segments"]["NSE"]["live_verified_on"] = "2026-01-06"
        config["segments"]["NSE"]["sessions"]["2026-01-06"] = {
            "open": "2026-01-06T09:15:00+05:30",
            "close": "2026-01-06T15:30:00+05:30",
        }
        self.assertEqual(config_id(config), original)
        config["segments"]["NSE"]["costs"]["buy_bps"] = 2
        self.assertNotEqual(config_id(config), original)


class MultiSessionTests(unittest.TestCase):
    def test_shadow_credit_requires_the_frozen_cohort_and_no_feed_failures(self):
        store, config = Store(":memory:"), configuration()
        try:
            c = contract()
            store.add_contract(c)
            day = NOW.date().isoformat()
            spec = config["segments"]["NSE"]["sessions"][day]
            opened, closed = datetime.fromisoformat(spec["open"]), datetime.fromisoformat(spec["close"])
            store.set_meta(
                "shadow-cohort:model:" + day,
                {"day": day, "started_at": opened.isoformat(), "segments": {"NSE": [c.key]}},
            )
            add_session(store, c, config["segments"]["NSE"], day)
            for sequence, seconds in enumerate(range(0, int((closed - opened).total_seconds()), 20), 1):
                store.add_quote(quote(c, opened + timedelta(seconds=seconds), sequence))
            with store.db:
                for bar in store.bars(c.key)[23:-1]:
                    payload = {"request": {"bar": bar["timestamp"], "mode": "shadow"}, "forecast": "FLAT"}
                    store.db.execute(
                        "INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                        (
                            digest(payload),
                            c.key,
                            bar["timestamp"],
                            bar["timestamp"],
                            "model",
                            canonical(payload),
                        ),
                    )
            finish_shadow(store, config, "model")
            self.assertEqual(
                tuple(store.db.execute("SELECT complete,failures FROM shadow").fetchone()), (1, 0)
            )
            store.health(c.key, "UNAVAILABLE", opened + BAR)
            finish_shadow(store, config, "model")
            self.assertEqual(
                tuple(store.db.execute("SELECT complete,failures FROM shadow").fetchone()), (1, 1)
            )
        finally:
            store.close()

    def setup_stream(self):
        from tests.test_smartapi_stream import Clock, NOW as clock_now
        import smartapi_stream as stream

        clock = Clock()
        client = SmartAPIData(
            Settings("fixture", "FIXTURE", "192.0.2.1", "203.0.113.1", "00:11:22:33:44:55"),
            clock=lambda: clock.instant,
        )
        client._jwt, client._feed_token = "fixture.jwt.token", "fixture"
        client._expires = clock_now + timedelta(hours=2)
        self.addCleanup(client.close)
        for name, value in (("monotonic", lambda: clock.elapsed), ("sleep", clock.sleep)):
            p = patch.object(stream.time, name, value)
            p.start()
            self.addCleanup(p.stop)
        c = replace(contract(), symbol="TEST-EQ", verified_on="2026-01-01")
        return client, clock, c

    def test_connection_waits_through_preopen_without_consuming_reconnect_budget(self):
        from tests.test_smartapi_stream import Socket, snap_quote

        client, clock, c = self.setup_stream()
        opened = clock.instant + timedelta(seconds=40)
        socket = Socket(
            clock,
            "pong",
            snap_quote(at=clock.instant - timedelta(days=1)),
            (20, "pong"),
            "pong",
            (20, "pong"),
            "pong",
            snap_quote(at=opened),
        )
        with patch("smartapi_stream._open_socket", return_value=socket) as connect:
            events = list(
                client.stream(
                    [c],
                    max_quotes=1,
                    duration=60,
                    windows={(1, c.token): (opened, opened + timedelta(hours=1))},
                )
            )
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(len([e for e in events if e["event"] == "quote"]), 1)
        self.assertFalse(any(e["status"] == "RECONNECTING" for e in events))

    def test_one_unavailable_book_does_not_stop_other_segments(self):
        from tests.test_smartapi_stream import Socket, snap_quote

        client, clock, c = self.setup_stream()
        other = replace(c, exchange="BSE", token="654321")
        empty = bytearray(snap_quote(at=clock.instant))
        struct.pack_into("<B25s", empty, 1, 3, other.token.encode())
        for index in range(10):
            struct.pack_into("<hqqh", empty, 147 + 20 * index, int(index < 5), 0, 0, 0)
        socket = Socket(clock, "pong", bytes(empty), snap_quote(at=clock.instant))
        windows = {
            (c.exchange_type, c.token): (clock.instant, clock.instant + timedelta(hours=1))
            for c in (c, other)
        }
        with patch("smartapi_stream._open_socket", return_value=socket):
            events = list(client.stream([c, other], max_quotes=1, duration=60, windows=windows))
        quotes = [e for e in events if e["event"] == "quote"]
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["exchange"], "NSE")
        self.assertIn("bid_units", quotes[0])
        self.assertNotIn("bid_paise", quotes[0])
        self.assertTrue(any(e.get("exchange") == "BSE" and e["status"] == "UNAVAILABLE" for e in events))

    def test_two_complete_subsequent_shadow_sessions_and_fresh_live_check_are_required(self):
        store, config = Store(":memory:"), configuration()
        try:
            c = contract()
            store.add_contract(c)
            model = {"id": "model", "ollama_digest": "a" * 64}
            test_end = (NOW - timedelta(days=3)).isoformat()
            store.set_meta(
                "evaluation:dataset:test",
                {
                    "model_id": "model",
                    "model_digest": "a" * 64,
                    "config_hash": config_id(config),
                    "calendars": {},
                    "test_end": test_end,
                    "segments": {s: {"passed": s == "NSE"} for s in SEGMENTS},
                },
            )
            self.assertEqual(readiness(store, config, model, "dataset", NOW)["NSE"]["status"], "SHADOW_READY")
            config["segments"]["NSE"]["live_verified_on"] = NOW.date().isoformat()
            report = store.meta("evaluation:dataset:test")
            report["config_hash"] = config_id(config)
            store.set_meta("evaluation:dataset:test", report)
            with store.db:
                for offset in (1, 2):
                    store.db.execute(
                        "INSERT INTO shadow VALUES (?,?,?,?,?)",
                        ("model", "NSE", (NOW - timedelta(days=offset)).date().isoformat(), 1, 0),
                    )
            self.assertEqual(
                readiness(store, config, model, "dataset", NOW)["NSE"]["status"], "PAPER_ENABLED"
            )
            self.assertEqual(
                readiness(store, config, model, "dataset", NOW + timedelta(days=8))["NSE"]["status"],
                "SHADOW_READY",
            )
        finally:
            store.close()


class LocalModelTests(unittest.TestCase):
    def test_artifact_reports_are_available_without_an_inference_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "adapter").mkdir()
            weights = root / "adapter" / "adapter_model.safetensors"
            weights.write_bytes(b"fictional-checksum-fixture")
            gguf = root / "model.f16.gguf"
            gguf.write_bytes(b"fictional-checksum-fixture")
            value = {"benchmark_only": False, "adapter_hashes": {weights.name: file_hash(weights)}}
            value.update(
                id=digest(value),
                gguf_hash=file_hash(gguf),
                ollama_name="fixture:local",
                ollama_digest="a" * 64,
            )
            (root / "model.json").write_text(canonical(value), encoding="utf-8")
            with patch("ai_paper.evaluation.Ollama") as runtime:
                artifact, predictor = model_artifact(root, check_runtime=False)
            self.assertEqual(artifact["id"], value["id"])
            self.assertIsNone(predictor)
            runtime.assert_not_called()

    def test_cpu_auto_and_explicit_cuda(self):
        torch = Mock()
        torch.cuda.is_available.return_value = False
        self.assertEqual(resolve_device("auto", torch), "cpu")
        with self.assertRaises(ValueError):
            resolve_device("cuda", torch)
        torch.cuda.is_available.return_value = True
        self.assertEqual(resolve_device("auto", torch), "cuda")
        self.assertEqual(resolve_device("cpu", torch), "cpu")

    def test_strict_direction_and_no_tools_or_cloud_fallback(self):
        local = Ollama("angel-paper:test")
        with (
            patch.object(local, "model_digest", return_value="f" * 64),
            patch.object(
                local,
                "_call",
                return_value={
                    "done": True,
                    "done_reason": "stop",
                    "message": {"content": '{"direction":"UP"}'},
                },
            ) as call,
        ):
            self.assertEqual(local.predict({"bars": []}), "UP")
            payload = call.call_args.args[1]
            self.assertFalse(payload["think"])
            self.assertNotIn("tools", payload)
        for content in ('{"direction":"BUY"}', '{"direction":"UP","quantity":999}', "[]", "not JSON"):
            with (
                patch.object(local, "model_digest", return_value="f" * 64),
                patch.object(
                    local,
                    "_call",
                    return_value={"done": True, "done_reason": "stop", "message": {"content": content}},
                ),
            ):
                with self.assertRaises(ValueError):
                    local.predict({})

    def test_cloud_or_digestless_model_is_rejected(self):
        local = Ollama("local:test")
        with patch.object(local, "_call", return_value={"remote_host": "https://cloud.example"}):
            with self.assertRaises(ValueError):
                local.model_digest()

    def test_macro_f1_and_all_segments_report_blocked(self):
        self.assertEqual(macro_f1(["UP", "DOWN", "FLAT"], ["UP", "DOWN", "FLAT"]), 1)
        store = Store(":memory:")
        try:
            states = readiness(store, configuration())
            self.assertEqual(set(states), set(SEGMENTS))
            self.assertTrue(all(s["status"] == "COLLECTING" for s in states.values()))
        finally:
            store.close()

    def test_cli_help_and_config_failure_do_not_authenticate(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["train", "--help"])
        self.assertEqual(caught.exception.code, 0)
        with patch("ai_trader._local_login") as login, redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["--config", "missing-fixture.json", "run", "--model-dir", "missing-model"]), 1
            )
            login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
