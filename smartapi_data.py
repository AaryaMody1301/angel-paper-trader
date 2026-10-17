"""Read-only SmartAPI REST data for NSE EQ instruments; no broker execution API."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import getpass
from http.client import HTTPException, HTTPSConnection
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import ssl
import sys
import time
import warnings

from paper_trader import MAX_AMOUNT, amount, integer, readable, utc

IST = timezone(timedelta(hours=5, minutes=30))
LOGIN = ("POST", "apiconnect.angelone.in", "/rest/auth/angelbroking/user/v1/loginByPassword")
QUOTE = ("POST", "apiconnect.angelone.in", "/rest/secure/angelbroking/market/v1/quote")
HISTORY = ("POST", "apiconnect.angelone.in", "/rest/secure/angelbroking/historical/v1/getCandleData")
MASTER = ("GET", "margincalculator.angelone.in", "/OpenAPI_File/files/OpenAPIScripMaster.json")
ALLOWED_ENDPOINTS = frozenset((LOGIN, QUOTE, HISTORY, MASTER))
ENV_KEYS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_LOCAL_IP", "ANGEL_PUBLIC_IP", "ANGEL_MAC_ADDRESS")
INTERVAL_DAYS = {"ONE_MINUTE": 30, "THREE_MINUTE": 60, "FIVE_MINUTE": 100,
                 "TEN_MINUTE": 100, "FIFTEEN_MINUTE": 200, "THIRTY_MINUTE": 200,
                 "ONE_HOUR": 400, "ONE_DAY": 2000}
AUTH_CODES = frozenset(("AG8001", "AG8002", "AG8003", "AB8050", "AB8051", "AB1010", "AB1011"))
MONTHS = {name: index for index, name in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


class SmartAPIError(ValueError):
    """Safe messages only: never include broker bodies, headers or exceptions."""


class SessionExpired(SmartAPIError):
    pass


class RateLimited(SmartAPIError):
    def __init__(self, retry_after=60):
        self.retry_after = retry_after
        super().__init__(f"Rate limited; wait at least {retry_after} seconds before retrying. No data returned.")


def _retry_after(value, now=None):
    # Do not echo an arbitrary response header into an exception.
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,8}", value):
        return max(int(value), 1)
    try:
        at = utc(parsedate_to_datetime(value))
        return max(1, math.ceil((at - (now or datetime.now(timezone.utc))).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return 60


def _rate_error(body):
    return any(text in body.lower() for text in (b"exceeding access rate", b"exceeding rate limit", b"too many requests"))


def _reject_constant(_):
    raise ValueError("non-finite JSON number")


def _http(endpoint, payload=None, headers=None):
    """The only outbound boundary. Fixed HTTPS targets, verified TLS, no redirects."""
    if endpoint not in ALLOWED_ENDPOINTS:
        raise SmartAPIError("Endpoint blocked: only login, FULL quotes, candles and the instrument master are allowed.")
    method, host, path = endpoint
    if endpoint == MASTER:
        headers = {"Accept": "application/json"}  # Never send account headers to the public master host.
    limit = (64 if endpoint == MASTER else 8) * 1024 * 1024
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = HTTPSConnection(host, timeout=15, context=context)
    try:
        connection.request(method, path, body=None if payload is None else json.dumps(payload, allow_nan=False), headers=headers or {})
        response = connection.getresponse()
        body = response.read(limit + 1)
        if len(body) > limit:
            raise SmartAPIError("SmartAPI response exceeds the size limit; no data returned.")
        retry = _retry_after(response.getheader("Retry-After"))
        if response.status == 429 or (response.status == 403 and _rate_error(body)):
            raise RateLimited(retry)
        if 300 <= response.status < 400:
            raise SmartAPIError("HTTP redirect blocked; check the current official endpoint. No credentials forwarded.")
        try:
            result = json.loads(body, parse_float=Decimal, parse_constant=_reject_constant)
        except (ValueError, UnicodeError):
            result = None
        code = result.get("errorcode") if isinstance(result, dict) else None
        if (isinstance(code, str) and code in AUTH_CODES) or response.status == 401:
            raise SessionExpired("Session invalid or expired; log in locally again with a fresh TOTP.")
        if code == "AB1021":
            raise RateLimited(retry)
        if response.status == 403:
            raise SmartAPIError("Access denied; check the API app, account permissions and registered static IPv4 egress.")
        if response.status != 200:
            raise SmartAPIError(f"SmartAPI HTTP {response.status}; no data returned. Retry only after resolving the service error.")
        if result is None:
            raise SmartAPIError("Invalid JSON response from SmartAPI; no data returned.")
        if endpoint == MASTER:
            return result
        if not isinstance(result, dict) or type(result.get("status")) is not bool:
            raise SmartAPIError("Malformed SmartAPI response envelope; no data returned.")
        if not result["status"] or result.get("errorcode") not in (None, ""):
            if _rate_error(body):
                raise RateLimited(retry)
            raise SmartAPIError("SmartAPI rejected the request; check local credentials, TOTP, API permissions and inputs. Broker details suppressed.")
        return result.get("data")
    except ssl.SSLCertVerificationError:
        raise SmartAPIError("TLS certificate validation failed; repair the local trust store or network. Certificate checks cannot be disabled.") from None
    except (OSError, HTTPException):
        raise SmartAPIError("Connection failed or disconnected; no data returned. Check connectivity before retrying.") from None
    finally:
        connection.close()


@dataclass(frozen=True, repr=False)
class Settings:
    api_key: str
    client_code: str
    local_ip: str
    public_ip: str
    mac_address: str

    def __post_init__(self):
        for name, value in zip(ENV_KEYS, (self.api_key, self.client_code, self.local_ip, self.public_ip, self.mac_address)):
            if not isinstance(value, str) or not 1 <= len(value) <= 256 or value.startswith("YOUR_") or any(not 33 <= ord(c) <= 126 for c in value):
                raise SmartAPIError(f"Set {name} locally to a valid value; placeholders and control characters are rejected.")
        if not re.fullmatch(r"[A-Za-z0-9]{1,32}", self.client_code):
            raise SmartAPIError("Invalid ANGEL_CLIENT_CODE format.")
        try:
            ipaddress.IPv4Address(self.local_ip)
            ipaddress.IPv4Address(self.public_ip)
        except ipaddress.AddressValueError:
            raise SmartAPIError("ANGEL_LOCAL_IP and ANGEL_PUBLIC_IP must contain real local/public IPv4 addresses.") from None
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", self.mac_address):
            raise SmartAPIError("ANGEL_MAC_ADDRESS must use six colon-separated hexadecimal pairs.")

    @classmethod
    def load(cls, path=".env"):
        """Small literal KEY=VALUE file; process environment takes precedence."""
        values = {}
        try:
            lines = Path(path).read_text(encoding="utf-8-sig").splitlines() if Path(path).exists() else []
        except (OSError, UnicodeError):
            raise SmartAPIError("Cannot read the local environment file.") from None
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not separator or key not in ENV_KEYS or key in values:
                raise SmartAPIError("Invalid environment file: use each supported ANGEL_* key once, as KEY=VALUE.")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
        return cls(*(os.environ.get(key, values.get(key, "")) for key in ENV_KEYS))


@dataclass(frozen=True)
class Instrument:
    exchange: str
    symbol: str
    token: str
    tick_paise: int

    def __post_init__(self):
        if self.exchange != "NSE" or not isinstance(self.symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9&._-]{0,76}-EQ", self.symbol):
            raise SmartAPIError("Only NSE EQ cash instruments are supported in this adapter.")
        if not isinstance(self.token, str) or not re.fullmatch(r"[0-9]{1,20}", self.token):
            raise SmartAPIError("Instrument token must be a numeric string from the current instrument master.")
        integer(self.tick_paise, "instrument tick in paise", 1)


def resolve_instrument(rows, exchange, symbol, token):
    """Bind all three identifiers to one current, unambiguous master row."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SmartAPIError("Malformed instrument master; cannot verify instrument identity.")
    matches = [row for row in rows if row.get("exch_seg") == exchange and (row.get("symbol") == symbol or row.get("token") == token)]
    if len(matches) != 1 or matches[0].get("symbol") != symbol or matches[0].get("token") != token:
        raise SmartAPIError("Instrument identity is missing, ambiguous or mismatched in the current master; check exchange, symbol and token.")
    row = matches[0]
    try:
        tick = Decimal(row["tick_size"])
        if row.get("instrumenttype") != "" or row.get("expiry") != "" or Decimal(row["lotsize"]) != 1 or not tick.is_finite() or not 1 <= tick <= MAX_AMOUNT or tick != tick.to_integral_value():
            raise ValueError
        # Master tick_size is already paise, unlike REST quote/candle prices (INR).
        return Instrument(exchange, symbol, token, int(tick))
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise SmartAPIError("Unsupported or invalid instrument metadata; only NSE EQ cash instruments with integral paise ticks and lot size 1 are supported.") from None


