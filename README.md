# Angel Paper Trader

An educational paper-trading simulator for Indian markets, being built in phases toward an Angel One market-data connection.

**Current phase: an offline simulator.** All balances and fills are virtual. The bundled `DEMO-EQ` instrument and its prices are fictional. This version uses no broker SDK, credentials or network connection and cannot send broker orders. Angel One does not sponsor or endorse this project.

## Run it

Use Python 3.11 or newer. No third-party packages, API key, static IP or paid service are needed for this phase.

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

This first phase fills the full requested quantity against a supplied quote. It does **not** model displayed liquidity, queue position, partial fills, limit/stop orders, exchange tick sizes, contract lots, margin, corporate actions or expiry/settlement. The fixed fee is not a complete Indian brokerage/tax model. There is no strategy, historical-data backtester or live market feed yet. Synthetic results are not evidence of trading profitability.

Although symbols can be named freely, this phase does not validate options/futures contracts. Live F&O work requires authoritative contract metadata and actual contract premiums; an index price is not an option premium.

## Public repository hygiene

Commit code, documentation and fictional examples. Keep credentials, TOTP seeds/QR codes, PINs, session tokens, account details and personal journals out of Git. `.gitignore` helps prevent accidental additions but does not protect a file already tracked. No credentials are needed for this version; an environment template will be added when configuration is actually used.

## Next phases

See [ROADMAP.md](ROADMAP.md) for the implementation sequence and acceptance criteria. The next phase is a **read-only Angel One data adapter**, after verifying current account access, authentication and network requirements. Adding that adapter must not introduce real-order submission into the paper engine.

## License

MIT. See [LICENSE](LICENSE).
