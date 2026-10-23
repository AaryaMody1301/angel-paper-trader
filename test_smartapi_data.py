"""Fictional protocol fixtures only. Every HTTP connection is mocked."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import ssl
import tempfile
import traceback
import unittest
from unittest.mock import patch

import smartapi_data as api

NOW = datetime(2026, 1, 1, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
MASTER_ROW = {"exch_seg": "NSE", "symbol": "TEST-EQ", "token": "123456",
              "instrumenttype": "", "expiry": "", "lotsize": "1", "tick_size": "5.000000"}


def quote_data():
    return {"fetched": [{"exchange": "NSE", "tradingSymbol": "TEST-EQ", "symbolToken": "123456",
                         "ltp": Decimal("100.05"), "exchFeedTime": "01-Jan-2026 10:00:00",
                         "exchTradeTime": "01-Jan-2026 09:59:59",
                         "depth": {"buy": [{"price": Decimal("99.95"), "quantity": 10, "orders": 2},
                                           {"price": Decimal("100.00"), "quantity": 5, "orders": 1}],
                                   "sell": [{"price": Decimal("100.15"), "quantity": 8, "orders": 1},
                                            {"price": Decimal("100.10"), "quantity": 9, "orders": 3}]}}],
            "unfetched": []}


class Response(io.BytesIO):
    def __init__(self, data, status=200, retry=None):
        super().__init__(data if isinstance(data, bytes) else json.dumps(data, default=str).encode())
        self.status, self.retry = status, retry

    def getheader(self, name):
        return self.retry if name == "Retry-After" else None


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.instrument = api.resolve_instrument([deepcopy(MASTER_ROW)], "NSE", "TEST-EQ", "123456")

    def test_identity_and_master_tick_units(self):
        self.assertEqual(self.instrument.tick_paise, 5)
        for rows, exchange, symbol, token in [([], "NSE", "TEST-EQ", "123456"),
                                             ([MASTER_ROW], "NSE", "WRONG-EQ", "123456"),
                                             ([MASTER_ROW], "NSE", "TEST-EQ", "999999"),
                                             ([MASTER_ROW, MASTER_ROW], "NSE", "TEST-EQ", "123456"),
                                             ([MASTER_ROW], "BSE", "TEST-EQ", "123456"),
                                             ([None], "NSE", "TEST-EQ", "123456")]:
            with self.subTest(rows=rows, exchange=exchange, symbol=symbol, token=token), self.assertRaises(api.SmartAPIError):
                api.resolve_instrument(rows, exchange, symbol, token)
        for field, value in [("tick_size", "0.5"), ("tick_size", "0"), ("tick_size", "NaN"),
                             ("tick_size", "1e999999"),
                             ("lotsize", "50"), ("instrumenttype", "OPTIDX"), ("expiry", "31DEC2026")]:
            with self.subTest(field=field, value=value), self.assertRaises(api.SmartAPIError):
                api.resolve_instrument([{**MASTER_ROW, field: value}], "NSE", "TEST-EQ", "123456")

    def test_full_quote_uses_buy_bid_sell_ask_and_exact_inr_scaling(self):
        quote = api.parse_quote(quote_data(), self.instrument, NOW)
        self.assertEqual((quote["bid_paise"], quote["ask_paise"], quote["ltp_paise"]), (10000, 10010, 10005))
        self.assertEqual(quote["timestamp"], NOW.isoformat())
        self.assertEqual(api.readable(quote)["ask_inr"], "100.10")
        # An old last trade does not make a newly updated order book stale.
        data = quote_data()
        data["fetched"][0]["exchTradeTime"] = "31-Dec-2025 15:00:00"
        self.assertEqual(api.parse_quote(data, self.instrument, NOW)["bid_paise"], 10000)

    def test_bad_identity_partial_response_and_unavailable_depth_fail_closed(self):
        changes = [("exchange", "NFO"), ("tradingSymbol", "OTHER-EQ"), ("symbolToken", "999999"),
                   ("depth", None), ("depth", {"buy": [], "sell": []}),
                   ("depth", {"buy": [{"price": 0, "quantity": 0, "orders": 0}],
                              "sell": [{"price": 0, "quantity": 0, "orders": 0}]}),
                   ("ltp", "100.001"), ("ltp", True), ("ltp", "NaN")]
        for field, value in changes:
            data = quote_data()
            data["fetched"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(api.SmartAPIError):
                api.parse_quote(data, self.instrument, NOW)
        for data in (None, {}, {"fetched": [], "unfetched": []},
                     {"fetched": quote_data()["fetched"], "unfetched": [{"error": "unavailable"}]},
                     {"fetched": quote_data()["fetched"] * 2, "unfetched": []}):
            with self.subTest(data=data), self.assertRaises(api.SmartAPIError):
                api.parse_quote(data, self.instrument, NOW)

    def test_stale_future_invalid_and_inconsistent_exchange_times(self):
        for at in (NOW + timedelta(seconds=31), NOW - timedelta(seconds=1)):
            with self.subTest(now=at), self.assertRaisesRegex(api.SmartAPIError, "stale or future"):
                api.parse_quote(quote_data(), self.instrument, at)
        self.assertEqual(api.parse_quote(quote_data(), self.instrument, NOW + timedelta(seconds=30))["bid_paise"], 10000)
        for field, value in [("exchFeedTime", ""), ("exchFeedTime", "01-Jan-2026 25:00:00"),
                             ("exchFeedTime", "01-XXX-2026 10:00:00"), ("exchTradeTime", "01-Jan-2026 10:00:01")]:
            data = quote_data()
            data["fetched"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(api.SmartAPIError):
                api.parse_quote(data, self.instrument, NOW)

    def test_crossed_depth_and_bad_quantities_are_rejected(self):
        for field, value in [("price", "101"), ("price", "99.99"), ("quantity", -1), ("quantity", True), ("orders", "1")]:
            data = quote_data()
            data["fetched"][0]["depth"]["buy"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(api.SmartAPIError):
                api.parse_quote(data, self.instrument, NOW)

    def test_history_uses_ist_requests_and_aware_sorted_candles_in_paise(self):
        start = NOW - timedelta(minutes=1)
        request = api.history_request(self.instrument, "ONE_MINUTE", start, NOW, NOW)
        self.assertEqual(request["fromdate"], "2026-01-01 09:59")
        rows = [["2026-01-01T09:59:00+05:30", "100", "101", "99", "100.05", 123]]
        candle = api.parse_candles(rows, start, NOW, NOW)[0]
        self.assertEqual(candle["timestamp"], start.isoformat())
        self.assertEqual(candle["close_paise"], 10005)
        self.assertEqual(candle["volume"], 123)
        # Historical ticks can differ from today's master; exact paise still apply.
        prior_tick_rows = [[rows[0][0], "100.01", "101", "99", "100.02", 123]]
        self.assertEqual(api.parse_candles(prior_tick_rows, start, NOW, NOW)[0]["close_paise"], 10002)
        for interval, begin, end in [("TICK", start, NOW), ("ONE_MINUTE", NOW, start),
                                     ("ONE_MINUTE", start, NOW + timedelta(minutes=1)),
                                     ("ONE_MINUTE", NOW - timedelta(days=31), NOW),
                                     ("ONE_MINUTE", start.replace(tzinfo=None), NOW),
                                     ("ONE_MINUTE", start + timedelta(seconds=1), NOW)]:
            with self.subTest(interval=interval, begin=begin, end=end), self.assertRaises(ValueError):
                api.history_request(self.instrument, interval, begin, end, NOW)
        for bad in ([], [rows[0], rows[0]], [["2026-01-01T09:59:00", *rows[0][1:]]],
                    [["2026-01-01T09:58:00+05:30", *rows[0][1:]]],
                    [[rows[0][0], "100", "99", "98", "100", 1]],
                    [[*rows[0][:-1], -1]], [[*rows[0][:-1], True]], [[rows[0][0], "100.001", *rows[0][2:]]]):
            with self.subTest(rows=bad), self.assertRaises(api.SmartAPIError):
                api.parse_candles(bad, start, NOW, NOW)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.factory_patch = patch("smartapi_data.HTTPSConnection")
        self.factory = self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
        self.connection = self.factory.return_value

    def respond(self, data, status=200, retry=None):
        self.connection.getresponse.return_value = Response(data, status, retry)

    def test_tls_timeout_fixed_target_and_public_master_without_account_headers(self):
        self.respond([MASTER_ROW])
        self.assertEqual(api._http(api.MASTER, headers={"Authorization": "secret"}), [MASTER_ROW])
        args, kwargs = self.factory.call_args
        self.assertEqual(args, ("margincalculator.angelone.in",))
        self.assertEqual(kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kwargs["context"].check_hostname)
        self.assertGreaterEqual(kwargs["context"].minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertEqual(kwargs["timeout"], 15)
        self.assertEqual(self.connection.request.call_args.kwargs["headers"], {"Accept": "application/json"})
        self.connection.close.assert_called_once()

    def test_account_mutations_unknown_hosts_methods_and_encoded_paths_block_before_io(self):
        paths = ["/rest/secure/angelbroking/order/v1/" + name for name in
                 ("placeOrder", "modifyOrder", "cancelOrder", "convertPosition", "getOrderBook")]
        paths += [prefix + name for prefix in ("/gtt-service/rest/secure/angelbroking/gtt/v1/", "/rest/secure/angelbroking/gtt/v1/")
                  for name in ("createRule", "modifyRule", "cancelRule", "ruleList")]
        paths += ["/rest/secure/angelbroking/edis/v1/generateTPIN", api.QUOTE[2] + "?redirect=placeOrder",
                  api.QUOTE[2] + "/../order/v1/placeOrder", api.QUOTE[2] + "%2f..%2forder/v1/placeOrder"]
        blocked = [("POST", api.QUOTE[1], path) for path in paths]
        blocked += [("DELETE", *api.QUOTE[1:]), ("GET", *api.LOGIN[1:]),
                    ("POST", "attacker.example", api.QUOTE[2]), ("POST", api.QUOTE[1] + ":443", api.QUOTE[2])]
        for endpoint in blocked:
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(api.SmartAPIError, "Endpoint blocked"):
                api._http(endpoint, {"quantity": 1})
        self.factory.assert_not_called()

    def test_redirect_is_not_followed_or_echoed(self):
        self.respond(b"secret redirect destination", 302)
        with self.assertRaisesRegex(api.SmartAPIError, "redirect blocked") as caught:
            api._http(api.QUOTE)
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(self.connection.request.call_count, 1)

    def test_rate_limits_include_plain_403_json_error_and_retry_after(self):
        for data, status, retry, expected in [(b"Access denied because of exceeding access rate", 403, None, 60),
                                             ({"status": False, "errorcode": "AB1021"}, 200, "7", 7),
                                             (b"secret", 429, "secret", 60),
                                             ({"status": False, "message": "Too many requests"}, 200, "3", 3)]:
            self.respond(data, status, retry)
            with self.subTest(status=status, data=data), self.assertRaises(api.RateLimited) as caught:
                api._http(api.HISTORY)
            self.assertEqual(caught.exception.retry_after, expected)

    def test_expired_auth_and_other_failures_do_not_expose_broker_details(self):
        for data, status, error in [({"status": False, "errorcode": "AG8002", "message": "secret"}, 200, api.SessionExpired),
                                    (b"secret", 401, api.SessionExpired), (b"secret", 403, api.SmartAPIError),
                                    (b"secret", 500, api.SmartAPIError), (b"secret", 200, api.SmartAPIError),
                                    ({"status": False, "message": "secret", "errorcode": "secret"}, 200, api.SmartAPIError),
                                    ({"status": "true", "data": "secret"}, 200, api.SmartAPIError),
                                    ({"status": False, "errorcode": {"secret": 1}}, 200, api.SmartAPIError),
                                    (b'{"status":true,"data":NaN}', 200, api.SmartAPIError)]:
            self.respond(data, status)
            with self.subTest(status=status, data=data), self.assertRaises(error) as caught:
                api._http(api.QUOTE)
            self.assertNotIn("secret", str(caught.exception))

    def test_disconnect_timeout_and_tls_errors_have_safe_tracebacks(self):
        for error in (OSError("secret headers and body"), TimeoutError("secret token"),
                      api.HTTPException("secret connection"), ssl.SSLCertVerificationError("secret certificate")):
            self.connection.request.side_effect = error
            try:
                api._http(api.QUOTE)
                self.fail("network failure must not return data")
            except api.SmartAPIError:
                self.assertNotIn("secret", traceback.format_exc())

    def test_response_size_is_bounded(self):
        self.respond(b"x" * (8 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(api.SmartAPIError, "size limit"):
            api._http(api.QUOTE)

    def test_numeric_json_prices_stay_decimal_and_retry_after_supports_http_dates(self):
        self.respond(b'{"status":true,"data":{"price":100.05}}')
        self.assertEqual(api._http(api.QUOTE)["price"], Decimal("100.05"))
        self.assertEqual(api._retry_after("Thu, 01 Jan 2026 04:32:00 GMT", NOW), 120)
        self.assertEqual(api._retry_after("7200", NOW), 7200)
        for invalid in (None, "secret", "-100", "Thu, 01 Jan 2026 04:32:00"):
            self.assertEqual(api._retry_after(invalid, NOW), 60)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.now, self.monotonic = NOW, 100.0
        self.http_patch = patch("smartapi_data._http")
        self.http = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.time_patch = patch("smartapi_data.time.monotonic", lambda: self.monotonic)
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.settings = api.Settings("secret-api-key", "TESTCLIENT", "192.0.2.1", "203.0.113.1", "00:11:22:33:44:55")
        self.client = api.SmartAPIData(self.settings, clock=lambda: self.now)
        self.addCleanup(self.client.close)
        self.instrument = api.resolve_instrument([MASTER_ROW], "NSE", "TEST-EQ", "123456")

    def login(self):
        self.http.return_value = {"jwtToken": "secret.jwt.token", "refreshToken": "secret-refresh", "feedToken": "secret-feed"}
        self.assertIsNone(self.client.login("1234", "123456"))

    def test_login_headers_memory_only_tokens_and_local_disposal(self):
        with self.assertRaises(api.SessionExpired):
            self.client.quote(self.instrument)
        self.http.assert_not_called()
        self.login()
        endpoint, body, headers = self.http.call_args.args
        self.assertEqual(endpoint, api.LOGIN)
        self.assertEqual(body, {"clientcode": "TESTCLIENT", "password": "1234", "totp": "123456"})
        self.assertNotIn("Authorization", headers)
        self.http.return_value = quote_data()
        self.client.quote(self.instrument)
        self.assertEqual(self.http.call_args.args[2]["Authorization"], "Bearer secret.jwt.token")
        self.assertNotIn("secret", repr(self.client))
        self.assertNotIn("secret-refresh", repr(vars(self.client)))
        self.assertNotIn("secret-feed", repr(vars(self.client)))
        count = self.http.call_count
        self.client.close()
        self.assertIsNone(self.client._jwt)
        self.assertEqual(self.http.call_count, count)  # No remote logout/session interference.

    def test_invalid_login_input_and_malformed_response_leave_no_session(self):
        for pin, totp in [("", "123456"), ("secret\n", "123456"), ("1234", "secret"), ("1234", "１２３４５６")]:
            with self.subTest(pin=pin, totp=totp), self.assertRaises(api.SmartAPIError) as caught:
                self.client.login(pin, totp)
            self.assertNotIn("secret", str(caught.exception))
        self.http.assert_not_called()
        for data in (None, {}, {"jwtToken": "secret with newline\r\n"}, {"jwtToken": True}):
            self.monotonic += 1
            self.http.return_value = data
            with self.subTest(data=data), self.assertRaises(api.SmartAPIError) as caught:
                self.client.login("1234", "123456")
            self.assertNotIn("secret", str(caught.exception))
            self.assertIsNone(self.client._jwt)

    def test_midnight_ist_and_broker_expiry_require_explicit_reauthentication(self):
        self.login()
        self.now = datetime(2026, 1, 1, 18, 30, tzinfo=timezone.utc)
        count = self.http.call_count
        with self.assertRaisesRegex(api.SessionExpired, "daily session expired"):
            self.client.quote(self.instrument)
        self.assertEqual(self.http.call_count, count)
        self.monotonic += 1
        self.login()
        self.http.side_effect = api.SessionExpired("Session invalid or expired.")
        with self.assertRaises(api.SessionExpired):
            self.client.quote(self.instrument)
        count = self.http.call_count
        with self.assertRaises(api.SessionExpired):
            self.client.quote(self.instrument)
        self.assertEqual(self.http.call_count, count)
        self.assertIsNone(self.client._jwt)

    def test_pacing_server_cooldown_and_recovery_without_hidden_retries(self):
        self.login()
        self.http.return_value = quote_data()
        first = self.client.quote(self.instrument)
        count = self.http.call_count
        with self.assertRaises(api.RateLimited) as caught:
            self.client.quote(self.instrument)
        self.assertEqual(caught.exception.retry_after, 1)
        self.assertEqual(self.http.call_count, count)
        self.monotonic += 1
        self.assertEqual(self.client.quote(self.instrument), first)
        self.monotonic += 1
        self.http.side_effect = api.RateLimited(7)
        with self.assertRaises(api.RateLimited):
            self.client.quote(self.instrument)
        count = self.http.call_count
        self.monotonic += 6
        with self.assertRaises(api.RateLimited):
            self.client.quote(self.instrument)
        self.assertEqual(self.http.call_count, count)
        self.monotonic += 1
        self.http.side_effect = None
        self.assertEqual(self.client.quote(self.instrument), first)

    def test_failed_disconnect_stale_and_out_of_order_updates_never_return_last_good_quote(self):
        self.login()
        self.http.return_value = quote_data()
        self.client.quote(self.instrument)
        self.monotonic += 1
        self.http.side_effect = api.SmartAPIError("Connection failed or disconnected; no data returned.")
        with self.assertRaisesRegex(api.SmartAPIError, "disconnected"):
            self.client.quote(self.instrument)
        self.monotonic += 1
        self.http.side_effect = None
        old = quote_data()
        old["fetched"][0]["exchFeedTime"] = "01-Jan-2026 09:59:59"
        self.http.return_value = old
        with self.assertRaisesRegex(api.SmartAPIError, "Out-of-order"):
            self.client.quote(self.instrument)
        self.monotonic += 1
        conflict = quote_data()
        conflict["fetched"][0]["ltp"] = Decimal("100.10")
        self.http.return_value = conflict
        with self.assertRaisesRegex(api.SmartAPIError, "conflicting"):
            self.client.quote(self.instrument)
        self.monotonic += 1
        self.now += timedelta(seconds=31)
        self.http.return_value = quote_data()
        with self.assertRaisesRegex(api.SmartAPIError, "stale"):
            self.client.quote(self.instrument)

    def test_history_request_binding_and_client_endpoint_restrictions(self):
        self.login()
        self.http.return_value = [["2026-01-01T09:59:00+05:30", "100", "101", "99", "100.05", 123]]
        result = self.client.candles(self.instrument, "ONE_MINUTE", NOW - timedelta(minutes=1), NOW)
        endpoint, body, _ = self.http.call_args.args
        self.assertEqual(endpoint, api.HISTORY)
        self.assertEqual(body["symboltoken"], "123456")
        self.assertEqual(result["candles"][0]["close_paise"], 10005)
        self.assertFalse(result["one_minute_record_limit_reached"])
        count = self.http.call_count
        for endpoint in (api.MASTER, ("POST", api.QUOTE[1], "/rest/secure/angelbroking/order/v1/placeOrder")):
            with self.assertRaisesRegex(api.SmartAPIError, "Endpoint blocked"):
                self.client._call(endpoint, {})
        for name in ("placeOrder", "modifyOrder", "cancelOrder", "gttCreateRule", "convertPosition", "request", "logout"):
            self.assertFalse(hasattr(self.client, name))
        self.assertEqual(self.http.call_count, count)

    def test_cli_master_login_quote_flow_redacts_credentials_and_never_opens_journal(self):
        self.http.side_effect = [[MASTER_ROW], {"jwtToken": "secret.jwt.token"}, quote_data()]
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(api.Settings, "load", return_value=self.settings), patch.object(api.sys.stdin, "isatty", return_value=True), \
                patch.object(api, "SmartAPIData", return_value=self.client), \
                patch.object(api.getpass, "getpass", side_effect=["1234", "123456"]), \
                patch("paper_trader.PaperAccount") as paper, redirect_stdout(output), redirect_stderr(errors):
            result = api.main(["quote", "--symbol", "TEST-EQ", "--token", "123456"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["ask_inr"], "100.10")
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("secret", output.getvalue())
        self.assertEqual([call.args[0] for call in self.http.call_args_list], [api.MASTER, api.LOGIN, api.QUOTE])
        paper.assert_not_called()

    def test_cli_missing_config_noninteractive_and_prompt_failures_are_offline_or_stop_before_login(self):
        for load_error, interactive, prompt_error in [(api.SmartAPIError("Set ANGEL_API_KEY locally."), True, None),
                                                      (None, False, None), (None, True, api.getpass.GetPassWarning("secret"))]:
            self.http.reset_mock()
            self.http.return_value = [MASTER_ROW]
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(api.Settings, "load", side_effect=load_error, return_value=self.settings), \
                    patch.object(api.sys.stdin, "isatty", return_value=interactive), \
                    patch.object(api.getpass, "getpass", side_effect=prompt_error), redirect_stdout(output), redirect_stderr(errors):
                result = api.main(["quote", "--symbol", "TEST-EQ", "--token", "123456"])
            self.assertEqual(result, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn("secret", errors.getvalue())
            self.assertEqual(self.http.call_count, 1 if prompt_error else 0)

    def test_offline_demo_has_no_connection_or_environment_dependency(self):
        from paper_trader import demo
        self.assertEqual(demo()["account"]["realized_paise"], 7800)
        self.http.assert_not_called()

    def test_cli_argument_errors_do_not_echo_accidentally_supplied_secrets(self):
        for arguments in (["quote", "--symbol", "TEST-EQ", "--token", "123456", "--pin", "secret-pin"],
                          ["placeOrder", "secret-order"], ["quote", "--max-quote-age", "secret-age"]):
            errors = io.StringIO()
            with redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
                api.main(arguments)
            self.assertEqual(caught.exception.code, 2)
            self.assertNotIn("secret", errors.getvalue())
        self.http.assert_not_called()


class SettingsTests(unittest.TestCase):
    def test_local_literal_file_environment_precedence_and_safe_repr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# private fixture\nANGEL_API_KEY='secret#literal'\nANGEL_CLIENT_CODE=TESTCLIENT\n"
                            "ANGEL_LOCAL_IP=192.0.2.1\nANGEL_PUBLIC_IP=203.0.113.1\nANGEL_MAC_ADDRESS=00:11:22:33:44:55\n", encoding="utf-8")
            with patch.dict(api.os.environ, {}, clear=True):
                settings = api.Settings.load(path)
                self.assertEqual(settings.api_key, "secret#literal")
                self.assertNotIn("secret", repr(settings))
            with patch.dict(api.os.environ, {"ANGEL_API_KEY": "override"}, clear=True):
                self.assertEqual(api.Settings.load(path).api_key, "override")
            for contents in ("ANGEL_PIN=secret", "export ANGEL_API_KEY=secret", "ANGEL_API_KEY=a\nANGEL_API_KEY=b", "secret"):
                path.write_text(contents, encoding="utf-8")
                with patch.dict(api.os.environ, {}, clear=True), self.assertRaises(api.SmartAPIError) as caught:
                    api.Settings.load(path)
                self.assertNotIn("secret", str(caught.exception))

    def test_missing_placeholder_control_character_and_invalid_metadata_settings(self):
        valid = ["secret", "TESTCLIENT", "192.0.2.1", "203.0.113.1", "00:11:22:33:44:55"]
        for index, value in [(0, ""), (0, "YOUR_API_KEY"), (0, "secret\r\nInjected: header"),
                             (1, "invalid:client"), (2, "secret"), (3, "::1"), (4, "secret")]:
            values = valid.copy()
            values[index] = value
            with self.subTest(index=index), self.assertRaises(api.SmartAPIError) as caught:
                api.Settings(*values)
            self.assertNotIn("secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
