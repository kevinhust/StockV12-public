# Architecture Overview

> Merged from L1-L5. For detailed deep-dives, see individual design docs.

## 1. System Topology

**V3.2 Modular Isolation Mode** — Each business stack has its own private infrastructure containers.

```
Mac Mini (<dev-host>)          NUC (<nuc-host>)
┌─────────────────────┐           ┌──────────────────────────────────────┐
│ Code editing only   │           │ Stock-Analysis Stack (stock-net)    │
│ CI / pytest         │           │  ├─ IB Gateway (stock_ibgw :4002)  │
│                     │           │  ├─ Shadow Runner :8003             │
│                     │  SSH      │  ├─ Pattern Scanner :8001           │
│                     │◄────────►│  ├─ Longbridge Streamer (primary)   │
│                     │  git     │  ├─ IBKR Streamer (CID 110, backup) │
│                     │          │  ├─ Alert Poller                    │
│                     │           │  ├─ Private Redis (6379)            │
│                     │           │  ├─ Private Kafka (9092)            │
│                     │           │  └─ Private TimescaleDB (5432)      │
└─────────────────────┘           └──────────────────────────────────────┘
```

### MagicDNS Access

| Resource | URL |
|----------|-----|
| Shadow Runner API | `http://nuc-host:8003` |
| Pattern Scanner API | `http://nuc-host:8001` |
| Grafana Dashboard | `http://nuc-host:3001` |

---

## 2. Business Architecture

### Core Philosophy

1. **Safety First**: `HardRiskGatekeeper` is deterministic rule-based. No AI can bypass it.
2. **AI as Graph Nodes**: MarketAnalyst and HeadTrader operate as LangGraph nodes, producing validated suggestions.
3. **Observability**: Every decision logged to TimescaleDB, Redis audit streams, and Prometheus metrics.

### Core Trading Loop

- **Market Data**: Longbridge (primary) + IBKR Gateway (Docker `stock_ibgw`, backup) → `ibkr_streamer` → Redis pub/sub + Kafka topics
- **Hard Risk Gatekeeper**: Validates every order against Max Drawdown, Daily Loss Limit, Position Limits, Kelly Criterion
- **Token Validator**: Cryptographic order authentication
- **Execution Engine**: Translates validated orders into IBKR API calls

### Agentic Layer (LangGraph StateGraph)

```
DataPrep → SuggestionConsumer → ExternalIntentConsumer
                                        │
                          ┌─────────────┼─────────────┐
                          ▼             ▼             ▼
                    MarketAnalyst   RiskValidation   (FORCE_LIQUIDATE)
                          │             │
                          ▼             │
                    HeadTrader          │
                          │             │
                          ▼             │
                    RiskValidation ◄────┘
                          │
                          ▼
                    TokenValidator
                          │
                          ▼
                       Execution
```

**Key Nodes:**
| Node | Purpose |
|------|---------|
| DataPrepNode | Fetches real-time market data from Redis |
| MarketAnalystNode | LLM-powered macro/sector analysis with RAG |
| HeadTraderNode | LLM-powered BUY/SELL/HOLD decision |
| RiskValidationNode | Wraps HardRiskGatekeeper |
| TokenValidatorNode | Cryptographic order auth |
| ExecutionNode | Routes orders to IBKR |

---

## 3. Data Architecture

### Kafka + Redis Dual-Bus

| Component | Technology | Purpose |
|-----------|------------|---------|
| Event Bus | Apache Kafka KRaft 3.7.0 | Persistent event streams |
| Hot Store | Redis 7 | Sub-ms state, pub/sub |
| Cold Store | TimescaleDB (PG15) | Time-series, 30-day rolling window |
| Vector DB | ChromaDB | RAG knowledge base |

### Redis Key Patterns

```
price:{symbol}:current           # Latest price (TTL 5min)
watchlist:tier1                  # L1 technical scan results (ZSET)
watchlist:tier2                  # L2 catalyst scored (ZSET)
watchlist:tier3                  # L3 YOLO/forced patrol (ZSET)
watchlist:current                # Bridge: tier2 → consumer format (JSON list)
market_regime:current            # Current regime (JSON)
shadow:portfolio:*               # Shadow positions
paper:portfolio:*                # Paper positions
```

