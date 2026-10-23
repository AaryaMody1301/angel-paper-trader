"""Read-only SmartAPI WebSocket 2.0 SnapQuote streaming for validated instruments."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import getpass
from importlib.metadata import PackageNotFoundError, version
import json
import logging
import math
import ssl
import struct
import sys
import time

from ai_paper.market import Contract

from paper_trader import integer, readable, utc
from smartapi_data import (AUTH_CODES, MASTER, Instrument, RateLimited, SessionExpired, SmartAPIError,
                          _ArgumentParser, _http, _local_login, _local_settings, _retry_after, resolve_instrument)

try:
    from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
    from websockets.sync.client import reconnect
except ImportError:
    reconnect = None
    ConnectionClosed = InvalidHandshake = InvalidStatus = ()

HANDSHAKE_ERRORS = (ssl.SSLError, InvalidHandshake) if reconnect is not None else (ssl.SSLError,)

STREAM_URL = "wss://smartapisocket.angelone.in/smart-stream"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PING_INTERVAL = 20
PONG_TIMEOUT = 10


class _Interrupted(SmartAPIError):
    """A transport interruption that permits a bounded reconnect."""


class _QuoteUnavailable(SmartAPIError):
    """Valid identity with no currently executable, fresh book."""


def parse_snap_quote(packet, instrument, now, max_age=30):
    """Read 379-byte SnapQuote: equity paise or a verified Contract's native units."""
    integer(max_age, "maximum quote age", 1, 3600)
    if not isinstance(instrument, (Instrument, Contract)) or not isinstance(packet, bytes) or len(packet) != 379:
        raise SmartAPIError("Invalid SnapQuote packet: require a validated NSE EQ instrument and exactly 379 binary bytes.")
    try:
        mode, exchange = struct.unpack_from("<BB", packet)
        token, terminator, padding = packet[2:27].partition(b"\0")
        if mode != 3 or exchange != getattr(instrument, "exchange_type", 1) or not terminator or any(padding) or token.decode("ascii") != instrument.token:
            raise ValueError
        sequence, milliseconds, ltp = struct.unpack_from("<qqq", packet, 27)
        integer(sequence, "sequence number", maximum=2**63 - 1)
        integer(milliseconds, "exchange epoch milliseconds", 1)
        integer(ltp, "last traded price in paise", 1)
        at, now = EPOCH + timedelta(milliseconds=milliseconds), utc(now)
        if not 0 <= (now - at).total_seconds() <= max_age:
            raise _QuoteUnavailable("WebSocket quote is stale or future-dated; no usable quote.")
        sides, flags = {1: [], 0: []}, {1: 0, 0: 0}
        analytical=isinstance(instrument,Contract) and instrument.kind=="INDEX"
        for offset in (() if analytical else range(147, 347, 20)):
            flag, quantity, price, orders = struct.unpack_from("<hqqh", packet, offset)
            if flag not in sides:
                raise ValueError
            flags[flag] += 1
            integer(quantity, "depth quantity")
            if isinstance(instrument, Contract):
                quantity = integer(quantity*instrument.depth_factor, "normalised depth quantity")
            integer(price, "depth price in paise")
            integer(orders, "depth orders")
            if quantity:
                if price == 0 or price % (instrument.tick if isinstance(instrument,Contract) else instrument.tick_paise):
                    raise ValueError
                sides[flag].append((price, quantity))
            elif price or orders:
                raise ValueError
        # Protocol flags: 1 is buy/bid, 0 is sell/ask. SDK helper labels are reversed.
        if not analytical and flags != {1: 5, 0: 5}:
            raise ValueError
        if not analytical and (not sides[1] or not sides[0]):
            raise _QuoteUnavailable("Executable bid/ask depth is unavailable.")
        bid, ask = (0,0) if analytical else (max(p for p, _ in sides[1]), min(p for p, _ in sides[0]))
        if bid > ask:
            raise ValueError
    except SmartAPIError:
        raise
    except (ValueError, UnicodeError, OverflowError, struct.error):
        raise SmartAPIError("Malformed SnapQuote identity, timestamp, sequence, prices or bid/ask depth; no usable quote.") from None
    extra = {"bid_qty": sum(q for p, q in sides[1] if p == bid),
             "ask_qty": sum(q for p, q in sides[0] if p == ask)}
    if isinstance(instrument, Contract):
        volume = 0.0 if analytical else struct.unpack_from("<d", packet, 67)[0]
        if not math.isfinite(volume) or volume < 0 or not volume.is_integer():
            raise SmartAPIError("Invalid cumulative traded volume.")
        extra["volume_total"] = integer(int(volume)*instrument.volume_factor, "cumulative traded volume")
    prices = {"bid_units":bid,"ask_units":ask,"ltp_units":ltp,"price_scale":instrument.scale} if isinstance(instrument,Contract) else {"bid_paise":bid,"ask_paise":ask,"ltp_paise":ltp}
    return {"source": "ANGEL_SMARTAPI_WS_SNAP_QUOTE", "exchange": instrument.exchange,
            "symbol": instrument.symbol, "token": instrument.token, "sequence": sequence,
            "timestamp": at.isoformat(), "received_at": now.isoformat(), **prices, **extra}


