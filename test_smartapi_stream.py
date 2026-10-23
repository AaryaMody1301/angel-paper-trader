"""Fictional binary and connection fixtures; no broker access or credentials."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import ssl
import struct
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import smartapi_data as api
import smartapi_stream as stream
from smartapi_data import Instrument, SmartAPIError, parse_quote
from smartapi_stream import EPOCH, parse_snap_quote

NOW = datetime(2026, 1, 1, 4, 30, 0, 123000, tzinfo=timezone.utc)
INSTRUMENT = Instrument("NSE", "TEST-EQ", "123456", 5)


def snap_quote(sequence=1, at=NOW, bid=10000, ask=10010):
    packet = bytearray(379)
    struct.pack_into("<BB25sqqq", packet, 0, 3, 1, b"123456", sequence,
                     (at - EPOCH) // timedelta(milliseconds=1), 10005)
    for index in range(10):
        buy = index < 5
        price = bid - index * 5 if buy else ask + (index - 5) * 5
        struct.pack_into("<hqqh", packet, 147 + index * 20, int(buy), 10, price, 2)
    return bytes(packet)


def changed(packet, offset, fmt, *values):
    packet = bytearray(packet)
    struct.pack_into("<" + fmt, packet, offset, *values)
    return bytes(packet)


class SnapQuoteTests(unittest.TestCase):
    def test_wire_flags_prices_and_millisecond_time_match_rest_full(self):
        quote = parse_snap_quote(snap_quote(), INSTRUMENT, NOW)
        self.assertEqual((quote["bid_paise"], quote["ask_paise"], quote["ltp_paise"]), (10000, 10010, 10005))
        self.assertEqual(quote["timestamp"], NOW.isoformat())
        rest = parse_quote({"fetched": [{"exchange": "NSE", "tradingSymbol": "TEST-EQ", "symbolToken": "123456",
                                        "ltp": Decimal("100.05"), "exchFeedTime": "01-Jan-2026 10:00:00",
                                        "exchTradeTime": "01-Jan-2026 09:59:59",
                                        "depth": {"buy": [{"quantity": 10, "orders": 2, "price": Decimal("100.00")}],
                                                  "sell": [{"quantity": 10, "orders": 2, "price": Decimal("100.10")}]}}],
                            "unfetched": []}, INSTRUMENT, NOW)
        for field in ("exchange", "symbol", "token", "bid_paise", "ask_paise", "ltp_paise"):
            self.assertEqual(quote[field], rest[field])

    def test_flags_determine_sides_even_if_records_are_interleaved(self):
        packet = snap_quote()
        records = [packet[offset:offset + 20] for offset in range(147, 347, 20)]
        interleaved = b"".join(records[index] for index in (5, 0, 6, 1, 7, 2, 8, 3, 9, 4))
        quote = parse_snap_quote(packet[:147] + interleaved + packet[347:], INSTRUMENT, NOW)
        self.assertEqual((quote["bid_paise"], quote["ask_paise"]), (10000, 10010))

    def test_modes_identity_lengths_and_token_padding_fail_closed(self):
        packet = snap_quote()
        invalid = [None, "secret", packet[:51], packet[:123], packet[:-1], packet + b"\0", packet * 2,
                   changed(packet, 0, "B", 1), changed(packet, 0, "B", 4), changed(packet, 1, "B", 2),
                   changed(packet, 2, "25s", b"654321"), changed(packet, 2, "25s", b"123456\0garbage"),
                   changed(packet, 2, "25s", b"\xff"), changed(packet, 2, "25s", b"1" * 25)]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(SmartAPIError) as caught:
                parse_snap_quote(value, INSTRUMENT, NOW)
            self.assertNotIn("secret", str(caught.exception))

    def test_stale_future_seconds_instead_of_milliseconds_and_bad_sequence(self):
        packet = snap_quote()
        invalid = [snap_quote(at=NOW - timedelta(seconds=31)), snap_quote(at=NOW + timedelta(milliseconds=1)),
                   changed(packet, 35, "q", int(NOW.timestamp())), changed(packet, 35, "q", 2**63 - 1),
                   changed(packet, 35, "q", -1), changed(packet, 27, "q", -1)]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(SmartAPIError):
                parse_snap_quote(value, INSTRUMENT, NOW)
        self.assertEqual(parse_snap_quote(snap_quote(at=NOW - timedelta(seconds=30)), INSTRUMENT, NOW)["bid_paise"], 10000)

    def test_bad_depth_crossed_empty_and_wrong_tick_never_become_ltp_quotes(self):
        packet = snap_quote()
        invalid = [snap_quote(bid=10100), changed(packet, 147, "h", 2), changed(packet, 147, "h", 0),
                   changed(packet, 149, "q", -1), changed(packet, 157, "q", 0), changed(packet, 157, "q", 9999),
                   changed(packet, 165, "h", -1), changed(packet, 43, "q", 0), changed(packet, 43, "q", -100)]
        empty = bytearray(packet)
        for index in range(5):
            struct.pack_into("<hqqh", empty, 147 + index * 20, 1, 0, 0, 0)
        invalid.append(bytes(empty))
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(SmartAPIError):
                parse_snap_quote(value, INSTRUMENT, NOW)
        # Empty trailing levels are permitted, with their side flags intact.
        one_level = packet
        for index in (1, 2, 3, 4, 6, 7, 8, 9):
            one_level = changed(one_level, 147 + index * 20, "hqqh", int(index < 5), 0, 0, 0)
        self.assertEqual(parse_snap_quote(one_level, INSTRUMENT, NOW)["ask_paise"], 10010)

    def test_dummy_fields_are_ignored_instead_of_treated_as_prices_or_freshness(self):
        packet = bytearray(snap_quote())
        packet[51:147] = b"\xff" * 96
        packet[347:] = b"\xff" * 32
        self.assertEqual(parse_snap_quote(bytes(packet), INSTRUMENT, NOW)["timestamp"], NOW.isoformat())

    def test_protocol_price_quantity_and_sequence_fields_are_eight_bytes(self):
        packet = changed(snap_quote(sequence=2**40), 43, "q", 2**40)
        packet = changed(packet, 149, "q", 2**40)
        quote = parse_snap_quote(packet, INSTRUMENT, NOW)
        self.assertEqual(quote["sequence"], 2**40)
        self.assertEqual(quote["ltp_paise"], 2**40)


class Clock:
    def __init__(self):
        self.instant, self.elapsed, self.delays = NOW, 100.0, []

    def advance(self, seconds):
        self.instant += timedelta(seconds=seconds)
        self.elapsed += seconds

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.advance(seconds)


class Socket:
    def __init__(self, clock, *messages):
        self.clock, self.messages, self.sent, self.closed = clock, list(messages), [], 0

    def send(self, message):
        self.sent.append(message)

    def recv(self, timeout):
        if not self.messages:
            self.clock.advance(timeout)
            raise TimeoutError
        message = self.messages.pop(0)
        if isinstance(message, tuple):
            delay, message = message
            self.clock.advance(delay)
        if callable(message):
            message = message()
        if isinstance(message, BaseException):
            raise message
        return message

    def close(self):
        self.closed += 1


class Closed(Exception):
    def __init__(self, code=None, sent=None):
        super().__init__("secret broker close reason")
        self.rcvd = SimpleNamespace(code=code) if code else None
        self.sent = SimpleNamespace(code=sent) if sent else None


class BadStatus(Exception):
    def __init__(self, code, retry=None):
        super().__init__("secret HTTP headers and body")
        self.response = SimpleNamespace(status_code=code, headers={"Retry-After": retry})


class BadHandshake(Exception):
    pass


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = api.SmartAPIData(api.Settings("secret-key", "TESTCLIENT", "192.0.2.1", "203.0.113.1", "00:11:22:33:44:55"), clock=lambda: self.clock.instant)
        self.addCleanup(self.client.close)
        with patch.object(api, "_http", return_value={"jwtToken": "secret.jwt.token", "feedToken": "secret-feed", "refreshToken": "secret-refresh"}):
            self.client.login("1234", "123456", streaming=True)
        self.real_open = stream._open_socket
        for target, value in [("ConnectionClosed", Closed), ("InvalidStatus", BadStatus),
                              ("HANDSHAKE_ERRORS", (ssl.SSLError, BadHandshake))]:
            patcher = patch.object(stream, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.open_patch = patch.object(stream, "_open_socket")
        self.open = self.open_patch.start()
        self.addCleanup(self.open_patch.stop)
        for target, value in [("monotonic", lambda: self.clock.elapsed), ("sleep", self.clock.sleep)]:
            patcher = patch.object(stream.time, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def events(self, **kwargs):
        return self.client.stream(INSTRUMENT, **kwargs)

    def failing_events(self, error=SmartAPIError, **kwargs):
        events = []
        with self.assertRaises(error) as caught:
            events.extend(self.events(**kwargs))
        self.assertNotIn("secret", str(caught.exception))
        return events, caught.exception

    def test_subscription_text_heartbeat_duplicates_sequence_gaps_and_clean_stop(self):
        socket = Socket(self.clock, "pong", snap_quote(), snap_quote(), (0.2, snap_quote(sequence=5, at=NOW + timedelta(milliseconds=200))))
        self.open.return_value = socket
        events = list(self.events(max_quotes=2))
        quotes = [event for event in events if event["event"] == "quote"]
        self.assertEqual([quote["sequence"] for quote in quotes], [1, 5])
        self.assertTrue(all(quote["status"] == "LIVE" for quote in quotes))
        self.assertEqual(events[-1]["status"], "STOPPED")
        self.assertEqual(socket.closed, 1)
        self.assertEqual(json.loads(socket.sent[0]), {"correlationID": "paperdata1", "action": 1,
                         "params": {"mode": 3, "tokenList": [{"exchangeType": 1, "tokens": ["123456"]}]}})
        self.assertEqual(socket.sent[1:], ["ping"])
        self.assertNotIn("secret", json.dumps(events))

    def test_reconnect_closes_old_socket_resubscribes_and_waits_for_new_feed_time(self):
        first = Socket(self.clock, "pong", snap_quote(sequence=10), Closed())
        second = Socket(self.clock, "pong", snap_quote(sequence=0),
                        snap_quote(sequence=1, at=NOW - timedelta(milliseconds=1)),
                        (1, snap_quote(sequence=2, at=NOW + timedelta(seconds=2))))
        def opening(*_):
            if self.open.call_count == 1:
                return first
            self.assertEqual(first.closed, 1)
            return second
        self.open.side_effect = opening
        events = list(self.events(max_quotes=2))
        quotes = [event for event in events if event["event"] == "quote"]
        self.assertEqual([quote["sequence"] for quote in quotes], [10, 2])
        self.assertGreater(quotes[1]["timestamp"], quotes[0]["timestamp"])
        self.assertEqual(first.sent[0], second.sent[0])
        self.assertEqual(second.closed, 1)
        self.assertEqual(self.clock.delays, [1])
        self.assertIn("RECONNECTING", [event["status"] for event in events])

    def test_retry_budget_is_not_reset_by_receiving_a_quote(self):
        sockets = [Socket(self.clock, "pong", snap_quote(at=NOW + timedelta(seconds=offset)), Closed()) for offset in (0, 1, 3)]
        self.open.side_effect = sockets
        events, error = self.failing_events(max_quotes=100, max_reconnects=2)
        self.assertIn("budget exhausted", str(error))
        self.assertEqual(self.open.call_count, 3)
        self.assertEqual(self.clock.delays, [1, 2])
        self.assertTrue(all(socket.closed == 1 for socket in sockets))
        self.assertEqual(len([event for event in events if event["event"] == "quote"]), 3)

    def test_heartbeat_loss_and_pongs_without_data_do_not_produce_cached_prices(self):
        for messages, max_age, reason, count in [( [snap_quote()], 30, "HEARTBEAT_TIMEOUT", 1),
                                                 (["pong"], 3, "NO_FRESH_QUOTE", 0),
                                                 (["pong", snap_quote(), (4, "pong")], 3, "STALE_QUOTE", 1)]:
            with self.subTest(reason=reason):
                self.clock.instant, self.clock.elapsed = NOW, 100
                socket = Socket(self.clock, *messages)
                self.open.return_value = socket
                events, _ = self.failing_events(max_quotes=100, max_age=max_age, max_reconnects=0)
                self.assertEqual(len([event for event in events if event["event"] == "quote"]), count)
                self.assertEqual(events[-1]["reason"], reason)
                self.assertEqual(socket.closed, 1)

    def test_application_ping_repeats_with_pongs_without_refreshing_quote_time(self):
        socket = Socket(self.clock, "pong", snap_quote(), (20, "pong"), "pong", (0.1, snap_quote(sequence=2, at=NOW + timedelta(seconds=20.1))))
        self.open.return_value = socket
        events = list(self.events(max_quotes=2))
        self.assertEqual(socket.sent[1:], ["ping", "ping"])
        self.assertEqual(events[-1]["status"], "STOPPED")

    def test_out_of_order_and_conflicting_sequence_and_feed_timestamps_are_terminal(self):
        invalid = [snap_quote(sequence=0), changed(snap_quote(), 43, "q", 10010),
                   snap_quote(sequence=2, at=NOW - timedelta(milliseconds=1))]
        for packet in invalid:
            with self.subTest(packet=packet):
                socket = Socket(self.clock, "pong", snap_quote(), packet)
                self.open.reset_mock()
                self.open.return_value = socket
                events, _ = self.failing_events(max_quotes=100)
                self.assertEqual(len([event for event in events if event["event"] == "quote"]), 1)
                self.assertEqual(self.open.call_count, 1)
                self.assertEqual(socket.closed, 1)

    def test_new_sequence_can_update_prices_in_the_same_millisecond(self):
        socket = Socket(self.clock, "pong", snap_quote(), snap_quote(sequence=2, bid=10005, ask=10015))
        self.open.return_value = socket
        quotes = [event for event in self.events(max_quotes=2) if event["event"] == "quote"]
        self.assertEqual([quote["bid_paise"] for quote in quotes], [10000, 10005])
        self.assertEqual(quotes[0]["timestamp"], quotes[1]["timestamp"])

    def test_auth_quota_redirect_policy_and_tls_errors_do_not_retry_or_expose_details(self):
        errors = [BadStatus(403), BadStatus(302), BadStatus(400), BadHandshake("secret"),
                  ssl.SSLCertVerificationError("secret"), Closed(1008), Closed(sent=1009)]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.open.reset_mock()
                self.open.side_effect = error
                self.failing_events(max_reconnects=5)
                self.assertEqual(self.open.call_count, 1)
                self.assertEqual(self.clock.delays, [])

    def test_rate_limit_honours_server_cooldown_without_a_reconnect(self):
        for status in (429, 503):
            with self.subTest(status=status):
                self.open.reset_mock()
                self.open.side_effect = BadStatus(status, "7")
                _, error = self.failing_events(error=api.RateLimited)
                self.assertEqual(error.retry_after, 7)
                self.assertEqual(self.open.call_count, 1)
                with self.assertRaises(api.RateLimited) as caught:
                    self.real_open(self.client, 1)
                self.assertEqual(caught.exception.retry_after, 7)

    def test_transient_service_error_without_retry_after_can_recover(self):
        socket = Socket(self.clock, "pong", snap_quote(at=NOW + timedelta(seconds=1)))
        self.open.side_effect = [BadStatus(503), socket]
        events = list(self.events(max_quotes=1))
        self.assertEqual(events[-1]["status"], "STOPPED")
        self.assertEqual(self.clock.delays, [1])
        self.assertEqual(socket.closed, 1)

    def test_invalid_handshake_session_clears_both_tokens(self):
        self.open.side_effect = BadStatus(401)
        self.failing_events(error=api.SessionExpired)
        self.assertIsNone(self.client._jwt)
        self.assertIsNone(self.client._feed_token)

    def test_server_auth_and_subscription_text_are_sanitised_and_terminal(self):
        for code, error in [("AG8002", api.SessionExpired), ("E1001", SmartAPIError), ("E1002", SmartAPIError), ("unknown", SmartAPIError)]:
            with self.subTest(code=code):
                self.client._jwt, self.client._feed_token = "secret.jwt.token", "secret-feed"
                self.client._expires = NOW + timedelta(hours=1)
                socket = Socket(self.clock, json.dumps({"errorCode": code, "errorMessage": "secret"}))
                self.open.reset_mock()
                self.open.return_value = socket
                self.failing_events(error=error)
                self.assertEqual(self.open.call_count, 1)
                self.assertEqual(socket.closed, 1)

    def test_midnight_expiry_during_receive_stops_before_emitting_the_packet(self):
        self.clock.instant = NOW.replace(hour=18, minute=29, second=59, microsecond=900000)
        socket = Socket(self.clock, (0.2, snap_quote(at=self.clock.instant + timedelta(seconds=0.2))))
        self.open.return_value = socket
        events, _ = self.failing_events(error=api.SessionExpired)
        self.assertFalse(any(event["event"] == "quote" for event in events))
        self.assertEqual(socket.closed, 1)
        self.assertIsNone(self.client._feed_token)

    def test_iterator_close_and_session_disposal_close_the_socket(self):
        socket = Socket(self.clock, "pong", snap_quote())
        self.open.return_value = socket
        events = self.events()
        while next(events)["event"] != "quote":
            pass
        events.close()
        self.assertEqual(socket.closed, 1)
        socket = Socket(self.clock, "pong", snap_quote())
        self.open.return_value = socket
        events = self.events()
        while next(events)["event"] != "quote":
            pass
        self.client.close()
        with self.assertRaises(api.SessionExpired):
            list(events)
        self.assertEqual(socket.closed, 1)

    def test_duration_and_invalid_limits_terminate_without_fabricating_quotes(self):
        socket = Socket(self.clock, "pong")
        self.open.return_value = socket
        self.failing_events(duration=2)
        self.assertEqual(socket.closed, 1)
        self.open.reset_mock()
        for options in ({"max_age": 0}, {"max_reconnects": 6}, {"max_quotes": 0}, {"duration": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                list(self.events(**options))
        self.open.assert_not_called()

    def test_receive_cannot_revive_a_late_heartbeat_or_emit_after_the_duration(self):
        self.open.return_value = Socket(self.clock, (11, "pong"))
        events, _ = self.failing_events(max_reconnects=0)
        self.assertEqual(events[-1]["reason"], "HEARTBEAT_TIMEOUT")
        self.clock.instant, self.clock.elapsed = NOW, 100
        self.open.return_value = Socket(self.clock, (3, snap_quote(at=NOW + timedelta(seconds=3))))
        events, _ = self.failing_events(duration=2)
        self.assertFalse(any(event["event"] == "quote" for event in events))

    def test_transport_errors_have_no_raw_credentials_in_tracebacks(self):
        self.open.side_effect = OSError("secret key and token")
        try:
            list(self.events(max_reconnects=0))
            self.fail("expected connection failure")
        except SmartAPIError:
            self.assertNotIn("secret", traceback.format_exc())


class LoginAndTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = api.SmartAPIData(api.Settings("secret-key", "TESTCLIENT", "192.0.2.1", "203.0.113.1", "00:11:22:33:44:55"), clock=lambda: NOW)
        self.addCleanup(self.client.close)

    def login(self, data=None, streaming=True):
        with patch.object(api, "_http", return_value=data or {"jwtToken": "secret.jwt.token", "feedToken": "secret-feed"}):
            self.client.login("1234", "123456", streaming=streaming)

    def test_feed_token_is_opt_in_memory_only_validated_and_cleared_on_close(self):
        self.login(streaming=False)
        with self.assertRaises(api.SessionExpired):
            list(self.client.stream(INSTRUMENT))
        for token in (None, "", True, "secret\r\nInjected: header"):
            self.client._next_allowed.clear()
            with self.subTest(token=token), self.assertRaisesRegex(SmartAPIError, "no valid streaming feed token"):
                self.login({"jwtToken": "secret.jwt.token", "feedToken": token})
            self.assertIsNone(self.client._jwt)
            self.assertIsNone(self.client._feed_token)
        self.client._next_allowed.clear()
        self.login()
        self.assertEqual(self.client._feed_token, "secret-feed")
        self.assertNotIn("secret", repr(self.client))
        self.client.close()
        self.assertIsNone(self.client._feed_token)

    def test_missing_or_wrong_optional_transport_fails_before_connection(self):
        self.login()
        with patch.object(stream, "reconnect", None), self.assertRaisesRegex(SmartAPIError, "requirements-stream.txt"):
            stream._open_socket(self.client, 3)
        with patch.object(stream, "version", return_value="99.0"), self.assertRaisesRegex(SmartAPIError, "requirements-stream.txt"):
            stream.check_transport()

    @unittest.skipIf(stream.reconnect is None, "optional transport not installed; run tests in .venv for transport checks")
    def test_actual_transport_is_fixed_tls_verified_bounded_and_does_not_log_credentials(self):
        self.login()
        captured = []
        socket = Mock()
        def connect(connector):
            captured.append(connector)
            return socket
        with patch.object(stream.reconnect, "connect", connect):
            self.assertIs(stream._open_socket(self.client, 4), socket)
        connector = captured[0]
        self.assertEqual(connector.uri, stream.STREAM_URL)
        self.assertIsNone(connector.proxy)
        self.assertEqual(connector.ssl.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(connector.ssl.check_hostname)
        self.assertGreaterEqual(connector.ssl.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(connector.open_timeout, 4)
        self.assertEqual(connector.connection_kwargs["close_timeout"], 1)
        self.assertIsNone(connector.connection_kwargs["ping_interval"])
        self.assertEqual(connector.connection_kwargs["max_queue"], 4)
        self.assertEqual(connector.protocol_kwargs["max_size"], 4096)
        self.assertIsNone(connector.protocol_kwargs["extensions"])
        self.assertTrue(connector.logger.disabled)
        self.assertFalse(connector.logger.propagate)
        self.assertEqual(connector.additional_headers, {"Authorization": "secret.jwt.token", "x-api-key": "secret-key",
                                                       "x-client-code": "TESTCLIENT", "x-feed-token": "secret-feed"})

    @unittest.skipIf(stream.reconnect is None, "optional transport not installed; run tests in .venv for transport checks")
    def test_real_transport_redirect_policy_rejects_same_and_cross_host_before_second_handshake(self):
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        self.login()
        for location in ("/other", "https://attacker.example/order", "wss://attacker.example/stream"):
            response = Response(302, "Found", Headers({"Location": location}), bytearray(b"secret"))
            error = stream.InvalidStatus(response)
            with self.subTest(location=location), patch.object(stream.reconnect, "open_connection", side_effect=error) as opening, \
                    self.assertRaises(stream.InvalidStatus):
                stream._open_socket(self.client, 4)
            self.assertEqual(opening.call_count, 1)


class StreamCliTests(unittest.TestCase):
    def setUp(self):
        self.settings = api.Settings("secret-key", "TESTCLIENT", "192.0.2.1", "203.0.113.1", "00:11:22:33:44:55")
        self.client = api.SmartAPIData(self.settings, clock=lambda: NOW)
        self.addCleanup(self.client.close)
        self.arguments = ["--symbol", "TEST-EQ", "--token", "123456", "--count", "1"]
        self.master = [{"exch_seg": "NSE", "symbol": "TEST-EQ", "token": "123456", "lotsize": "1",
                        "instrumenttype": "", "expiry": "", "tick_size": "5.000000"}]

    def test_local_cli_flow_has_live_status_jsonl_safe_credentials_and_no_journal(self):
        clock = Clock()
        socket = Socket(clock, "pong", snap_quote())
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(stream, "check_transport"), patch.object(api.Settings, "load", return_value=self.settings), \
                patch.object(api.sys.stdin, "isatty", return_value=True), patch.object(stream, "_http", return_value=self.master) as master, \
                patch.object(api, "SmartAPIData", return_value=self.client), patch.object(api, "_http", return_value={"jwtToken": "secret.jwt.token", "feedToken": "secret-feed"}) as http, \
                patch.object(api.getpass, "getpass", side_effect=["1234", "123456"]), patch.object(stream, "_open_socket", return_value=socket), \
                patch("paper_trader.PaperAccount") as paper, redirect_stdout(output), redirect_stderr(errors):
            result = stream.main(self.arguments)
        self.assertEqual(result, 0)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([event["status"] for event in events], ["CONNECTING", "SUBSCRIBED", "LIVE", "STOPPED"])
        self.assertEqual(events[2]["ask_inr"], "100.10")
        self.assertNotIn("secret", output.getvalue() + errors.getvalue())
        self.assertEqual(master.call_args.args, (api.MASTER,))
        self.assertEqual(http.call_args.args[0], api.LOGIN)
        self.assertEqual(socket.closed, 1)
        self.assertIsNone(self.client._jwt)
        self.assertIsNone(self.client._feed_token)
        paper.assert_not_called()

    def test_cli_interrupt_closes_iterator_and_disposes_session(self):
        closed = []
        def events(*_, **__):
            try:
                yield {"event": "status", "status": "SUBSCRIBED"}
                raise KeyboardInterrupt
            finally:
                closed.append(True)
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(stream, "check_transport"), patch.object(stream, "_local_settings", return_value=self.settings), \
                patch.object(stream, "_http", return_value=self.master), patch.object(stream, "_local_login", return_value=self.client), \
                patch.object(self.client, "stream", events), redirect_stdout(output), redirect_stderr(errors):
            result = stream.main(self.arguments)
        self.assertEqual(result, 130)
        self.assertEqual(closed, [True])
        self.assertIn("Cancelled", errors.getvalue())

    def test_missing_dependency_invalid_options_and_pasted_secrets_stop_before_network(self):
        for arguments, dependency_error in [(self.arguments, SmartAPIError("Install requirements-stream.txt")),
                                            ([*self.arguments, "--max-reconnects", "6"], None)]:
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(stream, "check_transport", side_effect=dependency_error), patch.object(stream, "_http") as http, \
                    patch.object(stream, "_local_login") as login, redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(stream.main(arguments), 1)
            http.assert_not_called()
            login.assert_not_called()
            self.assertEqual(output.getvalue(), "")
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
            stream.main([*self.arguments, "--feed-token", "secret"])
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn("secret", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
