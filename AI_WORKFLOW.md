# Local LLM research and paper trading

`ai_trader.py` collects private market data, prepares chronological datasets, fine-tunes Qwen3-0.6B with LoRA, evaluates local Ollama predictions against baselines and recorded order books, and runs gated virtual sessions. No trained market model or account verification is bundled. Every segment starts blocked until its evidence is available.

The original `paper_trader.py` commands and `data/paper.sqlite3` remain independent. The AI journal defaults to `data/ai.sqlite3`. Opening an offline journal through the AI CLI is rejected without changing it. No broker execution endpoint or LLM trading tool is exposed.

## Code map

| Module | Responsibility |
| --- | --- |
| `ai_trader.py` | CLI, session orchestration and background inference while feed recording continues. |
| `smartapi_data.py`, `smartapi_stream.py` | Read-only broker transport, authentication, protocol and health checks. |
| `ai_paper/market.py` | Verified identities and units, private recordings, completed bars and frozen shortlists. |
| `ai_paper/learning.py` | Causal datasets, local LoRA training and strict local direction inference. |
| `ai_paper/evaluation.py` | Recorded-book replay, locked holdout evidence and per-segment promotion gates. |
| `ai_paper/execution.py`, `paper_trader.settle_fill` | Shared exact accounting, sizing, virtual fills, risk limits and reports. |
| `tests/` | Offline accounting, protocol, execution, causality, recovery and optional real CPU/CUDA training checks. |

Ponytail full is the standing coding rule in `AGENTS.md`. Existing helpers own shared behavior: sizing and fills use one slippage calculation, replay uses the same execution engine, and both journals use the same settlement arithmetic.

## Environment

Use Python 3.13 for the pinned training stack. Python 3.11+ supports the dependency-free core. The commands below use Windows PowerShell; on Linux, create the environment with `python3.13 -m venv .venv` and use `.venv/bin/python`.

```powershell
py -V:3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-stream.txt
.\.venv\Scripts\python.exe -m unittest -v
.\.venv\Scripts\python.exe paper_trader.py demo
.\.venv\Scripts\python.exe ai_trader.py --help
```

Optional CPU training dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-training.txt --extra-index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m unittest -v tests.test_ai_training
```

For NVIDIA training, install the matching CUDA PyTorch build using the [official selector](https://pytorch.org/get-started/locally/), then install the remaining pinned requirements. `--device auto` chooses available CUDA or CPU; `cpu` forces CPU; `cuda` fails if unavailable. Intel integrated graphics are not CUDA. Offline tests train a tiny randomly initialised Qwen3 with real LoRA/checkpoint operations; this does not establish full-model speed or forecasting skill.

## Private configuration

Complete the README's SmartAPI setup: app permissions, actual registered static IPv4 egress, hidden local PIN/TOTP entry, and independent REST/history/WebSocket comparisons. Login alone does not establish data access. Create configuration only if it does not exist:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path market.local.json)) { Copy-Item market.example.json market.local.json }
```

Edit both files locally. The market example declares NSE, BSE, NFO, BFO, MCX, NCDEX and CDS. Missing verification dates, commodity/currency units, calendars and costs deliberately prevent trading. A declared segment does not establish broker access.

Verify each enabled segment or contract override:

- `scale`: native integer price units per INR. Master ticks are multiplied by the verified `master_tick_factor`. Currency prices can require sub-paise precision.
- Master lot size and `multiplier`, which converts native quoted price times quantity into INR notional. `depth_factor` and `volume_factor` normalise broker quantities into those same units; defaults are 1 and require independent validation.
- `verified_on`: metadata/unit verification date, valid for 90 days. `live_verified_on`: separate account/feed comparison date, valid for seven days for paper admission.
- `sessions`: exact dated timezone-aware `open` and `close` timestamps, including special sessions and the historical training period. Missing dates block trading; no calendar is guessed.
- `costs`: `valid_from`, `valid_until`, exact `buy_bps`/`sell_bps`, `flat_paise` and integer `slippage_bps`. Fractional cost rates use decimal strings. This is a dated all-in rate plus flat charge, rounded up to paise, rather than a complete broker tariff/tax calculator. Verify effective assumptions independently.
- Derivatives require a contract override under `contracts`, keyed by `EXCHANGE:token`, with exact timezone-aware `expiry` cutoff matching the master. Resolve commodity quotation units and multipliers per contract when they differ.
- BSE cash symbols lack NSE's `-EQ` suffix. Verify the security type independently and add `"kind":"CASH"` in its dated contract override; an empty master type alone does not identify equity rather than a debt or other security.

