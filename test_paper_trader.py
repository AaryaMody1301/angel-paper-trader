"""Accounting, persistence and execution-boundary checks; no network required."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import sqlite3
import unittest

from paper_trader import PaperAccount, amount, demo, readable


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "paper.sqlite3"
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.account = PaperAccount(self.path, clock=lambda: self.now)
        self.addCleanup(lambda: self.account.close())
        self.account.initialize(amount("10000"), fee=amount("1"))
        self.account.quote("DEMO-EQ", amount("99"), amount("101"))

    def test_spread_fees_partial_exit_and_final_cost_allocation(self):
        self.account.trade("buy", "DEMO-EQ", "BUY", 3)
        self.assertEqual(self.account.status()["cash_paise"], amount("9696"))
        self.now += timedelta(seconds=1)
        self.account.quote("DEMO-EQ", amount("109"), amount("111"))
        first = self.account.trade("sell-1", "DEMO-EQ", "SELL", 1)
        self.assertEqual(first["realized_paise"], 667)
        self.account.trade("sell-2", "DEMO-EQ", "SELL", 2)
        state = self.account.status()
        self.assertEqual(state["positions"], [])
        self.assertEqual(state["cash_paise"], amount("10021"))
        self.assertEqual(state["realized_paise"], amount("21"))
        self.assertEqual(state["total_pnl_before_exit_costs_paise"], amount("21"))

    def test_multiple_entries_use_weighted_cost_and_include_entry_fees(self):
        self.account.trade("buy-1", "DEMO-EQ", "BUY", 2)
        self.now += timedelta(seconds=1)
        self.account.quote("DEMO-EQ", amount("109"), amount("111"))
        self.account.trade("buy-2", "DEMO-EQ", "BUY", 1)
        self.account.trade("exit", "DEMO-EQ", "SELL", 3)
        self.assertEqual(self.account.status()["realized_paise"], amount("11"))

    def test_buy_and_sell_slippage_are_adverse_and_round_conservatively(self):
        account = PaperAccount(":memory:", clock=lambda: self.now)
        self.addCleanup(account.close)
        account.initialize(amount("10000"), slippage_bps=10)
        account.quote("DEMO-EQ", amount("99"), amount("101"))
        buy = account.trade("buy", "DEMO-EQ", "BUY", 1)
        sell = account.trade("sell", "DEMO-EQ", "SELL", 1)
        self.assertEqual(buy["price_paise"], 10111)
        self.assertEqual(sell["price_paise"], 9890)
        self.assertEqual(sell["realized_paise"], -221)

    def test_retries_survive_restart_without_repeating_a_fill(self):
        original = self.account.trade("retry-me", "DEMO-EQ", "BUY", 2)
        other = PaperAccount(self.path, clock=lambda: self.now + timedelta(days=1))
        self.addCleanup(other.close)
        self.assertEqual(other.trade("retry-me", "DEMO-EQ", "BUY", 2), original)
        self.assertEqual(len(other.trades()), 1)
        with self.assertRaisesRegex(ValueError, "different request"):
            other.trade("retry-me", "DEMO-EQ", "BUY", 3)

    def test_simultaneous_retries_create_only_one_fill(self):
        def submit(_):
            account = PaperAccount(self.path, clock=lambda: self.now)
            try:
                return account.trade("shared-id", "DEMO-EQ", "BUY", 2)
            finally:
                account.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.account.trades()), 1)
        self.assertEqual(self.account.status()["cash_paise"], amount("9797"))

    def test_invalid_orders_leave_cash_positions_and_journal_unchanged(self):
        before = self.account.status()
        requests = [("cash", "DEMO-EQ", "BUY", 1000),
                    ("short", "DEMO-EQ", "SELL", 1),
                    ("zero", "DEMO-EQ", "BUY", 0),
                    ("negative", "DEMO-EQ", "BUY", -1),
                    ("fraction", "DEMO-EQ", "BUY", 1.5),
                    ("boolean", "DEMO-EQ", "BUY", True),
                    ("missing", "MISSING", "BUY", 1),
                    ("side", "DEMO-EQ", "HOLD", 1)]
        for request in requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.account.trade(*request)
        self.assertEqual(self.account.status(), before)
        self.assertEqual(self.account.trades(), [])

    def test_stale_quotes_stop_fills_and_hide_unavailable_marks(self):
        self.account.trade("entry", "DEMO-EQ", "BUY", 1)
        self.now += timedelta(seconds=31)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.account.trade("stale", "DEMO-EQ", "SELL", 1)
        state = self.account.status()
        self.assertIsNone(state["equity_before_exit_costs_paise"])
        self.assertIsNone(state["positions"][0]["bid_value_paise"])
        self.assertEqual(len(self.account.trades()), 1)
        self.account.quote("DEMO-EQ", amount("99"), amount("101"))
        self.assertIsNotNone(self.account.status()["equity_before_exit_costs_paise"])

    def test_bad_quote_does_not_replace_a_good_quote(self):
        for bid, ask, at in [(0, 100, self.now), (101, 100, self.now),
                             (99, 101, self.now + timedelta(seconds=1)),
                             (99, 101, self.now.replace(tzinfo=None))]:
            with self.subTest(bid=bid, ask=ask, at=at), self.assertRaises(ValueError):
                self.account.quote("DEMO-EQ", bid, ask, at)
        self.now += timedelta(seconds=2)
        self.account.quote("DEMO-EQ", amount("100"), amount("102"))
        with self.assertRaisesRegex(ValueError, "timestamp must advance"):
            self.account.quote("DEMO-EQ", amount("99"), amount("101"), self.now - timedelta(seconds=1))
        self.assertEqual(self.account.trade("entry", "DEMO-EQ", "BUY", 1)["price_paise"], amount("102"))

    def test_cannot_accidentally_reset_an_existing_account(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.account.initialize(amount("20000"))
        self.assertEqual(self.account.status()["initial_cash_paise"], amount("10000"))

    def test_journal_failure_rolls_back_cash_and_position(self):
        before = self.account.status()
        self.account.db.execute("""CREATE TRIGGER fail_fill BEFORE INSERT ON fills
            BEGIN SELECT RAISE(ABORT, 'injected journal failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.account.trade("failure", "DEMO-EQ", "BUY", 1)
        self.assertEqual(self.account.status(), before)
        self.assertEqual(self.account.trades(), [])

    def test_money_input_and_output_are_exact(self):
        for text in ("NaN", "Infinity", "-1", "1.001", "1.000000000000000000000000000001"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                amount(text)
        self.assertEqual(amount("1.2300"), 123)
        self.assertEqual(readable({"loss_paise": -101}), {"loss_inr": "-1.01"})

    def test_demo_has_expected_net_profit(self):
        result = demo()
        self.assertEqual(result["data"], "FICTIONAL")
        self.assertEqual(result["account"]["realized_paise"], amount("78"))
        self.assertEqual(len(result["fills"]), 2)


if __name__ == "__main__":
    unittest.main()
