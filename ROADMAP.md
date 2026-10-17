# Build roadmap

Each phase should leave a runnable, checked result. Complete and review one phase before expanding the scope.

## Phase 1 — offline accounting and execution

Implemented:

- Virtual cash, long positions, buys/sells and persistent SQLite fills.
- Exact money arithmetic, weighted cost allocation, adverse slippage and configurable flat fees.
- Quote freshness, cash/holding checks and idempotent requests across restarts/concurrent writers.
- An isolated fictional demo, CLI instructions and accounting/error-path tests.

Acceptance: `python -m unittest -v` passes; `python paper_trader.py demo` finishes with ₹78 realised profit; a rejected trade cannot change the ledger. No broker SDK or network path is present.

## Phase 2 — authorised Angel One market data

- Re-check current official SmartAPI access conditions, including registered static egress and daily authentication; verify them against the actual account without putting secrets in chat or Git.
- Add a narrowly scoped authentication/data adapter and a placeholder-only `.env.example` when used.
- Verify instrument identity, price scaling, timestamps, REST quotes and WebSocket access separately.
- Validate SDK depth labels against the current protocol and REST FULL quotes; retain TLS certificate verification and redact credentials in errors.
- Test disconnection, expiry, rate responses and reconnect behaviour. Reject stale prices throughout.

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