def _price(value, instrument=None):
    if type(value) not in (int, str, Decimal):
        raise SmartAPIError("Invalid market price type.")
    try:
        price = amount(str(value))
        if price <= 0 or (instrument is not None and price % instrument.tick_paise):
            raise ValueError
        return price
    except ValueError:
        raise SmartAPIError("Invalid market price: require positive INR, exact paise and the instrument tick size.") from None


def _feed_time(value):
    """Official FULL timestamps are English day-month-year in exchange time (IST)."""
    try:
        match = re.fullmatch(r"([0-9]{2})-([A-Za-z]{3})-([0-9]{4}) ([0-9]{2}):([0-9]{2}):([0-9]{2})", value)
        day, month, year, hour, minute, second = match.groups()
        return datetime(int(year), MONTHS[month], int(day), int(hour), int(minute), int(second), tzinfo=IST).astimezone(timezone.utc)
    except (TypeError, AttributeError, KeyError, ValueError):
        raise SmartAPIError("Invalid exchange timestamp; expected DD-Mon-YYYY HH:MM:SS in IST.") from None


def _best(levels, instrument, buy):
    if not isinstance(levels, list) or not 1 <= len(levels) <= 5:
        raise SmartAPIError("Bid/ask depth is unavailable or malformed; no usable quote.")
    prices = []
    for level in levels:
        try:
            quantity = integer(level["quantity"], "depth quantity")
            integer(level["orders"], "depth order count")
            if quantity:
                prices.append(_price(level["price"], instrument))
        except (KeyError, TypeError, ValueError):
            raise SmartAPIError("Invalid bid/ask depth; no usable quote.") from None
    if not prices:
        raise SmartAPIError("Bid/ask depth is empty; no usable quote.")
    return max(prices) if buy else min(prices)