def check_transport():
    try:
        supported = reconnect is not None and version("websockets") == "17.2"
    except PackageNotFoundError:
        supported = False
    if not supported:
        raise SmartAPIError("Install the optional pinned transport: python -m pip install -r requirements-stream.txt")


def _open_socket(client, timeout):
    """One fixed WSS target; no proxy, redirects, custom URL or generic sends exposed."""
    client._require_session(streaming=True)
    remaining = client._next_allowed.get(STREAM_URL, 0) - time.monotonic()
    if remaining > 0:
        raise RateLimited(math.ceil(remaining))
    check_transport()

    class NoRedirect(reconnect):
        def process_redirect(self, exc):
            return exc  # The transport otherwise follows redirects, including account headers.

    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    logger = logging.Logger("smartapi.stream")
    logger.disabled = True  # The dependency's DEBUG logs can contain handshake credentials.
    logger.propagate = False
    connector = NoRedirect(STREAM_URL, ssl=context, proxy=None, compression=None,
                           additional_headers={"Authorization": client._jwt, "x-api-key": client.settings.api_key,
                                               "x-client-code": client.settings.client_code, "x-feed-token": client._feed_token},
                           open_timeout=min(10, timeout), close_timeout=1, ping_interval=None,
                           max_size=4096, max_queue=4, logger=logger)
    # Call once; don't use the dependency's automatic retry iterator.
    connection = connector.connect()
    connection.pending_legacy_warning = False  # Explicit single-connection use of the pinned transport.
    return connection


def _close_socket(connection):
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass  # Cleanup must never replace a safe error with a raw transport exception.


def _control_message(message, client):
    try:
        data = json.loads(message)
        code = data.get("errorCode", data.get("errorcode")) if isinstance(data, dict) else None
    except (ValueError, TypeError):
        code = None
    if isinstance(code, str) and code in AUTH_CODES:
        client.close()
        raise SessionExpired("WebSocket session invalid or expired; log in locally with a fresh TOTP.")
    if code == "E1002":
        raise SmartAPIError("WebSocket subscription quota exceeded; inspect other connections/subscriptions before retrying.")
    if code == "E1001":
        raise SmartAPIError("SmartAPI rejected the WebSocket subscription; verify the current protocol.")
    raise SmartAPIError("Unexpected WebSocket text/control response; broker details suppressed. No usable quote.")


def _status(client, status, **details):
    return {"event": "status", "status": status, "timestamp": utc(client.clock()).isoformat(), **details}


def _validate_options(max_age, max_reconnects, max_quotes, duration):
    integer(max_age, "maximum quote age", 1, 3600)
    integer(max_reconnects, "maximum reconnects", 0, 5)
    integer(max_quotes, "maximum quotes", 1, 1_000_000)
    integer(duration, "stream duration", 1, 86400)


def _active_session(client, expected):
    client._require_session(streaming=True)
    if client._jwt != expected:
        raise SessionExpired("Session changed; restart streaming after local authentication.")