### Cold Storage Strategy

- **Memory Target**: < 2GB total
- **Hot**: 30-day window in TimescaleDB
- **Cold**: Parquet files in `archive/ohlcv/`

---

## 4. Technology Stack

### Core

| Component | Technology |
|-----------|------------|
| Language | Python 3.10+ |
| Web Framework | FastAPI + Uvicorn |
| Agent Framework | LangGraph StateGraph |
| Broker API | ib_insync ≥0.9.86 |

### AI/ML

| Component | Technology |
|-----------|------------|
| RL Framework | Stable Baselines 3 (PPO/SAC) |
| Computer Vision | Ultralytics YOLO11 |
| LLM | DeepSeek / X.AI / OpenAI via LiteLLM |
| Vector DB | ChromaDB |

### Infrastructure

| Component | Technology |
|-----------|------------|
| Event Bus | Apache Kafka KRaft 3.7.0 |
| Hot Store | Redis 7-alpine |
| Cold Store | TimescaleDB latest-pg15 |
| Monitoring | Prometheus + Grafana |
| Containerization | Docker Compose v2 |

---

## 5. Market Regime

> Source: `config/risk_regimes.yaml` → `l2_catalyst_multipliers`

| Regime | Catalyst Multiplier | VIX Range |
|--------|-------------------|-----------|
| BULL_LONG | 1.2x | < 18 |
| BULL_SHORT | 1.1x | 15-22 |
| NEUTRAL | 1.0x | 18-25 |
| BEAR_SHORT | 0.9x | 18-28 |
| BEAR_LONG | 0.8x | 25-35 |
| PANIC | 0.5x (hard lock) | > 35 |

---

## 6. OpenClaw Supervisor

> **Status**: Disconnected from trading pipeline (commit `4bd1d7de`). SuggestionConsumerNode exists but is a no-op. Retained for potential future reactivation.

**Role**: Strategic advisor, NOT executor.

- Publishes `Suggestion` objects to Redis `supervisor:pending_queue`
- StockApp's `SuggestionConsumerNode` ingests suggestions (currently disabled)
- HeadTrader has **final authority** — can ignore any suggestion

**Suggestion Types:**
| Type | Purpose |
|------|---------|
| SUGGEST_TRADE | Suggest open/add position |
| SUGGEST_LIQUIDATE | Suggest close/reduce |
| SUGGEST_HALT | Suggest temporary halt |
| PROVIDE_INTEL | Provide market intelligence |

---

## 7. Current Stage

| Stage | Status | Description |
|-------|--------|-------------|
| Infrastructure | ✅ Done | Docker, Redis, Kafka, IBKR, TimescaleDB |
| Selection Funnel (L1-L3) | ✅ Done | IntradayScanner → CatalystScorer → Forced Patrol |
| Shadow Trading | ✅ Done | Shadow runner with 17 lifecycle jobs |
| Shadow Backtesting | ✅ Done | Stage 4.5 — simulate suggestion impact |
| Paper Trading | Active (Phase 0.1) | `paper_ibkr` mode, IBExecutor connected to Docker-internal IBGW |
| Live Trading | 📋 Planned | Full risk stack + paper validation first |

See `docs/_archive/SYSTEM_UPGRADE_PLAN.md` for the full phased upgrade roadmap (archived planning doc).

---

**Last Updated**: 2026-04-10

---

## Code Mainline & Archive Policy

- **Mainline (active)**: `src/` (entry: `src/main.py`), orchestrated by `src/services/lifecycle_scheduler.py`, configured by `config/trading_modes.yaml`.
- **Legacy (deprecated, do not run)**: moved to `archive/legacy/` (old systemd/launchd stacks, retired entrypoints).
- **Lab/Research (offline only)**: moved to `archive/lab/` (training briefs, notebooks, model training/backtest scripts).