def parse_quote(data, instrument, now, max_age=30):
    integer(max_age, "maximum quote age", 1, 3600)
    if not isinstance(data, dict) or data.get("unfetched") != [] or not isinstance(data.get("fetched"), list) or len(data["fetched"]) != 1:
        raise SmartAPIError("Requested quote is missing, partial or unfetched; no usable quote.")
    row = data["fetched"][0]
    if not isinstance(row, dict) or (row.get("exchange"), row.get("tradingSymbol"), row.get("symbolToken")) != (instrument.exchange, instrument.symbol, instrument.token):
        raise SmartAPIError("Quote instrument identity does not match the validated master.")
    at, traded = _feed_time(row.get("exchFeedTime")), _feed_time(row.get("exchTradeTime"))
    now = utc(now)
    if not 0 <= (now - at).total_seconds() <= max_age:
        raise SmartAPIError("Quote is stale or future-dated; no usable quote. Check exchange hours and the local clock.")
    if traded > at:
        raise SmartAPIError("Last-trade timestamp is later than the exchange feed timestamp.")
    depth = row.get("depth")
    if not isinstance(depth, dict):
        raise SmartAPIError("FULL bid/ask depth is missing; LTP is never a replacement for a bid/ask.")
    bid, ask = _best(depth.get("buy"), instrument, True), _best(depth.get("sell"), instrument, False)
    if bid > ask:
        raise SmartAPIError("Crossed bid/ask rejected; verify data and depth-side mapping.")
    return {"source": "ANGEL_SMARTAPI_REST_FULL", "exchange": instrument.exchange,
            "symbol": instrument.symbol, "token": instrument.token,
            "bid_paise": bid, "ask_paise": ask, "ltp_paise": _price(row.get("ltp")),
            "timestamp": at.isoformat(), "last_trade_timestamp": traded.isoformat(), "received_at": now.isoformat()}


