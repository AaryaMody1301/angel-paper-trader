"""Atomic virtual fills, position limits and durable model decisions."""

import json

from ai_paper.market import (
    AIError,
    BAR,
    DIRECTIONS,
    Contract,
    charges,
    canonical,
    digest,
    direction,
    instant,
    session_window,
)
from paper_trader import integer, settle_fill, utc
from smartapi_data import IST


class PaperExecution:
    def __init__(self, store, config):
        self.store, self.config, self.db = store, config, store.db
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO account VALUES (1,1000000000,1000000000)")

    def mark(self, instrument, now):
        health = self.db.execute("SELECT status,at FROM health WHERE instrument=?", (instrument,)).fetchone()
        quote = self.store.latest(instrument)
        if not health or health[0] != "LIVE" or not quote:
            raise AIError("feed unavailable")
        age = (utc(now) - instant(quote["timestamp"])).total_seconds()
        if not 0 <= age <= 30 or not 0 <= (utc(now) - instant(health[1])).total_seconds() <= 30:
            raise AIError("quote or connection health is stale")
        return quote

    def equity(self, now):
        cash = self.db.execute("SELECT cash FROM account WHERE id=1").fetchone()[0]
        for position in self.db.execute("SELECT * FROM positions"):
            try:
                quote = self.mark(position["instrument"], now)["payload"]
                cash += self.store.contract(position["instrument"]).notional(
                    quote["bid"], position["quantity"], False
                )
            except ValueError:
                return None
        return cash

    def risk(self, now):
        day = utc(now).astimezone(IST).date().isoformat()
        equity = self.equity(now)
        previous = self.store.meta("risk:" + day)
        if equity is None:
            raise AIError("equity unavailable; entries stopped")
        state = dict(previous) if previous else {"opening": equity, "halted": False}
        state["halted"] |= equity * 100 <= state["opening"] * 99
        # Called inside the fill transaction; do not commit a nested transaction.
        if state != previous:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ("risk:" + day, canonical(state)))
        return state

    def _fill_price(self, c, quote, side):
        if c.kind == "INDEX":
            raise AIError("indices cannot be traded")
        bps = integer(self.config["segments"][c.exchange]["costs"]["slippage_bps"], "slippage", 0, 1000)
        if side == "BUY":
            return ((quote["ask"] * (10000 + bps) + 10000 * c.tick - 1) // (10000 * c.tick)) * c.tick
        return (quote["bid"] * (10000 - bps) // (10000 * c.tick)) * c.tick

    def submit(
        self, identity, instrument, side, quantity, now, model, bar, *, mode="paper", forecast=None, latency=0
    ):
        """Rejected requests are durable; fill and decision commit or roll back together."""
        if mode not in ("paper", "shadow") or side not in ("BUY", "SELL"):
            raise AIError("invalid execution mode or side")
        integer(quantity, "quantity", 1, 1_000_000_000)
        key = digest(identity)
        request = {
            "instrument": instrument,
            "side": side,
            "quantity": quantity,
            "model": model,
            "bar": bar,
            "mode": mode,
        }
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            old = self.db.execute("SELECT payload FROM decisions WHERE id=?", (key,)).fetchone()
            if old:
                result = json.loads(old[0])
                if result["request"] != request:
                    raise AIError("decision ID belongs to a different request")
                return result
            result = {"request": request, "forecast": forecast, "latency_seconds": latency}
            try:
                c = self.store.contract(instrument)
                if quantity % c.lot or c.kind == "INDEX":
                    raise AIError("quantity must be whole contract lots; indices cannot be traded")
                if not c.verified(now):
                    raise AIError("contract metadata is not verified")
                quote = self.mark(instrument, now)
                q = quote["payload"]
                day = utc(now).astimezone(IST).date().isoformat()
                opened, closed = session_window(self.config, c, day)
                if not opened <= utc(now) < closed:
                    raise AIError("exchange session is closed")
                if side == "BUY" and (
                    utc(now) + BAR >= closed or not instant(bar) + BAR <= utc(now) < instant(bar) + 2 * BAR
                ):
                    raise AIError("prediction target or entry window has expired")
                held = self.db.execute("SELECT * FROM positions WHERE instrument=?", (instrument,)).fetchone()
                cash, initial = self.db.execute("SELECT cash,initial FROM account WHERE id=1").fetchone()
                price = self._fill_price(c, q, side)
                notional = c.notional(price, quantity, side == "BUY")
                fee = charges(self.config, c, now, notional, side)
                liquidity = q.get("liquidity_ids", {}).get(side, q.get("liquidity_id", quote["id"]))
                consumed = self.db.execute(
                    "SELECT quantity FROM consumed WHERE quote_id=? AND side=?", (liquidity, side)
                ).fetchone()
                if quantity + (consumed[0] if consumed else 0) > q["ask_qty" if side == "BUY" else "bid_qty"]:
                    raise AIError("insufficient unconsumed displayed liquidity")
                if side == "BUY":
                    if self.risk(now)["halted"]:
                        raise AIError("daily loss stop is active")
                    positions = list(self.db.execute("SELECT * FROM positions"))
                    if held or len(positions) >= 3:
                        raise AIError("position already open or three-position limit reached")
                    gross = sum(
                        self.store.contract(p["instrument"]).notional(
                            self.mark(p["instrument"], now)["payload"]["ask"], p["quantity"]
                        )
                        for p in positions
                    )
                    if notional * 4 > initial or (gross + notional) * 4 > initial * 3:
                        raise AIError("position or total notional limit exceeded")
                values = settle_fill(
                    cash,
                    held["quantity"] if held else 0,
                    held["cost"] if held else 0,
                    side,
                    quantity,
                    notional,
                    fee,
                )
                if mode == "shadow":
                    result.update(
                        status="SHADOW", quote_id=quote["id"], notional_paise=notional, fee_paise=fee
                    )
                else:
                    new_cash, remaining, cost, realized = values
                    self.db.execute("UPDATE account SET cash=? WHERE id=1", (new_cash,))
                    if remaining:
                        due = (instant(bar) + 2 * BAR).isoformat() if side == "BUY" else held["due"]
                        self.db.execute(
                            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?)",
                            (instrument, remaining, cost, due, model),
                        )
                    else:
                        self.db.execute("DELETE FROM positions WHERE instrument=?", (instrument,))
                    self.db.execute(
                        "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            key,
                            instrument,
                            side,
                            quantity,
                            notional,
                            fee,
                            realized,
                            quote["id"],
                            utc(now).isoformat(),
                            model,
                        ),
                    )
                    self.db.execute(
                        "INSERT INTO consumed VALUES (?,?,?) ON CONFLICT(quote_id,side) DO UPDATE SET quantity=quantity+excluded.quantity",
                        (liquidity, side, quantity),
                    )
                    result.update(
                        status="FILLED",
                        quote_id=quote["id"],
                        price_units=price,
                        price_scale=c.scale,
                        notional_paise=notional,
                        fee_paise=fee,
                        realized_paise=realized,
                        cash_after_paise=new_cash,
                    )
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                result.update(
                    status="REJECTED",
                    reason=str(exc)
                    if isinstance(exc, ValueError)
                    else "contract configuration is incomplete",
                )
            self.db.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?)",
                (key, instrument, bar, utc(now).isoformat(), model, canonical(result)),
            )
            return result

    def size(self, instrument, now):
        c = self.store.contract(instrument)
        quote = self.mark(instrument, now)["payload"]
        cash, initial = self.db.execute("SELECT cash,initial FROM account WHERE id=1").fetchone()
        positions = list(self.db.execute("SELECT * FROM positions"))
        if len(positions) >= 3 or any(p["instrument"] == instrument for p in positions):
            raise AIError("position count limit or existing position")
        gross = sum(
            self.store.contract(p["instrument"]).notional(
                self.mark(p["instrument"], now)["payload"]["ask"], p["quantity"]
            )
            for p in positions
        )
        available = min(initial // 4, initial * 3 // 4 - gross)
        price = self._fill_price(c, quote, "BUY")
        low, high = (
            0,
            min(
                max(0, available) // c.notional(price, c.lot),
                quote["ask_qty"] // c.lot,
                1_000_000_000 // c.lot,
            ),
        )
        while low < high:
            middle = (low + high + 1) // 2
            total = c.notional(price, middle * c.lot)
            if total <= available and total + charges(self.config, c, now, total, "BUY") <= cash:
                low = middle
            else:
                high = middle - 1
        if low:
            return low * c.lot
        raise AIError("no affordable whole lot")

    def exits(self, now):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                halt = self.risk(now)["halted"]
            except ValueError:
                halt = False
        results = []
        for p in list(self.db.execute("SELECT * FROM positions ORDER BY instrument")):
            c = self.store.contract(p["instrument"])
            day = utc(now).astimezone(IST).date().isoformat()
            try:
                _, closed = session_window(self.config, c, day)
                due = halt or utc(now) >= min(instant(p["due"]), closed - BAR)
            except ValueError:
                due = True
            if due:
                quote = self.store.latest(c.key)
                if quote is None:
                    continue
                identity = ["EXIT", c.key, quote["id"]]
                if self.db.execute("SELECT 1 FROM decisions WHERE id=?", (digest(identity),)).fetchone():
                    continue
                results.append(
                    self.submit(
                        identity,
                        c.key,
                        "SELL",
                        p["quantity"],
                        now,
                        p["model"],
                        p["due"],
                    )
                )
        return results

    def report(self, now):
        fills = [dict(r) for r in self.db.execute("SELECT * FROM fills ORDER BY rowid")]
        decisions = [
            json.loads(r[0]) for r in self.db.execute("SELECT payload FROM decisions ORDER BY rowid")
        ]
        equities = [
            r[0] for r in self.db.execute("SELECT value FROM equity WHERE value IS NOT NULL ORDER BY at")
        ]
        account = dict(self.db.execute("SELECT * FROM account WHERE id=1").fetchone())
        peak, drawdown = account["initial"], 0
        for value in equities:
            peak = max(peak, value)
            drawdown = max(drawdown, peak - value)
        correct = scored = forecasts = 0
        for d in decisions:
            if d.get("forecast") not in DIRECTIONS:
                continue
            forecasts += 1
            at = instant(d["request"]["bar"])
            if utc(now) < at + 2 * BAR:
                continue
            rows = list(
                self.db.execute(
                    "SELECT payload FROM bars WHERE instrument=? AND timestamp IN (?,?) ORDER BY timestamp",
                    (d["request"]["instrument"], at.isoformat(), (at + BAR).isoformat()),
                )
            )
            if len(rows) == 2:
                first, following = (json.loads(r[0]) for r in rows)
                if not first["flags"] and not following["flags"]:
                    scored += 1
                    correct += d["forecast"] == direction(first["ohlc"][3], following["ohlc"][3])
        slippage = 0
        for fill in fills:
            row = self.db.execute("SELECT payload FROM quotes WHERE id=?", (fill["quote_id"],)).fetchone()
            if row is None:
                slippage = None
                break
            q = json.loads(row[0])
            c = (
                Contract(**q["contract_spec"])
                if "contract_spec" in q
                else self.store.contract(fill["instrument"])
            )
            raw = c.notional(
                q["ask" if fill["side"] == "BUY" else "bid"], fill["quantity"], fill["side"] == "BUY"
            )
            slippage += abs(fill["notional"] - raw)
        latencies = sorted(
            d.get("latency_seconds", 0)
            for d in decisions
            if d.get("forecast") in DIRECTIONS or d.get("reason") == "MODEL_FAILURE"
        )
        return {
            "mode": "AI_PAPER",
            "account": {"initial_cash_paise": account["initial"], "cash_paise": account["cash"]},
            "equity_paise": self.equity(now),
            "realized_paise": sum(f["realized"] for f in fills),
            "fees_paise": sum(f["fee"] for f in fills),
            "turnover_paise": sum(f["notional"] for f in fills),
            "slippage_paise": slippage,
            "max_drawdown_paise": drawdown if equities else None,
            "prediction_accuracy": {
                "scored": scored,
                "correct": correct,
                "pending": forecasts - scored,
                "accuracy": correct / scored if scored else None,
            },
            "latency_p95_seconds": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            if latencies
            else None,
            "rejections": [d for d in decisions if d["status"] == "REJECTED"],
            "decisions": decisions,
            "unresolved_positions": [
                {("cost_paise" if k == "cost" else k): v for k, v in dict(r).items()}
                for r in self.db.execute("SELECT * FROM positions")
            ],
            "fills": [
                {(k + "_paise" if k in ("notional", "fee", "realized") else k): v for k, v in f.items()}
                for f in fills
            ],
        }
