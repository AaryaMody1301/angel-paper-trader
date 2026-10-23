# Build roadmap

Each phase should leave a runnable, checked result. Complete and review one phase before expanding the scope.

## Phase 1 — offline accounting and execution

Implemented:

- Virtual cash, long positions, buys/sells and persistent SQLite fills.
- Exact money arithmetic, weighted cost allocation, adverse slippage and configurable flat fees.
- Quote freshness, cash/holding checks and idempotent requests across restarts/concurrent writers.
- An isolated fictional demo, CLI instructions and accounting/error-path tests.

Acceptance verified again on 17 October 2026: the original 12 accounting tests pass; `python paper_trader.py demo` finishes with ₹78 realised profit; rejected trades cannot change the ledger. `paper_trader.py` and its demo remain offline, with no broker SDK or network path.

## Phase 2 — authorised Angel One market data (REST/WebSocket implemented; live acceptance open)

Implemented and checked on 17 October 2026 (offline unless stated):

- Standard-library `smartapi_data.py`: local PIN/TOTP authentication, memory-only JWT, conservative midnight IST expiry, REST FULL bid/ask snapshots and historical OHLCV. No automatic refresh, retries or invented/cached-price fallback.
- Exact outbound allowlist for login, quote, candles and public instrument master only; verified TLS, blocked redirects, safe errors and no execution SDK. All order/GTT/account-mutation endpoints are inaccessible through the adapter.
- Placeholder-only `.env.example`, a literal local loader, hidden PIN/TOTP prompts, no session/account files and existing `.gitignore` protections preserved.
- NSE EQ cash scope: fresh master lookup binds exchange/symbol/token and supported lot/tick metadata; exact INR-to-paise conversion, exchange timestamps, quote age/depth and historical candle checks. Historical prices do not assume today's tick applied to older trades.
- Public metadata check: an actual verified-TLS master download resolved `NSE / SBIN-EQ / 3045` with a 5-paise tick. This exercised no login or authenticated endpoint and did not save the master or any account data.
- Offline tests cover parsing, identity/scaling, credentials/TLS, endpoint denial before I/O, midnight/broker expiry, rate cooldown, disconnect/failure without a stale-price fallback, request recovery and CLI isolation from accounting.
- Dated official-source review and local commands in [README.md](README.md). Newer broker moderator guidance requires static IP for all app types, superseding the temporary 2025 data exemption. Conservative pacing uses the stricter conflicting documentation. REST buy/sell mapping was checked against the WebSocket flag contract and the official SDK's internal label swaps.

WebSocket increment implemented and checked offline on 18 October 2026:

- `smartapi_stream.py` and `SmartAPIData.stream()`: single-instrument NSE EQ SnapQuote subscription, binary identity/depth/sequence validation and exact paise/millisecond decoding. Fictional protocol fixtures independently match the REST FULL sides and scaling.
- Opt-in memory-only feed token, shared local hidden login prompts and daily session expiry checked throughout streaming, including after a blocking receive. Closing/cancelling disposes sockets and tokens without broker logout.
- Text heartbeat and data-age watchdogs, explicit health events, bounded lifetime reconnect/backoff and resubscription. No cached/fictional prices or silent REST fallback. After reconnect, old/equal feed timestamps are withheld until a newer quote arrives.
- Fixed market-data WSS target, verified TLS, rejected redirects/proxies, bounded frames/messages, suppressed dependency credential logging and no generic send/URL API. Existing HTTP execution restrictions remain unchanged.
- One optional pinned transport (`requirements-stream.txt`); REST/accounting/core fixtures still run without it. All 69 tests pass with it installed; the dependency-free run passes with two installed-transport checks skipped. The isolated demo still returns ₹78 profit.

Not included in Phase 2:

- Automatic delivery of broker data into paper fills. The REST inspection CLI never opens the paper journal; forward-execution modelling belongs to Phase 3.
- Multiple instruments/modes, derivatives or other exchanges. The stream keeps the same supported NSE EQ identity scope as REST.

Account-dependent checks **not performed / not verified**:

- App creation/access, accepted authentication flow, actual registered static IPv4 route, account permission for REST quotes and history, and daily session behaviour against the broker.
- Live instrument/price/feed-time and bid/ask comparison with Angel One's terminal, plus small-range historical OHLCV reconciliation. The offline fixture tests are not evidence of live access or authoritative live-price accuracy.
- Actual broker denial/rate-limit/disconnect and recovery behaviour under authorised low-volume use. WebSocket authentication/access, heartbeat behaviour and independent live REST-versus-WebSocket depth comparison remain unverified; the implementation and offline recovery checks are complete for the stated scope.

Remaining setup: create/verify your app and TOTP locally, obtain/register static IPv4 egress, confirm data/feed permissions, populate ignored `.env`, install the optional stream dependency and run the documented quote/history/stream commands from that route. Keep all credentials, raw responses and account data local. Neither implementation increment attempted authenticated broker requests.

Acceptance: authorised live quotes are independently checked; failures have clear status; an outbound-endpoint audit shows that the paper application cannot submit or change real orders. Broker access must be demonstrated separately from offline tests.

## Phase 3 — realistic forward paper trading

Implementation added on 23 October 2026: `ai_trader.py` provides collection, immutable datasets, CPU/CUDA LoRA training, local Ollama evaluation, recorded-book replay, gated shadow/paper sessions and private reports. The separate AI journal preserves original offline journals. Native-unit contract prices, lots/multipliers, full-notional futures collateral, bounded displayed-liquidity fills, costs, exposure/daily limits and recovery controls are implemented. See [AI_WORKFLOW.md](AI_WORKFLOW.md).

All SmartAPI segments have capability/readiness records; activation requires verified metadata/account access, sufficient historical and recorded data, passing holdout results and two complete subsequent shadow sessions. No real market dataset, trained trading model, authenticated segment acceptance or profitable strategy has been established. Partial fills remain outside this increment; orders require the whole requested quantity in displayed top-of-book depth.

Remaining acceptance:

- Verify dated contract units, calendars, costs and authenticated feeds locally; reconcile fills and reports with independent calculations.
- Collect at least 60 training, 20 validation and 20 untouched test sessions per enabled segment, including recorded executable depth for return evaluation.
- Pass the locked holdout and recorded-book performance gates, then complete two subsequent shadow sessions without control failures.
- Activate segments progressively: cash equities, financial derivatives, commodities and currencies. Unsupported or insufficiently evidenced contracts stay blocked.

Replay must remain reproducible; unavailable liquidity/data must never produce an invented fill. Partial fills and overnight settlement remain unsupported.

## Phase 4 — dashboard and strategy evaluation

The local fine-tuned predictor/evaluation pipeline is implemented; market performance acceptance remains open. Dashboard work is deferred until the terminal flow is accepted.

- Add a local dashboard for quote age, connection health, selected instrument, virtual orders, holdings and net P&L.
- Add one explicitly specified strategy at a time and compare with a simple baseline.
- Report drawdown, turnover, fees/slippage and rejected signals alongside returns.

Acceptance: UI actions reconcile with the stored journal; restart recovery works; performance is labelled with its data and modelling assumptions. Strategy results do not become claims of guaranteed profitability.

## Real-money execution

Outside the current project scope. It requires a separate decision and review of the then-current broker/exchange rules, execution controls and operational readiness. A paper account is never converted by adding a live-mode toggle.