def history_request(instrument, interval, start, end, now):
    start, end = utc(start), utc(end)
    if interval not in INTERVAL_DAYS or start >= end or end > utc(now):
        raise SmartAPIError("History requires a supported interval and an increasing, non-future time range.")
    if start.second or start.microsecond or end.second or end.microsecond:
        raise SmartAPIError("History boundaries must use whole minutes; timestamps must include a timezone.")
    if end - start > timedelta(days=INTERVAL_DAYS[interval]):
        raise SmartAPIError("History range exceeds the documented interval limit; request a smaller range.")
    return {"exchange": instrument.exchange, "symboltoken": instrument.token, "interval": interval,
            "fromdate": start.astimezone(IST).strftime("%Y-%m-%d %H:%M"),
            "todate": end.astimezone(IST).strftime("%Y-%m-%d %H:%M")}


def parse_candles(rows, start, end, now):
    if not isinstance(rows, list) or not rows:
        raise SmartAPIError("Historical candles are unavailable or empty; no data invented.")
    candles, previous = [], None
    start, end, now = utc(start), utc(end), utc(now)
    try:
        for row in rows:
            if not isinstance(row, list) or len(row) != 6:
                raise ValueError
            at = utc(datetime.fromisoformat(row[0]))
            if not start <= at <= end or at > now or (previous is not None and at <= previous):
                raise ValueError
            # Today's master tick need not be the tick used by a historical trade.
            opened, high, low, close = (_price(value) for value in row[1:5])
            if not low <= min(opened, close) <= max(opened, close) <= high:
                raise ValueError
            candles.append({"timestamp": at.isoformat(), "open_paise": opened, "high_paise": high,
                            "low_paise": low, "close_paise": close, "volume": integer(row[5], "candle volume")})
            previous = at
    except (ValueError, TypeError, OverflowError):
        raise SmartAPIError("Invalid historical candle: check timestamps, range, order, OHLC prices and volume.") from None
    return candles