def stream_quotes(client, instrument, *, max_age=30, max_reconnects=2, max_quotes=10, duration=60, windows=None):
    """Yield JSON-ready health/quote events. Close the iterator if stopping early."""
    instruments = list(instrument) if isinstance(instrument, (list, tuple)) else [instrument]
    if not 1 <= len(instruments) <= 21 or any(not isinstance(c, (Instrument, Contract)) for c in instruments):
        raise SmartAPIError("Streaming requires an instrument validated against the current master.")
    by_wire = {(getattr(c, "exchange_type", 1), c.token): c for c in instruments}
    if len(by_wire) != len(instruments):
        raise SmartAPIError("Duplicate streaming instrument identity.")
    if windows is not None:
        if set(windows)!=set(by_wire) or any(len(w)!=2 or utc(w[0])>=utc(w[1]) for w in windows.values()):
            raise SmartAPIError("Verified exchange session windows are required for every selected instrument.")
    _validate_options(max_age, max_reconnects, max_quotes, duration)
    client._require_session(streaming=True)
    session = client._jwt
    deadline = time.monotonic() + duration
    previous_times, received, retries = {}, 0, 0
    groups = {}
    for (exchange, token) in by_wire:
        groups.setdefault(exchange, []).append(token)
    subscription = json.dumps({"correlationID": "paperdata1", "action": 1,
                               "params": {"mode": 3, "tokenList": [{"exchangeType": e, "tokens": tokens} for e, tokens in groups.items()]}})
    while received < max_quotes and time.monotonic() < deadline:
        if windows is not None and all(utc(client.clock())>=closed for _,closed in windows.values()):
            break
        connection, reason = None, None
        try:
            _active_session(client, session)
            yield _status(client, "CONNECTING")
            if time.monotonic() >= deadline:
                break
            connection = _open_socket(client, max(0.01, deadline - time.monotonic()))
            _active_session(client, session)
            connection.send(subscription)
            connection.send("ping")
            ping_at = connected_at = time.monotonic()
            next_ping, pending_ping = ping_at + PING_INTERVAL, ping_at
            sequences, fingerprints, current_times = {}, {}, {}
            unavailable = set()
            barriers = dict(previous_times)
            yield _status(client, "SUBSCRIBED")
            while received < max_quotes and time.monotonic() < deadline:
                _active_session(client, session)
                instant = time.monotonic()
                if pending_ping is not None and instant - pending_ping >= PONG_TIMEOUT:
                    raise _Interrupted("HEARTBEAT_TIMEOUT")
                if instant >= next_ping and pending_ping is None:
                    connection.send("ping")
                    pending_ping, next_ping = instant, instant + PING_INTERVAL
                if windows is None:
                    if len(current_times) != len(instruments) and instant-connected_at>=max_age:
                        raise _Interrupted("NO_FRESH_QUOTE")
                    if any(not 0 <= (utc(client.clock()) - at).total_seconds() <= max_age for at in current_times.values()):
                        raise _Interrupted("STALE_QUOTE")
                else:
                    wall=utc(client.clock())
                    if all(wall>=closed for _,closed in windows.values()):
                        break
                    for key,(opened,closed) in windows.items():
                        at=current_times.get(key)
                        missing=opened<=wall<closed and ((at is None and (wall-opened).total_seconds()>max_age) or (at is not None and (wall-at).total_seconds()>max_age))
                        if (missing or wall>=closed) and key not in unavailable:
                            unavailable.add(key)
                            c=by_wire[key]
                            yield _status(client,"UNAVAILABLE",exchange=c.exchange,token=c.token,reason="NO_EXECUTABLE_BOOK")
                try:
                    message = connection.recv(timeout=min(1, max(0.01, deadline - instant)))
                except TimeoutError:
                    continue
                _active_session(client, session)
                if time.monotonic() >= deadline:
                    break
                if pending_ping is not None and time.monotonic() - pending_ping >= PONG_TIMEOUT:
                    raise _Interrupted("HEARTBEAT_TIMEOUT")
                if isinstance(message, str):
                    if message == "pong":
                        pending_ping = None
                        continue
                    _control_message(message, client)
                if not isinstance(message, bytes) or len(message) != 379:
                    raise SmartAPIError("Malformed SnapQuote frame.")
                wire = (message[1], message[2:27].partition(b"\0")[0].decode("ascii", errors="replace"))
                if wire not in by_wire:
                    raise SmartAPIError("Unexpected instrument in streaming response.")
                if windows is not None and not windows[wire][0]<=utc(client.clock())<windows[wire][1]:
                    continue
                try:
                    quote = parse_snap_quote(message, by_wire[wire], client.clock(), max_age)
                except _QuoteUnavailable:
                    if windows is None:
                        raise
                    if wire not in unavailable:
                        unavailable.add(wire)
                        c=by_wire[wire]
                        yield _status(client,"UNAVAILABLE",exchange=c.exchange,token=c.token,reason="NO_EXECUTABLE_BOOK")
                    continue
                previous_at = previous_times.get(wire)
                sequence, fingerprint = sequences.get(wire), fingerprints.get(wire)
                barrier = barriers.get(wire)
                suffix="_units" if isinstance(by_wire[wire],Contract) else "_paise"
                signature = (quote["timestamp"], quote["bid"+suffix], quote["ask"+suffix], quote["ltp"+suffix], quote["bid_qty"], quote["ask_qty"], quote.get("volume_total"))
                if sequence is not None:
                    if quote["sequence"] < sequence or (quote["sequence"] == sequence and signature != fingerprint):
                        raise SmartAPIError("Out-of-order or conflicting WebSocket sequence; no usable quote.")
                    if quote["sequence"] == sequence:
                        continue
                sequences[wire], fingerprints[wire] = quote["sequence"], signature
                at = datetime.fromisoformat(quote["timestamp"])
                if barrier is not None and at <= barrier:
                    continue  # Reconnect replay isn't fresh evidence; await a newer exchange timestamp.
                if previous_at is not None and at < previous_at:
                    raise SmartAPIError("Out-of-order WebSocket exchange timestamp; no usable quote.")
                current_times[wire] = previous_times[wire] = at
                unavailable.discard(wire)
                received += 1
                yield {"event": "quote", "status": "LIVE", **quote}
        except InvalidStatus as exc:
            code = exc.response.status_code
            try:
                retry_header = exc.response.headers.get("Retry-After")
            except LookupError:
                retry_header = None
            _close_socket(connection)
            connection = None
            if code == 401:
                client.close()
            yield _status(client, "UNAVAILABLE")
            if code == 401:
                raise SessionExpired("WebSocket authentication failed; log in locally with a fresh TOTP.") from None
            if code == 429 or (code in (500, 502, 503, 504) and retry_header is not None):
                retry = _retry_after(retry_header)
                client._next_allowed[STREAM_URL] = time.monotonic() + retry
                raise RateLimited(retry) from None
            if 300 <= code < 400:
                raise SmartAPIError("WebSocket redirect blocked; no credentials forwarded. Verify the official endpoint.") from None
            if code == 403:
                raise SmartAPIError("WebSocket access denied; check session, API app, feed permission and registered static IPv4 egress.") from None
            if code not in (500, 502, 503, 504):
                raise SmartAPIError("WebSocket handshake rejected; broker details suppressed.") from None
            reason = "SERVICE_UNAVAILABLE"
        except HANDSHAKE_ERRORS:
            _close_socket(connection)
            connection = None
            yield _status(client, "UNAVAILABLE")
            raise SmartAPIError("WebSocket TLS/handshake validation failed; certificate checks cannot be disabled. No data returned.") from None
        except ConnectionClosed as exc:
            codes = (getattr(getattr(exc, "rcvd", None), "code", None),
                     getattr(getattr(exc, "sent", None), "code", None))
            if any(code in (1002, 1003, 1007, 1008, 1009) for code in codes):
                _close_socket(connection)
                connection = None
                yield _status(client, "UNAVAILABLE")
                raise SmartAPIError("WebSocket closed for a protocol, size or access-policy error; broker details suppressed.") from None
            reason = "DISCONNECTED"
        except _Interrupted as exc:
            reason = str(exc)  # Only the fixed internal reason labels above.
        except (OSError, EOFError):
            reason = "CONNECTION_FAILED"
        except SmartAPIError:
            _close_socket(connection)
            connection = None
            yield _status(client, "UNAVAILABLE")
            raise
        finally:
            _close_socket(connection)
        if reason is not None:
            yield _status(client, "UNAVAILABLE", reason=reason)
            if retries >= max_reconnects:
                raise SmartAPIError("WebSocket unavailable; reconnect budget exhausted. No cached quote returned.") from None
            retries += 1
            delay = min(2 ** (retries - 1), 8)
            yield _status(client, "RECONNECTING", retry=retries, delay_seconds=delay)
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
    if received == 0:
        yield _status(client, "UNAVAILABLE", reason="NO_FRESH_QUOTE")
        raise SmartAPIError("Stream ended without a fresh quote; no prices returned.")
    yield _status(client, "STOPPED", quotes_received=received)