A `sessions` entry has this shape; substitute the verified exchange calendar:

```json
"2026-10-23": {
  "open": "2026-10-23T09:15:00+05:30",
  "close": "2026-10-23T15:30:00+05:30"
}
```

These times illustrate the format, not every segment's calendar. Economic/unit settings are hashed into dataset/model evidence. Changing them or historical session definitions invalidates earlier admission evidence. Verification-date refreshes and additive future calendar entries preserve it; freshness is checked separately.

Optional `contexts` maps a trading segment to an independently verified analytical index, for example `"contexts":{"NSE":{"exchange":"NSE","token":"99926000"}}`. Confirm that identity against the current master and terminal. Its matching 24 completed bars provide index-return inputs; absent or gapped context blocks that prediction. Collection subscribes to these indices alongside the bounded tradable shortlist. Indices use real LTP, allow a zero master tick, and have no executable depth or virtual fills. Configuration changes require a new dataset/evaluation.

## Collect and prepare

Missing or malformed market configuration stops before opening a journal or logging in. `collect` and `run` accept `--seconds` from 1 through 86,400; invalid durations stop before configuration, recording or authentication.

Global `--db`, `--config` and `--env-file` options precede the command. Public-master inspection needs no authentication:

```powershell
.\.venv\Scripts\python.exe ai_trader.py collect --metadata-only
.\.venv\Scripts\python.exe ai_trader.py report --output exports/readiness.json
```

After verifying configuration/account access, select past history boundaries and collect from the registered route:

```powershell
.\.venv\Scripts\python.exe ai_trader.py collect --history-from "2026-04-01T00:00:00+05:30" --history-to "2026-10-22T23:55:00+05:30" --seconds 3600
```

History uses at-most-100-day five-minute chunks, one scheduled request per second, and durable range watermarks. Broker errors stop collection without automatic retries or price fallback. Coordinate other processes sharing the client code. Empty ranges remain empty. Verify historical token/contract lifecycles; the current master cannot recover missing expired-contract history.

Add `--history-only` to backfill candles without a live feed, including outside market hours. Full dated instrument-master snapshots are compressed and archived privately so expiry/identity evidence is retained.

The recording shortlist selects up to two non-index contracts per segment by the immediately preceding dated session's traded notional and freezes for the session. Missing or incomplete prior-session data prevents selection; older sessions are not substituted. Recording can begin before model eligibility.

Quotes, displayed depth, cumulative volume, exchange/receipt timestamps, health transitions and dated metadata stay private. Recorded OHLCV derives from broker snapshots, not a complete exchange trade tape. Missing ticks/volume, partial starts, intraday gaps, possible corporate-action jumps and expiry cutoffs are flagged. Contracts remain separate across expiry. `data_issues` can additionally exclude known bad dates, keyed by instrument ID. Flagged or conflicting data requires review and a later clean dataset.

The multi-instrument feed observes each verified session window. Pre-open snapshots are ignored; one unavailable book blocks that instrument while other valid segments continue. Heartbeat/transport failures invalidate the whole connection. Generic contract events use explicit `bid_units`/`ask_units`/`ltp_units` and `price_scale`; the original NSE inspection API keeps its paise fields.

```powershell
.\.venv\Scripts\python.exe ai_trader.py prepare --output datasets/research-001
```

At least 100 complete dated sessions are required: the final 20 days are test, preceding 20 validation, and at least 60 earlier days training. Cuts are shared across instruments. Each segment separately needs 60/20/20 coverage. Consecutive, unflagged 24-bar windows predict the next completed five-minute close: UP above +10 bps, DOWN below -10 bps, otherwise FLAT. Targets cannot cross session/expiry cutoffs. Labels never appear in inputs. Dataset hashes prevent silent changes; use new output directories for later datasets.

## Train and import locally

```powershell
.\.venv\Scripts\python.exe ai_trader.py train --dataset datasets/research-001 --output models/benchmark-001 --device cpu --benchmark-only
```

The benchmark stops at 20 optimiser steps and is never promotion-eligible. Review measured time, memory and inference latency before a full run. Downloading the production base model is separate from the small offline checks.

Defaults: LoRA rank 8, alpha 16, dropout 0.05, q/v projections, learning rate 0.0002, batch 1, accumulation 16, one epoch, 1,024-token maximum, seed 42. Thinking is disabled; only assistant targets contribute to loss. Overlength examples fail instead of truncating. Frozen weights must stay unchanged and adapter weights must change. Artifacts contain dataset/configuration hashes, resolved base revision, parameters, dependency versions, measured statistics, checksums, adapter and merged safetensors.

