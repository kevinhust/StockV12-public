# StockApp V12 — Swing-Trading System (public snapshot)

> **Note:** This repository is a **sanitized snapshot** of a private, actively developed
> personal project. Secrets, credentials, account identifiers, host names, market data,
> trained model weights and logs have been removed or replaced with placeholders; the
> legacy V11 code is included only as a slimmed, non-runnable reference in `legacy/v11/`. Commit history is not included. Some scripts reference data/model
> paths that are intentionally absent, so this snapshot is meant for **code review**, not
> for running against a broker.

## What it does

StockApp V12 is a rule-gated **US-equity swing-trading system** (2–15 day holds) built
around a "silence is the safe default" philosophy: every failure path stops new orders
rather than trading through uncertainty.

- **Selection funnel** — daily-bar screening of a fixed liquid universe (swing scanner,
  Longbridge screener), followed by a multi-stage entry pipeline.
- **Trend analysis / forecasting** — Chronos time-series forecasts (single model and
  multi-seed ensemble backends behind a protocol), an optional chart-pattern filter, and
  a signal-rule engine (`SR-xx` rules, regime gate).
- **Risk gates** — a single `RiskGatekeeper` authority plus live guards for per-name and
  gross-exposure caps, daily entry caps, cooldowns, a NYSE trading-calendar guard,
  a Redis-backed kill switch and halt gate, all designed to fail closed.
- **Execution** — a `BrokerAdapter` port with a bracket-order service and an in-memory
  paper broker; real brokers plug in as adapters.
- **Position lifecycle** — explicit state machine, MFE/MAE tracking, atomic close, daily
  HOLD/TRIM/EXIT review, append-only trade journal and fsync'd audit ledger.
- **Shadow / paper trading** — shadow runners, a shadow model ensemble, walk-forward
  evaluation, champion-model selection with promotion gates, and paper-soak checklists.
  LLM output is advisory only; it never places orders.

## Architecture

Hexagonal modular monolith (see `docs/ADR-011-Hexagonal-Modular-Monolith.md`):

```
src/stockapp_v12/
├── contracts/   ports: Broker, MarketDataClient, ChronosBackend, Clock, Notifier, stores
├── core/        limits, sizing, decimal precision, NYSE calendar, execution (bracket, paper broker)
├── pipeline/    7-stage entry pipeline: guard → forecast → filter → signal rules →
│                analyst → live guard → gatekeeper → bracket → register
├── services/    entry orchestrator, position manager, daily review, forecast service,
│                swing scanner, signal rule engine, veto alarm
├── bridge/      gatekeeper wrapper, live guard, kill switch
├── adapters/    market data (Longbridge, composite, fake), Redis/memory stores, clock,
│                legacy_v11 (strangler seam to V11 code)
├── eval/        walk-forward metrics, quantiles, halt gate, champion selector/state
├── audit/       append-only audit ledger
├── notifiers/   log / Redis pub-sub / webhook backends
├── config/      typed (pydantic) config loaded from environment
└── app/         bootstrap, dependency wiring, CLI, cron entry points
scripts/         operational CLIs and cron wrappers (shadow runs, backtests, training, health)
tests/           pytest suite (unit + contract + e2e replay tests with fakes)
docs/            specs, ADRs, runbooks, architecture logs, review records
legacy/v11/      slimmed V11 reference snapshot (not runnable, not collected by pytest)
```

Import boundaries between layers are enforced by `scripts/check_import_boundaries.py`
and `tests/test_import_boundaries.py`.

**How V11 became V12:** see [`docs/ARCHITECTURE_EVOLUTION.md`](docs/ARCHITECTURE_EVOLUTION.md)
(V11 vs V12 components, data flow diagrams, motivating problems, design decisions and
trade-offs). The V11 code itself is in [`legacy/v11/`](legacy/v11/README.md).

Start with `docs/README.md` → `docs/SYSTEM_OVERVIEW.md`, `docs/TRADING_PIPELINE.md`,
`docs/RISK_MANAGEMENT.md`, `docs/POSITION_LIFECYCLE.md`.

## Tech stack

Python 3.11+ · pydantic · pandas / NumPy / PyArrow · Redis · loguru · pytest ·
AutoGluon-TimeSeries / Chronos (forecasting, optional) · Longbridge OpenAPI (market data) ·
cron on Linux (WSL) · GitHub Actions CI.

## Running the tests

```bash
uv venv -p 3.12 && source .venv/bin/activate
uv pip install pytest pytest-asyncio pytest-mock requests-mock pandas numpy pyarrow \
    pyyaml pydantic loguru redis fakeredis python-dotenv requests

# Fast CI subset (same as .github/workflows/ci.yml)
python scripts/check_import_boundaries.py --root . --strict
PYTHONPATH=src:scripts:. pytest -q tests/test_wiring.py tests/test_live_guard.py \
    tests/test_halt_gate.py tests/test_p1_9_red_line_rules.py

# Full suite
PYTHONPATH=src:scripts:. pytest -q
```

Tests that exercise real Chronos models need `autogluon.timeseries` (heavy, GPU-friendly)
and will fail/skip without it. A few tests depend on data fixtures or legacy V11 modules
that are not part of this snapshot.

## Configuration

All secrets come from environment variables; see `.env.example` for the full list of
placeholders. Never commit a real `.env`.

Risk limits are read from `config/risk_regimes.yaml`, which is deployed per host and not
shipped here; without it `stockapp_v12.core.limits` logs a warning and uses built-in
defaults. A reference copy with the same sections is at
[`legacy/v11/config/risk_regimes.yaml`](legacy/v11/config/risk_regimes.yaml).

## License

MIT — see `LICENSE`.
