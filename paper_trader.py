"""Offline, long-only paper trading. Money is stored as integer paise."""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
import sys

MAX_AMOUNT = 9_000_000_000_000_000


def integer(value, name, minimum=0, maximum=MAX_AMOUNT):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def amount(text):
    """Convert an INR string to paise without silently rounding it."""
    try:
        value = Decimal(text)
        if not value.is_finite() or not 0 <= value <= MAX_AMOUNT // 100:
            raise ValueError("amount is outside the supported range")
        rounded = value.quantize(Decimal("0.01"))
        if rounded != value:
            raise ValueError("amount must have at most two decimal places")
        return int(rounded * 100)
    except InvalidOperation as exc:
        raise ValueError("invalid INR amount") from exc


def symbol_name(value):
    if not isinstance(value, str):
        raise ValueError("symbol must be a string")
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,79}", value):
        raise ValueError("invalid symbol; use up to 80 letters, numbers, dots, dashes or underscores")
    return value


def utc(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc)


class PaperAccount:
    def __init__(self, path, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("unsupported database version; keep a backup before migrating")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                initial_cash INTEGER NOT NULL, cash INTEGER NOT NULL CHECK (cash >= 0),
                fee INTEGER NOT NULL, slippage_bps INTEGER NOT NULL, max_age INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quotes (
                symbol TEXT PRIMARY KEY, bid INTEGER NOT NULL, ask INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL CHECK (quantity >= 0),
                cost INTEGER NOT NULL CHECK (cost >= 0), realized INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fills (
                order_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, symbol TEXT NOT NULL,
                side TEXT NOT NULL, quantity INTEGER NOT NULL, price_paise INTEGER NOT NULL,
                fee_paise INTEGER NOT NULL, realized_paise INTEGER NOT NULL,
                cash_after_paise INTEGER NOT NULL
            );
            PRAGMA user_version = 1;
        """)

    def close(self):
        self.db.close()

    def _account(self):
        row = self.db.execute("SELECT * FROM account WHERE id = 1").fetchone()
        if row is None:
            raise ValueError("initialise a paper account first with the init command")
        return row

    def initialize(self, cash, fee=0, slippage_bps=0, max_age=30):
        integer(cash, "cash", 1)
        integer(fee, "fee")
        integer(slippage_bps, "slippage_bps", maximum=1000)
        integer(max_age, "max_age", 1, 3600)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM account").fetchone():
                raise ValueError("account already exists; choose a new database to start again")
            self.db.execute("INSERT INTO account VALUES (1, ?, ?, ?, ?, ?)",
                            (cash, cash, fee, slippage_bps, max_age))
        return self.status()

    @staticmethod
    def _fresh(timestamp, now, max_age):
        age = (utc(now) - utc(timestamp)).total_seconds()
        if not 0 <= age <= max_age:
            raise ValueError("quote is stale or in the future; supply a fresh quote")

    def quote(self, symbol, bid, ask, timestamp=None):
        symbol = symbol_name(symbol)
        integer(bid, "bid", 1)
        integer(ask, "ask", bid)
        now = utc(self.clock())
        timestamp = utc(timestamp) if timestamp is not None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            account = self._account()
            self._fresh(timestamp, now, account["max_age"])
            previous = self.db.execute("SELECT * FROM quotes WHERE symbol = ?", (symbol,)).fetchone()
            if previous and timestamp <= datetime.fromisoformat(previous["timestamp"]):
                if (timestamp.isoformat(), bid, ask) == (previous["timestamp"], previous["bid"], previous["ask"]):
                    return {"symbol": symbol, "bid_paise": bid, "ask_paise": ask, "timestamp": timestamp.isoformat()}
                raise ValueError("quote timestamp must advance; older or conflicting quotes are rejected")
            self.db.execute("INSERT OR REPLACE INTO quotes VALUES (?, ?, ?, ?)",
                            (symbol, bid, ask, timestamp.isoformat()))
        return {"symbol": symbol, "bid_paise": bid, "ask_paise": ask, "timestamp": timestamp.isoformat()}

    def trade(self, order_id, symbol, side, quantity):
        symbol = symbol_name(symbol)
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", order_id):
            raise ValueError("order ID must contain 1–128 letters, numbers, dots, colons, dashes or underscores")
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        integer(quantity, "quantity", 1, 1_000_000_000)
        with self.db:
            # Reserve the writer before reading balances or checking duplicate IDs.
            self.db.execute("BEGIN IMMEDIATE")
            account = self._account()
            existing = self.db.execute("SELECT * FROM fills WHERE order_id = ?", (order_id,)).fetchone()
            if existing:
                if (existing["symbol"], existing["side"], existing["quantity"]) != (symbol, side, quantity):
                    raise ValueError("order ID already belongs to a different request")
                return dict(existing)
            quote = self.db.execute("SELECT * FROM quotes WHERE symbol = ?", (symbol,)).fetchone()
            if quote is None:
                raise ValueError("no quote for this symbol")
            now = utc(self.clock())
            self._fresh(datetime.fromisoformat(quote["timestamp"]), now, account["max_age"])
            position = self.db.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,)).fetchone()
            held, cost, realized = (position["quantity"], position["cost"], position["realized"]) if position else (0, 0, 0)
            fee, bps = account["fee"], account["slippage_bps"]
            if side == "BUY":
                price = (quote["ask"] * (10_000 + bps) + 9999) // 10_000
                debit = price * quantity + fee
                if debit > account["cash"]:
                    raise ValueError("insufficient virtual cash, including fees and slippage")
                cash = account["cash"] - debit
                held, cost, profit = held + quantity, cost + debit, 0
            else:
                if quantity > held:
                    raise ValueError("cannot sell more than the paper position; short selling is unsupported")
                price = quote["bid"] * (10_000 - bps) // 10_000
                if price <= 0:
                    raise ValueError("slippage would make the fill price zero")
                credit = price * quantity - fee
                if account["cash"] + credit < 0:
                    raise ValueError("insufficient virtual cash to cover the exit fee")
                allocated = cost * quantity // held
                profit = credit - allocated
                cash = account["cash"] + credit
                held, cost = held - quantity, cost - allocated
            integer(cash, "resulting cash")
            integer(cost, "resulting position cost")
            integer(held, "resulting position quantity", maximum=1_000_000_000)
            integer(realized + profit, "resulting realized P&L", -MAX_AMOUNT)
            self.db.execute("UPDATE account SET cash = ? WHERE id = 1", (cash,))
            self.db.execute("INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?)",
                            (symbol, held, cost, realized + profit))
            self.db.execute("INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (order_id, now.isoformat(), symbol, side, quantity, price, fee, profit, cash))
            return dict(self.db.execute("SELECT * FROM fills WHERE order_id = ?", (order_id,)).fetchone())

    def trades(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM fills ORDER BY rowid")]

    def status(self):
        with self.db:
            self.db.execute("BEGIN")
            account = self._account()
            now = utc(self.clock())
            positions, value, all_fresh = [], 0, True
            for row in self.db.execute("SELECT * FROM positions WHERE quantity > 0 ORDER BY symbol"):
                quote = self.db.execute("SELECT * FROM quotes WHERE symbol = ?", (row["symbol"],)).fetchone()
                try:
                    if quote is None:
                        raise ValueError("missing quote")
                    self._fresh(datetime.fromisoformat(quote["timestamp"]), now, account["max_age"])
                    mark = integer(quote["bid"] * row["quantity"], "marked value")
                    value += mark
                except ValueError:
                    mark, all_fresh = None, False
                positions.append({"symbol": row["symbol"], "quantity": row["quantity"],
                                  "cost_paise": row["cost"], "bid_value_paise": mark,
                                  "unrealized_before_exit_costs_paise": None if mark is None else mark - row["cost"],
                                  "mark_status": "FRESH" if mark is not None else "UNAVAILABLE"})
            realized = self.db.execute("SELECT COALESCE(SUM(realized), 0) FROM positions").fetchone()[0]
            equity = account["cash"] + value if all_fresh else None
            return {"mode": "OFFLINE_PAPER", "initial_cash_paise": account["initial_cash"],
                    "cash_paise": account["cash"], "realized_paise": realized,
                    "equity_before_exit_costs_paise": equity,
                    "total_pnl_before_exit_costs_paise": None if equity is None else equity - account["initial_cash"],
                    "positions": positions}


def readable(value):
    """Render stored paise as exact INR strings for the CLI."""
    if isinstance(value, list):
        return [readable(item) for item in value]
    if isinstance(value, dict):
        return {key.removesuffix("_paise") + "_inr" if key.endswith("_paise") else key:
                (None if item is None else format(Decimal(item) / 100, ".2f"))
                if key.endswith("_paise") else readable(item) for key, item in value.items()}
    return value


def demo():
    """A deterministic synthetic round trip; never opens a file or network connection."""
    instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
    account = PaperAccount(":memory:", clock=lambda: instant)
    try:
        account.initialize(amount("100000"), fee=amount("1"))
        account.quote("DEMO-EQ", amount("99"), amount("101"))
        account.trade("paper-buy-1", "DEMO-EQ", "BUY", 10)
        instant = instant.replace(second=1)
        account.quote("DEMO-EQ", amount("109"), amount("111"))
        account.trade("paper-sell-1", "DEMO-EQ", "SELL", 10)
        return {"data": "FICTIONAL", "fills": account.trades(), "account": account.status()}
    finally:
        account.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/paper.sqlite3", help="local paper journal (default: data/paper.sqlite3)")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create virtual cash and immutable fill settings")
    init.add_argument("--cash", default="100000")
    init.add_argument("--fee", default="0", help="illustrative flat INR charge per fill; not an Angel tariff")
    init.add_argument("--slippage-bps", type=int, default=0)
    init.add_argument("--max-quote-age", type=int, default=30)
    quote = commands.add_parser("quote", help="enter a synthetic bid/ask")
    quote.add_argument("symbol")
    quote.add_argument("--bid", required=True)
    quote.add_argument("--ask", required=True)
    for side in ("buy", "sell"):
        trade = commands.add_parser(side, help=f"{side} using virtual cash only")
        trade.add_argument("symbol")
        trade.add_argument("quantity", type=int)
        trade.add_argument("--id", required=True, help="unique request ID; reuse only when retrying the same order")
    commands.add_parser("status", help="show cash, P&L and fresh bid marks")
    commands.add_parser("trades", help="show the persistent fill journal")
    commands.add_parser("demo", help="run an isolated, fictional round trip in memory")
    args = parser.parse_args(argv)
    account = None
    try:
        if args.command == "demo":
            result = demo()
        else:
            Path(args.db).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            account = PaperAccount(str(Path(args.db).expanduser()))
            if args.command == "init":
                result = account.initialize(amount(args.cash), amount(args.fee), args.slippage_bps, args.max_quote_age)
            elif args.command == "quote":
                result = account.quote(args.symbol, amount(args.bid), amount(args.ask))
            elif args.command in ("buy", "sell"):
                result = account.trade(args.id, args.symbol, args.command.upper(), args.quantity)
            else:
                result = getattr(account, args.command)()
        print(json.dumps(readable(result), indent=2))
        return 0
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if account is not None:
            account.close()


if __name__ == "__main__":
    sys.exit(main())