Install [Ollama](https://docs.ollama.com/windows) locally. Obtain a reviewed [llama.cpp checkout](https://github.com/ggml-org/llama.cpp), using command-scoped `git -c core.longpaths=true clone` on Windows. Conversion was checked with commit `f5027fb21307eb264aed483242603d68c87b0fe3` and this project's separate export requirements:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-export.txt
```

Export F16 GGUF and import with:

```powershell
.\.venv\Scripts\python.exe ai_trader.py train --dataset datasets/research-001 --output models/candidate-001 --device auto --gguf-tools .tools/llama.cpp --import-ollama
ollama pull qwen3:0.6b
```

The model receives a unique `angel-paper:<artifact-id>` name. Evaluation/run checks the GGUF and installed model digest. Inference uses fixed loopback Ollama endpoints, strict direction JSON, disabled thinking, bounded output and no tools/cloud fallback. Changed model digests stop inference.

## Evaluate and activate

```powershell
.\.venv\Scripts\python.exe ai_trader.py evaluate --dataset datasets/research-001 --model-dir models/candidate-001 --split validation
.\.venv\Scripts\python.exe ai_trader.py evaluate --dataset datasets/research-001 --model-dir models/candidate-001 --split test
```

Validation precedes the final holdout. Interrupted evaluations resume saved predictions for the same artifact; another model cannot reuse that final holdout. Different candidates may use validation. Compare macro-F1 against training-majority, momentum and the unfine-tuned local baseline; also report accuracy and latency.

Execution returns require recorded executable depth available at receipt time. Candles cannot invent bid/ask or liquidity. Promotion requires better forecast results than every baseline, positive cost-adjusted returns exceeding momentum, complete recorded-book coverage without unresolved positions, and ten-second p95 inference. Historical candles alone normally cannot meet that gate.

```powershell
.\.venv\Scripts\python.exe ai_trader.py run --model-dir models/candidate-001 --mode shadow --seconds 28800
.\.venv\Scripts\python.exe ai_trader.py report --model-dir models/candidate-001 --output exports/session.json
```

PAPER_ENABLED additionally requires two subsequent complete shadow sessions without model/feed control failures and fresh local live verification. Start before the market opens and collect through close; short demos do not qualify. Segment states remain visible as COLLECTING, EVALUATING, SHADOW_READY or PAPER_ENABLED.

```powershell
.\.venv\Scripts\python.exe ai_trader.py run --model-dir models/candidate-001 --mode paper --seconds 28800
```

Defaults are ₹1 crore virtual cash, 25% maximum position notional, 75% total deployment, three positions and a 1% loss stop against opening daily equity. Positions are not topped up. Cash shares and option premiums are paid in full; futures reserve full notional without leverage. UP can open a long; exit at the next completed bar or earlier session/risk cutoff. Partial fills, book walking, queue priority and overnight settlement are unsupported.

Displayed quantity is shared across repeated snapshots with an unchanged book side, including advancing sequences and reconnects. Ask consumption resets only when the displayed ask changes; bid updates cannot replenish it, and the reverse applies to sells. Invalid or timed-out model output durably stops that model's new entries for the session; risk controls and fresh-price exits continue. Reports can inspect model files and saved evidence without an active inference server.

Sizing includes adverse tick-rounded slippage and fees. Displayed quantity must cover the complete lot-based request; consumed snapshot liquidity cannot be reused. Decisions/fills commit atomically and retries survive restart. Feed handling continues during inference. Disconnects, invalid output, timeouts, stale marks and expired targets stop entries. Missing prices preserve unresolved positions instead of inventing exits.

Recover existing virtual exits without a working LLM:

```powershell
.\.venv\Scripts\python.exe ai_trader.py run --mode paper --exit-only --seconds 3600
```

Exit-only recovery needs fresh executable prices and verified tradable contracts, but does not require analytical index inputs. Unchanged risk/equity observations are not repeatedly written during idle polling. Each rejected exit is retried only after a new quote arrives; unresolved positions remain visible.

Reports include coverage/gates, evaluations, cash/equity, realised results, costs, turnover, drawdown, latency, rejected signals and unresolved positions. Live model latency includes inference queue time and failed inference attempts; exits and other non-model decisions do not dilute that measurement. Fine-tuning or passing fixtures does not establish profitability or authenticated broker acceptance. Keep configuration, datasets, models, exports and journals private.

Terminal JSON renders cash amounts as exact decimal strings with `_inr` names. Exported report files preserve the underlying integer values with `_paise` names; native contract prices keep their explicit units and scale in both forms.
