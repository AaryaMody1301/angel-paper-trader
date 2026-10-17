# Build roadmap

Each phase should leave a runnable, checked result. Complete and review one phase before expanding the scope.

## Phase 1 — offline accounting and execution

Implemented:

- Virtual cash, long positions, buys/sells and persistent SQLite fills.
- Exact money arithmetic, weighted cost allocation, adverse slippage and configurable flat fees.
- Quote freshness, cash/holding checks and idempotent requests across restarts/concurrent writers.
- An isolated fictional demo, CLI instructions and accounting/error-path tests.

Acceptance verified again on 17 October 2026: the original 12 accounting tests pass; `python paper_trader.py demo` finishes with ₹78 realised profit; rejected trades cannot change the ledger. `paper_trader.py` and its demo remain offline, with no broker SDK or network path.

## Phase 2 — authorised Angel One market data (REST implemented; live acceptance open)

Implemented and checked on 17 October 2026 (offline unless stated):

- Standard-library `smartapi_data.py`: local PIN/TOTP authentication, memory-only JWT, conservative midnight IST expiry, REST FULL bid/ask snapshots and historical OHLCV. No automatic refresh, retries or invented/cached-price fallback.
- Exact outbound allowlist for login, quote, candles and public instrument master only; verified TLS, blocked redirects, safe errors and no execution SDK. All order/GTT/account-mutation endpoints are inaccessible through the adapter.
- Placeholder-only `.env.example`, a literal local loader, hidden PIN/TOTP prompts, no session/account files and existing `.gitignore` protections preserved.
- NSE EQ cash scope: fresh master lookup binds exchange/symbol/token and supported lot/tick metadata; exact INR-to-paise conversion, exchange timestamps, quote age/depth and historical candle checks. Historical prices do not assume today's tick applied to older trades.
- Public metadata check: an actual verified-TLS master download resolved `NSE / SBIN-EQ / 3045` with a 5-paise tick. This exercised no login or authenticated endpoint and did not save the master or any account data.
- Offline tests cover parsing, identity/scaling, credentials/TLS, endpoint denial before I/O, midnight/broker expiry, rate cooldown, disconnect/failure without a stale-price fallback, request recovery and CLI isolation from accounting.
- Dated official-source review and local commands in [README.md](README.md). Newer broker moderator guidance requires static IP for all app types, superseding the temporary 2025 data exemption. Conservative pacing uses the stricter conflicting documentation. REST buy/sell mapping was checked against the WebSocket flag contract and the official SDK's internal label swaps.

Not implemented in this initial REST increment:

- WebSocket streaming, binary decoding and heartbeat/reconnect/resubscription. Requirements have been reviewed; add streaming as a separate Phase 2 increment with verified TLS and offline binary/disconnect tests.
- Automatic delivery of broker data into paper fills. The REST inspection CLI never opens the paper journal; forward-execution modelling belongs to Phase 3.

Account-dependent checks **not performed / not verified**:

- App creation/access, accepted authentication flow, actual registered static IPv4 route, account permission for REST quotes and history, and daily session behaviour against the broker.
- Live instrument/price/feed-time and bid/ask comparison with Angel One's terminal, plus small-range historical OHLCV reconciliation. The offline fixture tests are not evidence of live access or authoritative live-price accuracy.
- Actual broker denial/rate-limit/disconnect and recovery behaviour under authorised low-volume use. WebSocket access and independent REST-versus-WebSocket depth comparison remain unverified as well as unimplemented.

Remaining setup: create/verify your app and TOTP locally, obtain/register static IPv4 egress, confirm data permissions, populate ignored `.env`, and run the documented quote/history commands from that route. Keep all credentials, raw responses and account data local. This increment did not attempt authenticated broker requests.

Acceptance: authorised live quotes are independently checked; failures have clear status; an outbound-endpoint audit shows that the paper application cannot submit or change real orders. Broker access must be demonstrated separately from offline tests.

## Phase 3 — realistic forward paper trading

- Add authoritative contract lots/ticks, supported expiry policy, explicit quantity/liquidity assumptions and partial-fill handling.
- Replace the illustrative flat charge with a dated, configurable cost model for supported segments.
- Add exposure/position limits, daily entry stops and a feed-disconnect policy.
- Add recorded-data replay without future-data leakage and exportable, private session results.

Acceptance: manual calculations reconcile with fills and reports; replay is reproducible; unavailable liquidity/data never produces an invented fill. Supported and unsupported order/contract types are documented.

## Phase 4 — dashboard and strategy evaluation

- Add a local dashboard for quote age, connection health, selected instrument, virtual orders, holdings and net P&L.
- Add one explicitly specified strategy at a time and compare with a simple baseline.
- Report drawdown, turnover, fees/slippage and rejected signals alongside returns.

Acceptance: UI actions reconcile with the stored journal; restart recovery works; performance is labelled with its data and modelling assumptions. Strategy results do not become claims of guaranteed profitability.

## Real-money execution

Outside the current project scope. It requires a separate decision and review of the then-current broker/exchange rules, execution controls and operational readiness. A paper account is never converted by adding a live-mode toggle.