class SmartAPIData:
    """Memory-only sessions; explicit re-login after expiry, never automatic retries."""

    def __init__(self, settings, clock=None):
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._next_allowed = {}
        self.close()

    def close(self):
        # Local disposal only; a broker logout could invalidate another application's session.
        self._jwt = None
        self._expires = None
        self._seen = {}

    def _call(self, endpoint, payload):
        if endpoint not in (LOGIN, QUOTE, HISTORY):
            raise SmartAPIError("Endpoint blocked: this client exposes authentication and read-only data only.")
        if endpoint != LOGIN and (self._jwt is None or utc(self.clock()) >= self._expires):
            self.close()
            raise SessionExpired("No active session or daily session expired; log in locally with a fresh TOTP.")
        instant = time.monotonic()
        remaining = self._next_allowed.get(endpoint, instant) - instant
        if remaining > 0:
            raise RateLimited(math.ceil(remaining))
        # One request/second per endpoint is below every documented minute/hour cap.
        # ponytail: pacing is per instance; coordinate other processes/apps for the same client code.
        self._next_allowed[endpoint] = instant + 1
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "X-UserType": "USER", "X-SourceID": "WEB", "X-PrivateKey": self.settings.api_key,
                   "X-ClientLocalIP": self.settings.local_ip, "X-ClientPublicIP": self.settings.public_ip,
                   "X-MACAddress": self.settings.mac_address}
        if endpoint != LOGIN:
            headers["Authorization"] = "Bearer " + self._jwt
        try:
            return _http(endpoint, payload, headers)
        except SessionExpired:
            self.close()
            raise
        except RateLimited as exc:
            self._next_allowed[endpoint] = time.monotonic() + exc.retry_after
            raise

    def login(self, pin, totp):
        self.close()
        if not isinstance(pin, str) or not 1 <= len(pin) <= 128 or any(not 33 <= ord(c) <= 126 for c in pin):
            raise SmartAPIError("Enter a valid account PIN at the local hidden prompt.")
        if not isinstance(totp, str) or not re.fullmatch(r"[0-9]{6}", totp):
            raise SmartAPIError("Enter the current six-digit authenticator TOTP at the local hidden prompt.")
        data = self._call(LOGIN, {"clientcode": self.settings.client_code, "password": pin, "totp": totp})
        token = data.get("jwtToken") if isinstance(data, dict) else None
        if not isinstance(token, str) or len(token) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
            raise SmartAPIError("Login response has no valid JWT; session remains unavailable.")
        self._jwt = token
        self._expires = (utc(self.clock()).astimezone(IST) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        # Refresh/feed tokens and any profile data are intentionally discarded.

    def quote(self, instrument, max_age=30):
        integer(max_age, "maximum quote age", 1, 3600)
        data = self._call(QUOTE, {"mode": "FULL", "exchangeTokens": {instrument.exchange: [instrument.token]}})
        quote = parse_quote(data, instrument, self.clock(), max_age)
        key = instrument.exchange, instrument.token
        signature = (quote["timestamp"], quote["bid_paise"], quote["ask_paise"], quote["ltp_paise"])
        previous = self._seen.get(key)
        if previous and (signature[0] < previous[0] or (signature[0] == previous[0] and signature != previous)):
            raise SmartAPIError("Out-of-order or conflicting quote timestamp; no usable quote.")
        self._seen[key] = signature
        return quote

    def candles(self, instrument, interval, start, end):
        payload = history_request(instrument, interval, start, end, self.clock())
        data = self._call(HISTORY, payload)
        candles = parse_candles(data, start, end, self.clock())
        if interval == "ONE_MINUTE" and len(candles) > 8000:
            raise SmartAPIError("History exceeded the documented 8000-record one-minute limit; verify the current protocol.")
        return {"source": "ANGEL_SMARTAPI_HISTORY", "exchange": instrument.exchange,
                "symbol": instrument.symbol, "token": instrument.token, "interval": interval,
                "one_minute_record_limit_reached": interval == "ONE_MINUTE" and len(candles) == 8000,
                "candles": candles}


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unknown arguments, which might be pasted secrets.
        self.print_usage(sys.stderr)
        self.exit(2, "Error: invalid command arguments; use --help. Never pass credentials as arguments.\n")


def main(argv=None):
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env", help="local literal KEY=VALUE file; environment overrides it")
    commands = parser.add_subparsers(dest="command", required=True)
    quote = commands.add_parser("quote", help="inspect one fresh REST FULL bid/ask snapshot")
    quote.add_argument("--max-quote-age", type=int, default=30)
    history = commands.add_parser("candles", help="inspect historical OHLCV; never treated as a live quote")
    history.add_argument("--interval", choices=INTERVAL_DAYS, default="ONE_MINUTE")
    history.add_argument("--from-time", required=True, help="ISO 8601 with UTC offset, whole minutes")
    history.add_argument("--to-time", required=True, help="ISO 8601 with UTC offset, whole minutes")
    for command in (quote, history):
        command.add_argument("--exchange", choices=("NSE",), default="NSE")
        command.add_argument("--symbol", required=True, help="exact current master symbol, including -EQ")
        command.add_argument("--token", required=True, help="matching token from the current broker master")
    args = parser.parse_args(argv)
    client = None
    try:
        # Validate local inputs before any network connection or secret prompts.
        identity = Instrument(args.exchange, args.symbol, args.token, 1)
        if args.command == "candles":
            start, end = datetime.fromisoformat(args.from_time), datetime.fromisoformat(args.to_time)
            history_request(identity, args.interval, start, end, datetime.now(timezone.utc))
        else:
            integer(args.max_quote_age, "maximum quote age", 1, 3600)
        settings = Settings.load(args.env_file)
        if not sys.stdin.isatty():
            raise SmartAPIError("Use an interactive local terminal for hidden PIN/TOTP prompts; credentials cannot be piped or passed as CLI flags.")
        instrument = resolve_instrument(_http(MASTER), args.exchange, args.symbol, args.token)
        client = SmartAPIData(settings)
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            pin = getpass.getpass("Angel One PIN (local, hidden): ")
            totp = getpass.getpass("Current authenticator TOTP (local, hidden): ")
        try:
            client.login(pin, totp)
        finally:
            pin = totp = None
        result = client.quote(instrument, args.max_quote_age) if args.command == "quote" else client.candles(instrument, args.interval, start, end)
        print(json.dumps(readable(result), indent=2))
        return 0
    except SmartAPIError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ValueError:
        print("Error: invalid local input; check identifiers, quote age and timezone-aware ISO history dates.", file=sys.stderr)
        return 1
    except (OSError, getpass.GetPassWarning):
        print("Error: local I/O or hidden credential entry unavailable; use a local interactive terminal.", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled; no data returned.", file=sys.stderr)
        return 130
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
