# Angel Paper Trader

An educational paper-trading simulator for Indian markets, being built in phases toward an Angel One market-data connection.

**Current phase: offline accounting plus read-only SmartAPI REST and WebSocket adapters.** All balances and fills are virtual. The bundled `DEMO-EQ` instrument and its prices are fictional. `paper_trader.py` stays offline; the data CLIs separately inspect authorised market data. They cannot send broker orders. Angel One does not sponsor or endorse this project. Account-dependent live access has **not** been verified.

## Run it

Use Python 3.11 or newer. REST, accounting and the core offline tests use the standard library. WebSocket streaming has one optional transport dependency, installed below. The offline simulator and tests require no API key, static IP or paid service.

```bash
git clone https://github.com/AaryaMody1301/angel-paper-trader.git
cd angel-paper-trader
python paper_trader.py demo
python -m unittest -v
```

On Windows, `py` can be used instead of `python`.

The demo starts with **₹100,000** in memory, buys 10 fictional units at a ₹101 ask, then sells them at a ₹109 bid. It deducts an illustrative ₹1 charge per fill, producing **₹78 net realised P&L** and **₹100,078 cash**. Those charges are an example, not Angel One's tariff. The demo leaves your saved journal untouched.

## Use your own virtual account

```bash
python paper_trader.py init --cash 100000 --fee 1 --slippage-bps 10
python paper_trader.py quote DEMO-EQ --bid 99 --ask 101
python paper_trader.py buy DEMO-EQ 10 --id entry-001
python paper_trader.py status

# Enter a new fictional quote before exiting.
python paper_trader.py quote DEMO-EQ --bid 109 --ask 111
python paper_trader.py sell DEMO-EQ 10 --id exit-001
python paper_trader.py trades
```

Execute each trade within 30 seconds of its quote. Refresh the quote if it becomes stale. With 10 basis points of adverse slippage, your manual-session results differ from the zero-slippage demo.

The journal persists at `data/paper.sqlite3`, which Git ignores. To create another account, select another database; re-running `init` never resets an existing balance:

```bash
python paper_trader.py --db data/another-account.sqlite3 init --cash 50000
```

`--db` goes before the command. Configuration is fixed when an account is initialised: flat fee per fill, slippage in basis points and maximum quote age. `--max-quote-age 60` changes the age threshold for a new account. Defaults are zero fees, zero slippage and a 30-second quote age; choose explicit costs when evaluating outcomes.

## Accounting and execution rules

- Buys use the ask plus adverse slippage; sells use the bid minus adverse slippage. Buy prices round up to the next paise and sell prices round down.
- Money is stored as integer paise and displayed as exact INR strings. Inputs with sub-paise precision are rejected.
- Long positions only: buys need sufficient virtual cash including fees; sells cannot exceed the held quantity. No margin or short selling.
- Entry fees become part of the position cost. Partial exits allocate weighted position cost proportionally, with the remaining paise retained until later exits. Realised P&L includes entry and exit fees.
- Open positions use a fresh bid mark. Marked equity/unrealised P&L exclude hypothetical exit fees and exit slippage. Stale or missing marks make total equity unavailable, rather than reusing an old value.
- Every fill needs a unique request ID. Repeating the **same** ID, symbol, side and quantity returns the original receipt, including after restarting; it does not create a second fill. A changed request with a reused ID is rejected.
- SQLite transactions keep cash, positions and fills together and serialise concurrent writers. Rejected orders leave them unchanged.
- Crossed, zero, future-dated, stale and out-of-order quotes are rejected. Identical quote updates are harmless.

## Scope and limitations

The offline simulator fills the full requested quantity against a supplied quote. It does **not** model displayed liquidity, queue position, partial fills, limit/stop orders, exchange tick sizes, contract lots, margin, corporate actions or expiry/settlement. The fixed fee is not a complete Indian brokerage/tax model. There is no strategy or historical-data backtester. The read-only stream does not feed the paper execution engine. Synthetic results are not evidence of trading profitability.

Although offline symbols can be named freely, the data adapter supports only NSE cash instruments in the `EQ` series with lot size 1. Indices, other equity series, derivatives, currencies and other exchanges are rejected. Live F&O work requires authoritative contract metadata and actual contract premiums; an index price is not an option premium.

