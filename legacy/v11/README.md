# StockApp V11 — legacy reference snapshot

This folder is a **slimmed, read-only reference copy** of the V11 code base
(private archive `archive/v11-legacy-2026-07-09/`), kept so reviewers can see what
V12 replaced. It is **not runnable** and is **not collected by the V12 test suite**
(`norecursedirs = ["legacy", ...]` in the root `pyproject.toml`).

What was removed from the original archive: data, model artifacts and experiment
outputs, logs, notebooks, `.env` files, scripts, debugging notes, deployment notes,
and training/verification reports. Secrets, account identifiers, host names, IPs,
personal paths and IDs were scrubbed or replaced with placeholders.
The private archive also lacked some V11 packages (`src/core`, `src/agentic_layer`,
`src/infrastructure`), so imports of `src.core.risk_controller`,
`src.agentic_layer.*` and `src.infrastructure.*` do not resolve here. Some links
inside `docs/` point to documents that are not included.

## Layout

```
legacy/v11/
├── src/services/        V11 services (≈46 modules)
│   ├── lifecycle_scheduler.py        APScheduler job orchestrator (in-container)
│   ├── shadow_runner.py              shadow/paper trading loop driving the LangGraph crew
│   ├── selection/                    L1 intraday scanner, L2 catalyst scorer
│   ├── fusion_engine.py              signal fusion / sizing inputs
│   ├── pattern_*.py                  YOLO chart-pattern detection + monitor
│   ├── ibkr_realtime_streamer.py     IB Gateway (ib_insync) market-data streamer
│   ├── longbridge_realtime_streamer.py / longbridge_data_service.py
│   ├── connection.py, execution_service.py, liquidation_service.py, gap_risk_manager.py
│   ├── post_mortem_service.py, trade_recorder.py, reinforcement_service.py, rl_service.py
│   └── llm/                          multi-provider LLM clients
├── tests/               representative tests (pipeline flow, fusion weights, catalyst
│                        scoring, risk blacklist, EOD closing, IB protocol, e2e)
├── config/              risk regimes, trading modes, scanner configs (no secrets)
├── docker/              docker-compose stack (IB Gateway, Redis, Kafka, TimescaleDB, …);
│                        credentials come from environment variables only
├── prometheus/          scrape config
├── docs/                ARCHITECTURE.md, TRADING_PIPELINE.md, POST_MORTEM_PIPELINE.md
├── Dockerfile, requirements.txt, pyproject.toml
```

For how and why V11 evolved into V12, see
[`../../docs/ARCHITECTURE_EVOLUTION.md`](../../docs/ARCHITECTURE_EVOLUTION.md).
