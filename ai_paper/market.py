"""Verified contract metadata, private recordings and exact native price units."""

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import gzip
import json
from pathlib import Path
import sqlite3

from paper_trader import integer, utc
from smartapi_data import IST, SmartAPIData, SmartAPIError, _feed_time, history_request

SEGMENTS = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5, "NCDEX": 7, "CDS": 13}
DIRECTIONS = ("DOWN", "FLAT", "UP")
BAR = timedelta(minutes=5)
APP_ID = 0x41505054


class AIError(SmartAPIError):
    """Only fixed, operator-safe messages from this application."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def config_id(config):
    economic = json.loads(canonical(config))
    for spec in economic["segments"].values():
        for key in ("verified_on", "live_verified_on", "sessions"):
            spec.pop(key, None)
    for spec in economic.get("contracts", {}).values():
        spec.pop("verified_on", None)
    return digest(economic)


def calendars_match(config, recorded):
    return all(
        config["segments"][segment].get("sessions", {}).get(day) == session
        for segment, days in recorded.items()
        for day, session in days.items()
    )


def direction(close, target):
    return "UP" if target * 10000 > close * 10010 else "DOWN" if target * 10000 < close * 9990 else "FLAT"


def instant(value):
    return utc(datetime.fromisoformat(value))


def exact(value, scale):
    if isinstance(value, bool):
        raise AIError("boolean is not a price")
    scaled = Decimal(str(value)) * scale
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        raise AIError("price precision does not match the verified contract scale")
    return integer(int(scaled), "price units", 1)


@dataclass(frozen=True)
class Contract:
    exchange: str
    symbol: str
    token: str
    kind: str
    scale: int
    tick: int
    lot: int
    multiplier: str
    expiry: str | None = None
    verified_on: str | None = None
    depth_factor: int = 1
    volume_factor: int = 1

    def __post_init__(self):
        if self.exchange not in SEGMENTS or self.kind not in ("CASH", "FUTURE", "OPTION", "INDEX"):
            raise AIError("unsupported exchange or contract kind")
        if (
            not isinstance(self.symbol, str)
            or not 1 <= len(self.symbol) <= 100
            or any(ord(c) < 32 or ord(c) > 126 for c in self.symbol)
        ):
            raise AIError("invalid contract symbol")
        if (
            not isinstance(self.token, str)
            or not self.token.isascii()
            or not self.token.isdecimal()
            or len(self.token) > 20
        ):
            raise AIError("invalid instrument token")
        integer(self.scale, "price scale", 1, 100_000_000)
        integer(self.tick, "tick", 0 if self.kind == "INDEX" else 1)
        integer(self.lot, "lot", 1, 1_000_000_000)
        integer(self.depth_factor, "depth unit factor", 1, 1_000_000_000)
        integer(self.volume_factor, "volume unit factor", 1, 1_000_000_000)
        multiplier = Decimal(self.multiplier)
        if not multiplier.is_finite() or not 0 < multiplier <= 1_000_000_000:
            raise AIError("invalid contract multiplier")
        if self.expiry is not None:
            instant(self.expiry)
        if self.verified_on is not None:
            date.fromisoformat(self.verified_on)
        if self.kind in ("OPTION", "FUTURE") and self.expiry is None:
            raise AIError("derivatives require an exact expiry cutoff")

    @property
    def key(self):
        return digest([self.exchange, self.symbol, self.token, self.expiry])

    @property
    def exchange_type(self):
        return SEGMENTS[self.exchange]

    def notional(self, price, quantity, buy=True):
        integer(price, "native price", 1)
        integer(quantity, "units", 1)
        value = Decimal(price) * quantity * Decimal(self.multiplier) * 100 / self.scale
        return integer(
            int(value.to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR)), "notional", 1
        )

    def verified(self, now):
        return (
            self.verified_on is not None
            and 0 <= (utc(now).date() - date.fromisoformat(self.verified_on)).days <= 90
        )


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if config.get("version") != 1 or set(config.get("segments", {})) != set(SEGMENTS):
        raise AIError("config must declare version 1 and every SmartAPI segment")
    return config


def resolve_contracts(master, config, now):
    """Unknown multipliers/scales stay blocked; the master is never a calendar."""
    if not isinstance(master, list) or any(not isinstance(r, dict) for r in master):
        raise AIError("invalid instrument master")
    contracts, blocked = [], {}
    wires = Counter((r.get("exch_seg"), r.get("token")) for r in master)
    names = Counter((r.get("exch_seg"), r.get("symbol")) for r in master)
    for row in master:
        segment = row.get("exch_seg")
        if segment not in SEGMENTS:
            continue
        try:
            settings = config["segments"][segment]
            if wires[segment, row.get("token")] != 1 or names[segment, row.get("symbol")] != 1:
                raise AIError("ambiguous master identity")
            override = config.get("contracts", {}).get(segment + ":" + row["token"], {})
            spec = {**settings, **override}
            if not spec.get("verified_on") or not spec.get("scale") or not spec.get("multiplier"):
                raise AIError("metadata scale/multiplier verification required")
            name = row.get("instrumenttype", "")
            kind = (
                "FUTURE"
                if name.startswith("FUT")
                else "OPTION"
                if name.startswith("OPT")
                else "CASH"
                if name == ""
                and (
                    row.get("symbol", "").endswith("-EQ")
                    or segment == "BSE"
                    and override.get("kind") == "CASH"
                )
                else "INDEX"
                if name == "AMXIDX"
                else "UNSUPPORTED"
            )
            expiry = None
            if kind in ("OPTION", "FUTURE"):
                expiry_day = datetime.strptime(row["expiry"], "%d%b%Y").date().isoformat()
                expiry = spec.get("expiry")
                if expiry is None or instant(expiry).astimezone(IST).date().isoformat() != expiry_day:
                    raise AIError("verified contract-specific expiry cutoff required")
            tick = Decimal(str(row["tick_size"])) * Decimal(str(spec.get("master_tick_factor", "1")))
            lot = Decimal(str(row["lotsize"]))
            if tick != tick.to_integral_value() or lot != lot.to_integral_value():
                raise AIError("non-integral native tick or lot")
            contract = Contract(
                segment,
                row["symbol"],
                row["token"],
                kind,
                spec["scale"],
                int(tick),
                int(lot),
                str(spec["multiplier"]),
                expiry,
                spec["verified_on"],
                spec.get("depth_factor", 1),
                spec.get("volume_factor", 1),
            )
            if not contract.verified(now):
                raise AIError("metadata verification expired")
            contracts.append(contract)
        except (KeyError, ValueError, TypeError, ArithmeticError):
            blocked[segment] = blocked.get(segment, 0) + 1
    return contracts, blocked


def session_window(config, contract, day):
    entry = config["segments"][contract.exchange].get("sessions", {}).get(day)
    if not isinstance(entry, dict):
        raise AIError("verified dated exchange session is unavailable")
    opened, closed = instant(entry["open"]), instant(entry["close"])
    if opened >= closed or opened.astimezone(IST).date().isoformat() != day:
        raise AIError("invalid exchange session")
    if contract.expiry:
        closed = min(closed, instant(contract.expiry))
    return opened, closed


def charges(config, contract, at, notional, side):
    spec = config["segments"][contract.exchange]["costs"]
    day = utc(at).astimezone(IST).date()
    if not date.fromisoformat(spec["valid_from"]) <= day <= date.fromisoformat(spec["valid_until"]):
        raise AIError("dated cost model unavailable or expired")
    raw = spec["buy_bps" if side == "BUY" else "sell_bps"]
    if type(raw) not in (int, str):
        raise AIError("cost basis points must be exact integer or decimal text")
    bps = Decimal(raw)
    if not bps.is_finite() or not 0 <= bps <= 10000:
        raise AIError("invalid cost basis points")
    flat = integer(spec["flat_paise"], "flat charge")
    return integer(
        int((Decimal(notional) * bps / 10000).to_integral_value(rounding=ROUND_CEILING)) + flat,
        "total charge",
    )


def parse_full(data, contract, now, max_age=30):
    if not isinstance(data, dict) or data.get("unfetched") != [] or len(data.get("fetched", [])) != 1:
        raise AIError("missing or partial FULL quote")
    row = data["fetched"][0]
    if not isinstance(row, dict):
        raise AIError("malformed FULL quote")
    if (row.get("exchange"), row.get("tradingSymbol"), row.get("symbolToken")) != (
        contract.exchange,
        contract.symbol,
        contract.token,
    ):
        raise AIError("FULL quote identity mismatch")
    at = _feed_time(row.get("exchFeedTime"))
    now = utc(now)
    if not 0 <= (now - at).total_seconds() <= max_age:
        raise AIError("stale or future FULL quote")
    sides = []
    for name, buy in (("buy", True), ("sell", False)):
        levels = row.get("depth", {}).get(name)
        if not isinstance(levels, list) or not 1 <= len(levels) <= 5:
            raise AIError("missing FULL depth")
        active = []
        for level in levels:
            qty = integer(level["quantity"], "depth quantity")
            integer(level["orders"], "depth orders")
            if qty:
                price = exact(level["price"], contract.scale)
                if price % contract.tick:
                    raise AIError("depth tick mismatch")
                active.append((price, integer(qty * contract.depth_factor, "normalised depth quantity")))
        if not active:
            raise AIError("empty executable depth")
        price = (max if buy else min)(p for p, _ in active)
        sides.append((price, sum(q for p, q in active if p == price)))
    if sides[0][0] > sides[1][0]:
        raise AIError("crossed FULL quote")
    result = {
        "instrument": contract.key,
        "timestamp": at.isoformat(),
        "received_at": now.isoformat(),
        "bid": sides[0][0],
        "ask": sides[1][0],
        "bid_qty": sides[0][1],
        "ask_qty": sides[1][1],
        "ltp": exact(row["ltp"], contract.scale),
        "sequence": None,
        "source": "REST_FULL",
    }
    if "tradeVolume" in row:
        result["volume_total"] = integer(row["tradeVolume"] * contract.volume_factor, "traded volume")
    return result


class MarketData(SmartAPIData):
    def snapshot(self, contract, max_age=30):
        from smartapi_data import QUOTE

        data = self._call(QUOTE, {"mode": "FULL", "exchangeTokens": {contract.exchange: [contract.token]}})
        return parse_full(data, contract, self.clock(), max_age)

    def history(self, contract, start, end):
        from smartapi_data import HISTORY

        payload = history_request(contract, "FIVE_MINUTE", start, end, self.clock())
        rows = self._call(HISTORY, payload)
        if not isinstance(rows, list):
            raise AIError("history missing")
        result, previous = [], None
        for row in rows:
            if not isinstance(row, list) or len(row) != 6:
                raise AIError("malformed historical bar")
            at = instant(row[0])
            if at == utc(end) or at + BAR > utc(self.clock()):
                continue
            if not utc(start) <= at < utc(end) or (previous is not None and at <= previous):
                raise AIError("incomplete or out-of-order history")
            prices = [exact(p, contract.scale) for p in row[1:5]]
            if not prices[2] <= min(prices[0], prices[3]) <= max(prices[0], prices[3]) <= prices[1]:
                raise AIError("invalid history OHLC")
            result.append(
                {
                    "instrument": contract.key,
                    "timestamp": at.isoformat(),
                    "ohlc": prices,
                    "volume": integer(row[5] * contract.volume_factor, "volume"),
                    "origin": "SMARTAPI_HISTORY",
                    "flags": [],
                }
            )
            previous = at
        return result


class Store:
    """A separate, additive AI journal; opening an offline account is rejected."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        app = self.db.execute("PRAGMA application_id").fetchone()[0]
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if app not in (0, APP_ID) or (app == 0 and tables) or version not in (0, 1):
            self.db.close()
            raise AIError("choose a separate AI journal; existing database is untouched")
        self.db.executescript(f"""
            PRAGMA application_id = {APP_ID};
            PRAGMA user_version = 1;
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS masters (day TEXT NOT NULL, hash TEXT NOT NULL, compressed BLOB NOT NULL,
                PRIMARY KEY(day,hash));
            CREATE TABLE IF NOT EXISTS contracts (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS contract_wire ON contracts(json_extract(payload,'$.exchange'),json_extract(payload,'$.token'));
            CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, instrument TEXT NOT NULL, timestamp TEXT NOT NULL,
                received_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS quote_time ON quotes(instrument, timestamp);
            CREATE TABLE IF NOT EXISTS bars (instrument TEXT NOT NULL, timestamp TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(instrument, timestamp));
            CREATE TABLE IF NOT EXISTS health (instrument TEXT PRIMARY KEY, status TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS health_events (instrument TEXT NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL,
                PRIMARY KEY(instrument,status,at));
            CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), initial INTEGER NOT NULL, cash INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (instrument TEXT PRIMARY KEY, quantity INTEGER NOT NULL, cost INTEGER NOT NULL,
                due TEXT NOT NULL, model TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills (id TEXT PRIMARY KEY, instrument TEXT NOT NULL, side TEXT NOT NULL,
                quantity INTEGER NOT NULL, notional INTEGER NOT NULL, fee INTEGER NOT NULL, realized INTEGER NOT NULL,
                quote_id TEXT NOT NULL, at TEXT NOT NULL, model TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, instrument TEXT NOT NULL, bar TEXT NOT NULL,
                at TEXT NOT NULL, model TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS consumed (quote_id TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
                PRIMARY KEY(quote_id,side));
            CREATE TABLE IF NOT EXISTS equity (at TEXT PRIMARY KEY, value INTEGER, model TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shadow (model TEXT NOT NULL, segment TEXT NOT NULL, day TEXT NOT NULL,
                complete INTEGER NOT NULL, failures INTEGER NOT NULL, PRIMARY KEY(model,segment,day));
        """)

    def close(self):
        self.db.close()

    def archive_master(self, day, rows):
        payload = canonical(rows).encode()
        checksum = hashlib.sha256(payload).hexdigest()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO masters VALUES (?,?,?)", (day, checksum, gzip.compress(payload))
            )
        return checksum

    def set_meta(self, key, payload):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, canonical(payload)))

    def meta(self, key, default=None):
        row = self.db.execute("SELECT payload FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def add_contract(self, contract):
        row = self.db.execute("SELECT payload FROM contracts WHERE id=?", (contract.key,)).fetchone()
        old = Contract(**json.loads(row[0])) if row else None
        if old and (old.scale, old.tick, old.lot, old.multiplier, old.depth_factor, old.volume_factor) != (
            contract.scale,
            contract.tick,
            contract.lot,
            contract.multiplier,
            contract.depth_factor,
            contract.volume_factor,
        ):
            raise AIError("contract units changed; reconcile journal before replacing metadata")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO contracts VALUES (?,?)", (contract.key, canonical(asdict(contract)))
            )

    def contracts(self):
        return {r[0]: Contract(**json.loads(r[1])) for r in self.db.execute("SELECT * FROM contracts")}

    def contract(self, key):
        row = self.db.execute("SELECT payload FROM contracts WHERE id=?", (key,)).fetchone()
        if row is None:
            raise AIError("contract identity is not in this journal")
        return Contract(**json.loads(row[0]))

    def by_wire(self, exchange, token):
        return [
            Contract(**json.loads(r[0]))
            for r in self.db.execute(
                "SELECT payload FROM contracts WHERE json_extract(payload,'$.exchange')=? AND json_extract(payload,'$.token')=?",
                (exchange, token),
            )
        ]

    def health(self, instrument, status, now):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO health VALUES (?,?,?)", (instrument, status, utc(now).isoformat())
            )
            self.db.execute(
                "INSERT OR IGNORE INTO health_events VALUES (?,?,?)",
                (instrument, status, utc(now).isoformat()),
            )

    def add_quote(self, quote):
        c = self.contract(quote["instrument"])
        at, received = instant(quote["timestamp"]), instant(quote["received_at"])
        if not 0 <= (received - at).total_seconds() <= 30 or quote.get("source") not in (
            "REST_FULL",
            "WS_SNAP_QUOTE",
        ):
            raise AIError("invalid recording source or age")
        for name in ("bid", "ask", "ltp", "bid_qty", "ask_qty"):
            integer(quote[name], name, 0 if c.kind == "INDEX" and name != "ltp" else 1)
        if c.kind != "INDEX" and (
            quote["bid"] > quote["ask"] or quote["bid"] % c.tick or quote["ask"] % c.tick
        ):
            raise AIError("invalid recording prices")
        previous = self.latest(c.key)
        if quote["source"] == "WS_SNAP_QUOTE":
            integer(quote.get("sequence"), "sequence", 0, 2**63 - 1)
        fingerprint = {
            k: v
            for k, v in quote.items()
            if k not in ("received_at", "id", "contract_spec", "liquidity_id", "liquidity_ids")
        }
        key = digest(fingerprint)
        if previous:
            old = previous["payload"]
            if at < instant(old["timestamp"]):
                raise AIError("out-of-order recording")
            if at == instant(old["timestamp"]) and key != previous["id"]:
                if (
                    quote.get("sequence") is None
                    or old.get("sequence") is None
                    or quote["sequence"] <= old["sequence"]
                ):
                    raise AIError("conflicting recording at same timestamp")
        liquidity = {}
        for side, price, quantity in (("BUY", "ask", "ask_qty"), ("SELL", "bid", "bid_qty")):
            old = previous["payload"] if previous else {}
            unchanged = previous and (quote[price], quote[quantity]) == (old[price], old[quantity])
            liquidity[side] = (
                old.get("liquidity_ids", {}).get(side, old.get("liquidity_id", previous["id"]))
                if unchanged
                else key
            )
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO quotes VALUES (?,?,?,?,?)",
                (
                    key,
                    c.key,
                    at.isoformat(),
                    received.isoformat(),
                    canonical({**fingerprint, "contract_spec": asdict(c), "liquidity_ids": liquidity}),
                ),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO health VALUES (?,?,?)", (c.key, "LIVE", received.isoformat())
            )
        return key

    def latest(self, instrument):
        row = self.db.execute(
            "SELECT * FROM quotes WHERE instrument=? ORDER BY rowid DESC LIMIT 1", (instrument,)
        ).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def add_bar(self, bar):
        if bar.get("origin") not in ("SMARTAPI_HISTORY", "RECORDED"):
            raise AIError("bar origin must be authorised history or recording")
        c = self.contract(bar["instrument"])
        if not isinstance(bar.get("flags"), list):
            raise AIError("bar metadata or quality flags missing")
        instant(bar["timestamp"])
        if not isinstance(bar["ohlc"], list) or len(bar["ohlc"]) != 4:
            raise AIError("invalid bar OHLC")
        for price in bar["ohlc"]:
            integer(price, "bar price", 1)
        o, h, l, close = bar["ohlc"]
        if not l <= min(o, close) <= max(o, close) <= h:
            raise AIError("invalid bar range")
        integer(bar["volume"], "bar volume")
        previous = self.db.execute(
            "SELECT payload FROM bars WHERE instrument=? AND timestamp<? ORDER BY timestamp DESC LIMIT 1",
            (bar["instrument"], bar["timestamp"]),
        ).fetchone()
        if previous:
            previous = json.loads(previous[0])
            old_at, at = instant(previous["timestamp"]), instant(bar["timestamp"])
            if old_at.astimezone(IST).date() == at.astimezone(IST).date() and at - old_at > BAR:
                bar["flags"] = sorted(set(bar["flags"] + ["DATA_GAP_BEFORE"]))
            prior = previous["ohlc"][3]
            if (
                old_at.astimezone(IST).date() != at.astimezone(IST).date()
                and abs(bar["ohlc"][0] - prior) * 100 > prior * 20
            ):
                bar["flags"] = sorted(set(bar["flags"] + ["CORPORATE_ACTION_REVIEW"]))
        if c.expiry and instant(bar["timestamp"]) + BAR > instant(c.expiry):
            bar["flags"] = sorted(set(bar["flags"] + ["EXPIRY_CUTOFF"]))
        old = self.db.execute(
            "SELECT payload FROM bars WHERE instrument=? AND timestamp=?",
            (bar["instrument"], bar["timestamp"]),
        ).fetchone()
        if old and json.loads(old[0]) != bar:
            raise AIError("conflicting bar; investigate corrections before replacing data")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO bars VALUES (?,?,?)",
                (bar["instrument"], bar["timestamp"], canonical(bar)),
            )

    def bars(self, instrument):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT payload FROM bars WHERE instrument=? ORDER BY timestamp", (instrument,)
            )
        ]

    def window(self, instrument, timestamp):
        return [
            json.loads(r[0])
            for r in reversed(
                list(
                    self.db.execute(
                        "SELECT payload FROM bars WHERE instrument=? AND timestamp<=? ORDER BY timestamp DESC LIMIT 24",
                        (instrument, timestamp),
                    )
                )
            )
        ]

    def shortlist(self, config, day, eligible, purpose="paper"):
        cache_key = "shortlist:" + purpose + ":" + day
        frozen = self.meta(cache_key)
        if frozen is not None:
            return frozen
        contracts, ranked = self.contracts(), {}
        for key in eligible:
            c = contracts[key]
            if c.kind == "INDEX":
                continue
            try:
                opened, closed = session_window(config, c, day)
                if closed - opened < BAR * 2:
                    continue
                previous = max(d for d in config["segments"][c.exchange]["sessions"] if d < day)
                start, stop = session_window(config, c, previous)
                bars = [
                    json.loads(r[0])
                    for r in self.db.execute(
                        "SELECT payload FROM bars WHERE instrument=? AND timestamp>=? AND timestamp<? ORDER BY timestamp",
                        (key, start.isoformat(), stop.isoformat()),
                    )
                ]
                expected = [start + n * BAR for n in range((stop - start) // BAR)]
                if (
                    not bars
                    or [instant(b["timestamp"]) for b in bars] != expected
                    or any(b["flags"] for b in bars)
                ):
                    continue
                score = sum(c.notional(b["ohlc"][3], b["volume"]) for b in bars if b["volume"])
                if score:
                    ranked.setdefault(c.exchange, []).append((score, key))
            except (KeyError, ValueError):
                continue
        selected = [
            key for segment in SEGMENTS for _, key in sorted(ranked.get(segment, []), reverse=True)[:2]
        ]
        if selected:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO metadata VALUES (?,?)", (cache_key, canonical(selected))
                )
                selected = self.meta(cache_key)
        return selected


class BarBuilder:
    """Receipt gaps mark a recorded bar unusable; no ticks or volume are invented."""

    def __init__(self, contract, config):
        self.contract, self.config, self.current, self.last_volume = contract, config, None, None
        self.closed_through = None

    def flush(self, now, final=False):
        if self.current is None or instant(self.current["timestamp"]) + BAR + (
            timedelta(0) if final else timedelta(seconds=2)
        ) > utc(now):
            return None
        finished, self.current = self.current, None
        self.closed_through = instant(finished["timestamp"]) + BAR
        if self.closed_through - instant(finished["last_tick"]) > timedelta(seconds=30):
            finished["flags"].append("FEED_GAP")
        if instant(finished["first_tick"]) - instant(finished["timestamp"]) > timedelta(seconds=30):
            finished["flags"].append("INCOMPLETE_START")
        return finished

    def push(self, quote):
        at = instant(quote["timestamp"])
        day = at.astimezone(IST).date().isoformat()
        opened, closed = session_window(self.config, self.contract, day)
        if not opened <= at < closed:
            return None
        start = opened + ((at - opened) // BAR) * BAR
        if self.closed_through is not None and start < self.closed_through:
            raise AIError("late tick belongs to an already completed bar")
        finished = None
        if self.current and start > instant(self.current["timestamp"]):
            finished = self.current
            if at - instant(finished["last_tick"]) > timedelta(seconds=30):
                finished["flags"].append("FEED_GAP")
            if instant(finished["first_tick"]) - instant(finished["timestamp"]) > timedelta(seconds=30):
                finished["flags"].append("INCOMPLETE_START")
            self.current = None
            self.closed_through = instant(finished["timestamp"]) + BAR
        if self.current is None:
            self.current = {
                "instrument": self.contract.key,
                "timestamp": start.isoformat(),
                "ohlc": [quote["ltp"]] * 4,
                "volume": 0,
                "origin": "RECORDED",
                "flags": ["VOLUME_UNAVAILABLE"],
                "sampling": "BROKER_SNAPSHOTS",
                "first_tick": at.isoformat(),
                "last_tick": at.isoformat(),
            }
            if start == opened:
                self.last_volume = 0
        b = self.current
        if at - instant(b["last_tick"]) > timedelta(seconds=30):
            b["flags"].append("FEED_GAP")
        b["ohlc"][1] = max(b["ohlc"][1], quote["ltp"])
        b["ohlc"][2] = min(b["ohlc"][2], quote["ltp"])
        b["ohlc"][3], b["last_tick"] = quote["ltp"], at.isoformat()
        if "volume_total" in quote:
            total = integer(quote["volume_total"], "cumulative volume")
            if "volume_start" in b:
                if total < b["volume_last"]:
                    b["flags"].append("VOLUME_RESET")
                b["volume"] += max(0, total - b["volume_last"])
            else:
                b["volume_start"] = total
                b["flags"].remove("VOLUME_UNAVAILABLE")
                if self.last_volume is not None and total >= self.last_volume:
                    b["volume"] = total - self.last_volume
                else:
                    b["flags"].append("INCOMPLETE_VOLUME")
            b["volume_last"] = total
            self.last_volume = total
        return finished