## Read-only SmartAPI setup

The adapter offers a single-instrument REST FULL snapshot and historical OHLCV, using only Python's standard library. The optional `smartapi_stream.py` CLI adds single-instrument WebSocket 2.0 SnapQuote streaming. Neither writes to the paper journal or submits virtual fills.

1. Enable SmartAPI and TOTP on your own Angel One account. Create/verify your app in the [official SmartAPI portal](https://smartapi.angelone.in/). Confirm the app has market-data and historical-data permissions and that it accepts PIN/TOTP login. OAuth was announced in 2025; this adapter implements only the PIN/TOTP endpoint still listed in the current docs and official Python client. An OAuth-only account needs a separately reviewed adapter change.
2. Arrange actual static **IPv4** egress and register it with the app. Treat this as required for data access under the newer broker clarification below. Use the same route when running the commands. A value in an HTTP header does not establish or register your source IP; the adapter does not alter network routing, buy infrastructure or register anything on your account.
3. Copy the placeholder-only `.env.example` to `.env` **if you do not already have a local configuration**, then edit it locally. Enter the API key, client code, actual local/public IPv4 addresses and colon-separated MAC address. Keep file permissions restricted to your user. Never paste these values, PINs, TOTP seeds or session tokens into chat, issues or PRs.
4. Read the current [broker instrument master](https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json) and select an exact exchange/symbol/token combination. Each command downloads the master again and rejects missing, ambiguous or mismatched identities and unsupported metadata.

The `.env` format is literal `KEY=VALUE`, optionally enclosed in matching quotes, with full-line `#` comments. There is no shell execution, `export`, interpolation or inline-comment syntax. Only the five keys in `.env.example` are accepted; environment variables override the corresponding file values. PIN and the current six-digit TOTP are requested through **hidden prompts in your local interactive terminal**, never through command-line flags. The adapter stores no session files; JWTs live in memory, refresh tokens are discarded, and feed tokens are retained only when streaming is requested. Local disposal clears both tokens and sends no broker logout that could affect another application's session.

```bash
# Run locally during an active market session with a fresh order book.
# SBIN-EQ / 3045 is a public identifier example; verify it in today's master.
python smartapi_data.py quote --exchange NSE --symbol SBIN-EQ --token 3045

# Historical example: replace dates with the past period you need.
python smartapi_data.py candles --symbol SBIN-EQ --token 3045 --interval FIVE_MINUTE --from-time "2026-10-16T09:15:00+05:30" --to-time "2026-10-16T09:30:00+05:30"

python smartapi_data.py --help
python smartapi_data.py quote --help
python smartapi_data.py candles --help
```

`--env-file path/to/private.env` goes before the command. Quote age defaults to 30 seconds; `quote --max-quote-age 60 ...` explicitly changes that threshold. A closed market or absent/zero depth can legitimately produce an error instead of a quote. Last-trade time may be older than a fresh order book, so freshness uses **exchange feed time**, converted from IST to UTC, rather than receipt time or last-trade time. Output includes both feed and receipt timestamps. Recheck age when consuming a snapshot later; it is not a continuing feed.

REST prices arrive in INR and are parsed with `Decimal` into exact integer paise; CLI output uses exact INR strings. Master `tick_size` is already in paise. Active depth prices must match that tick; historical/last-trade prices retain exact paise checks without assuming today's tick applied to an older trade. Highest `depth.buy` is the bid and lowest `depth.sell` is the ask. Missing, partial, crossed, stale, future, mismatched and out-of-order/conflicting snapshots fail. LTP never replaces missing bid/ask data.

History requests require timezone-aware, increasing, past timestamps at whole minutes. Returned candles must be ordered, unique, inside the requested range, and have valid OHLC/volume. Candles are not executable live quotes. Results do not guarantee gap-free coverage; the last candle can be incomplete. An 8000-row one-minute response sets `one_minute_record_limit_reached`; narrow/split the request and investigate completeness before using it. There is no pagination or backtester.

On failure the REST CLI exits nonzero with a controlled error and no price output. It recognises invalid/expired sessions, access denial, JSON/API errors, HTTP 429, plain-text rate-limit 403s, `AB1021`, network timeout/disconnection, and stale data. Each instance permits at most one attempt per second per endpoint and respects `Retry-After` seconds/HTTP dates (60-second fallback). REST has no automatic retries or cached-price fallbacks. Wait for the reported cooldown before manually retrying; coordinate all other processes/apps sharing the client code. Re-login with fresh local TOTP after expiry, including the midnight IST cutoff. A successful login alone does not establish data permissions or static-IP acceptance.

TLS hostname/certificate verification is always enabled, with TLS 1.2 or newer. Redirects are blocked. Raw broker bodies, request headers, PINs and tokens are never logged or included in error messages. The only outbound targets are:

| Method | Host | Exact path |
| --- | --- | --- |
| POST | `apiconnect.angelone.in` | `/rest/auth/angelbroking/user/v1/loginByPassword` |
| POST | `apiconnect.angelone.in` | `/rest/secure/angelbroking/market/v1/quote` |
| POST | `apiconnect.angelone.in` | `/rest/secure/angelbroking/historical/v1/getCandleData` |
| GET | `margincalculator.angelone.in` | `/OpenAPI_File/files/OpenAPIScripMaster.json` |
| GET (WebSocket upgrade) | `smartapisocket.angelone.in` | `/smart-stream` |

The HTTP boundary checks the exact method/host/path against an immutable allowlist before constructing a connection. The separate WebSocket boundary connects only to the fixed `wss://smartapisocket.angelone.in/smart-stream` URI and sends only the selected SnapQuote subscription and text heartbeats. No generic broker URL, execution SDK, order placement/modification/cancellation, GTT, position conversion, EDIS or other account-changing route is exposed. Authentication is the only session-creating operation. The public master receives no account headers. This is an application boundary, not a sandbox for arbitrary Python code a user might add themselves.

## Optional WebSocket streaming

Create a virtual environment if needed and install the pinned optional transport. On Windows, use its Python directly so activation-policy changes are unnecessary:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-stream.txt
.\.venv\Scripts\python.exe -m unittest -v
.\.venv\Scripts\python.exe smartapi_stream.py --help

# Complete the local .env/app/static-IP setup above first.
# Check today's master and run during an active market session.
.\.venv\Scripts\python.exe smartapi_stream.py --symbol SBIN-EQ --token 3045 --count 10 --seconds 60 --max-reconnects 2
```

On macOS/Linux the equivalent executable is `.venv/bin/python`. The dependency is `websockets==17.2`, released 3 October 2026 on [PyPI](https://pypi.org/project/websockets/17.2/). It supplies the WebSocket transport; authentication and quote decoding stay in this project. Missing or different transport versions fail before login/network access. `--help` and the binary/lifecycle fixture tests work without it; two installed-transport checks are skipped when it is absent. Both test runs were checked on 18 October 2026.

The CLI prints one JSON object per line. `CONNECTING` and `SUBSCRIBED` health events contain no prices. A verified quote event has `event: "quote"`, `status: "LIVE"`, exact INR bid/ask/LTP strings, sequence, exchange timestamp and receipt timestamp. `UNAVAILABLE`, `RECONNECTING` and `STOPPED` invalidate any prior live status; consumers must stop using previous quotes and still enforce their age. The command stops at `--count` fresh quotes or `--seconds` elapsed, and Ctrl+C closes the connection and clears the local session. Failure exits nonzero. It never prints a replacement price when data fails.

Only NSE EQ / exchange type 1 / mode 3 is supported. The parser checks the exact 379-byte packet, NUL-padded instrument token, signed 64-bit sequence/price/quantity fields, millisecond exchange feed time, five records per depth side, quantities, ticks and non-crossed prices. Wire **flag 1 means bid, flag 0 means ask**. WebSocket prices are already paise, so no extra INR-to-paise multiplication is applied. Last-trade and dummy/OI-change fields do not establish quote freshness. Repeated identical sequences are ignored; sequence regressions, same-sequence price conflicts and backwards feed timestamps stop the stream. Sequence gaps are accepted as complete snapshots without inventing the missing ticks; an advancing sequence may update prices within the same millisecond.

Application text `ping` is sent immediately and then every 20 seconds, within the documented 30-second heartbeat interval. A missing text `pong` for 10 seconds, absent fresh data or an aged quote marks the stream unavailable. Pongs cannot refresh a quote timestamp. Recoverable transport/heartbeat/no-data interruptions close the previous socket and use a lifetime reconnect budget: default two reconnects with 1- then 2-second backoff (configurable 0–5, delay capped at 8 seconds). Successful ticks do not reset that budget. Reconnection repeats the same subscription, permits a new connection's sequence to restart, and emits no quote until the feed timestamp is newer than the last accepted quote. REST is never silently substituted for the stream.

Authentication, access, subscription, protocol and TLS errors stop without retries. HTTP 429 or a transient service error with `Retry-After` stops with a cooldown, retained by the client instance; manually retry after it expires. Raw broker close reasons/headers/bodies are suppressed. Sockets use verified TLS 1.2+, disabled transport credential logging, no automatic redirects or environment proxies, no compression, a 4096-byte message limit and a four-frame queue. Static egress must therefore come from the actual local network route. Programmatic callers should use `login(..., streaming=True)` and close `client.stream(...)` with `contextlib.closing` when stopping iteration early.

## Official SmartAPI review — 17 October 2026

Primary sources conflict in places. Publication dates below refer to the original announcement, not the most recent reply in its thread. Some forum threads now display a deletion notice while still exposing indexed posts; direct docs access also intermittently returns an authorisation page. The review used the retrievable official docs/indexed text and official SDK source; it did not validate an account or treat a user reply as broker policy.

- **Static IP:** the [26 September 2025 admin rollout notice](https://smartapi.angelone.in/smartapi/forum/topic/5352/static-ip-based-api-keys-now-live-old-flow-still-supported-temporarily) temporarily exempted APIs other than Orders/GTT. The [broker notice published 25 February, updated 13 March 2026](https://www.angelone.in/news/market-updates/what-s-changing-in-angel-one-s-smartapi-access-from-april-1-2026) makes order execution subject to registered static IP from 1 April 2026. A newer [Moderator_3 clarification in the September 2026 data-only thread](https://smartapi.angelone.in/smartapi/forum/topic/5671/asking-for-static-ip-for-getting-api-for-historical-data/3?lang=en-GB), also on the [official moderator profile](https://smartapi.angelone.in/smartapi/forum/user/moderator_3/posts), says the broker applies static IP to **all** app types/access. The newer clarification determines our data-access setup. It distinguishes broker conditions from the exchange's order-execution requirement. Its timestamp is displayed relatively, so no exact publication day is asserted here; retrieved 17 October 2026. Do not rely on the older temporary exemption or try to spoof egress headers.
- **Authentication/sessions:** [current User documentation](https://smartapi.angelone.in/docs/User) lists client code, PIN and TOTP, returning JWT/refresh/feed tokens. The [26 September 2025 admin notice](https://smartapi.angelone.in/smartapi/forum/topic/5352/static-ip-based-api-keys-now-live-old-flow-still-supported-temporarily) confirms daily midnight logout. [Earlier OAuth announcements and the 18 September 2025 admin reply](https://smartapi.angelone.in/smartapi/forum/topic/5254/important-updates-to-smartapi-in-compliance-with-sebi-guidelines/37) do not establish removal of PIN/TOTP: the [official Python client](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartConnect.py) still uses `loginByPassword`. Account acceptance remains unverified; the adapter requests reauthentication rather than assuming refresh extends a daily session.
- **REST/rates:** [current market-data/history/rate documentation](https://smartapi.angelone.in/docs) describes FULL depth and ISO OHLCV candles. The quote section says **1 request/second, up to 50 tokens**, while the rate table says **10/second, 500/minute, 5000/hour**. Use the stricter 1/second (this adapter requests one token). History is **3/second, 150/minute, 5000/hour** in the current table; the [28 February 2024 moderator notice and 18 March correction](https://smartapi.angelone.in/smartapi/forum/topic/4387/changes-in-api-rate-limit/1) used 180/minute. Use 150. Limits apply per client code, not per instance. The [historical expansion release](https://smartapi.angelone.in/smartapi/forum/topic/4012/release-note-free-historical-data-access-for-indices-nse-nfo-bse-bfo-mcx-and-cds-with-smartapi/1) notes the 8000-record one-minute ceiling. The adapter enforces the documented day ranges: 30/60/100/100/200/200/400/2000 for 1m/3m/5m/10m/15m/30m/1h/1d respectively.
- **Units/depth:** the [official master-field clarification](https://smartapi.angelone.in/smartapi/forum/topic/1300/instrument-master-fields-interpretation/1) identifies master values as paise for the supported equity case; currency conventions differ and are unsupported. REST FULL's named buy/sell arrays determine this adapter's sides. The [WebSocket 2.0 protocol](https://smartapi.angelone.in/docs/WebSocket2) specifies little-endian SnapQuote depth with flag **1 = buy/bid, 0 = sell/ask**, equity prices in paise, and exchange feed timestamps in epoch milliseconds. The [current official SDK implementation](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartWebSocketV2.py) reverses labels inside its depth helper and swaps them back in its outer parser. Copying helper labels alone gives the wrong sides. It also uses `CERT_NONE` in its connection code; this project never imports or adopts that client.
- **WebSockets (implementation review extended 18 October 2026):** [the protocol](https://smartapi.angelone.in/docs/WebSocket2) uses the stream URL above, JWT/API-key/client-code/feed-token headers, text ping/pong within 30 seconds, three concurrent connections per client and 1000 token/mode subscriptions per connection. The new adapter uses one connection/subscription at a time. The table's LTP `int32` label conflicts with its eight-byte width; the layout and [official SDK's signed 64-bit decoder](https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartWebSocketV2.py) determine decoding. Offline fixtures check both protocol sides against REST FULL. The [pinned transport source](https://github.com/python-websockets/websockets/blob/17.2/src/websockets/sync/client.py) follows redirects by default, so this adapter rejects them through its redirect hook; an installed-transport test verifies no second handshake. These checks establish implementation behaviour, **not live access or broker quotas**.

## Verification and remaining account checks

```bash
python -m unittest -v
python paper_trader.py demo
python smartapi_data.py --help
python smartapi_stream.py --help
git diff --check
git check-ignore .env .env.local data/paper.sqlite3 logs/session.log
```

The offline suite mocks every SmartAPI HTTP/WebSocket connection and uses fictional fixtures. It covers accounting, binary/REST parsing and cross-checks, instrument identity/units/timestamps, restricted endpoints before I/O, verified TLS configuration, redirects, credential-safe failures, session expiry, local/server rate limits, heartbeat loss, bounded reconnects/resubscription, replay rejection after reconnect and both CLI flows. With `requirements-stream.txt` installed, all 69 tests passed on 18 October 2026. The dependency-free run passed with two installed-transport tests skipped. The demo remains isolated and returns ₹78 realised profit. Passing these checks is **not live broker verification**. Separately, a public-master HTTPS download and resolution of `NSE / SBIN-EQ / 3045` to a 5-paise tick succeeded on 17 October 2026; this used no credentials or authenticated endpoint.

Remaining local checks require your app/credentials, registered static egress and data permissions: log in locally; compare a fresh bid/ask, token, feed time and price scale against Angel One's terminal; request a small known historical range and independently compare OHLCV; run the WebSocket CLI and compare contemporaneous SnapQuote and REST FULL depth. Check actual expiry/re-login, heartbeat responses, rate/access failures and reconnect/resubscription recovery using authorised, low-volume requests. Never test the read-only restriction by submitting a real order. Record only a non-sensitive outcome/date in project notes; keep broker responses, app/account screenshots and tokens private. WebSocket implementation is complete for the stated scope, while authenticated REST/WebSocket verification and Phase 2 live acceptance remain open.

## Public repository hygiene

Commit code, documentation and fictional examples. Keep credentials, TOTP seeds/QR codes, PINs, session tokens, account details and personal journals out of Git. `.gitignore` excludes `.env`/`.env.*` except the placeholder-only `.env.example`, private journals, exports and logs; it does not protect a file already tracked. Never force-add local configuration or account data.

## Next phases

See [ROADMAP.md](ROADMAP.md) for implementation status and acceptance criteria. Phase 1 remains implemented and checked. Phase 2's REST and single-instrument WebSocket adapters are implemented and checked offline; authorised live validation remains open. Phase 3 forward execution has not started. No dashboard, strategy or real-money execution is included.

## License

MIT. See [LICENSE](LICENSE).