def main(argv=None):
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env", help="local literal KEY=VALUE configuration")
    parser.add_argument("--exchange", choices=("NSE",), default="NSE")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--max-quote-age", type=int, default=30)
    parser.add_argument("--count", type=int, default=10, help="stop after this many fresh quotes")
    parser.add_argument("--seconds", type=int, default=60, help="total stream duration, including reconnects")
    parser.add_argument("--max-reconnects", type=int, default=2, help="lifetime reconnect budget, 0 to 5")
    args = parser.parse_args(argv)
    client = None
    try:
        Instrument(args.exchange, args.symbol, args.token, 1)
        _validate_options(args.max_quote_age, args.max_reconnects, args.count, args.seconds)
        check_transport()
        settings = _local_settings(args.env_file)
        instrument = resolve_instrument(_http(MASTER), args.exchange, args.symbol, args.token)
        client = _local_login(settings, streaming=True)
        with closing(client.stream(instrument, max_age=args.max_quote_age, max_reconnects=args.max_reconnects,
                                   max_quotes=args.count, duration=args.seconds)) as events:
            for event in events:
                print(json.dumps(readable(event)), flush=True)
        return 0
    except SmartAPIError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except ValueError:
        print("Error: invalid local stream options or instrument identifiers; use --help.", file=sys.stderr)
        return 1
    except (OSError, getpass.GetPassWarning):
        print("Error: local I/O or hidden credential entry unavailable; use a local interactive terminal.", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled; stream closed.", file=sys.stderr)
        return 130
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
