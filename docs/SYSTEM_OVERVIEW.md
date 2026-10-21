# StockApp V12 — System Overview

> **Version**: 12.0  
> **Status**: Specification (implementation in progress)  
> **Baseline**: V11 snapshot in `/V11/`

This document is the **master spec**. It answers: what the system is, why it exists, what is in/out of scope, and how modules divide responsibility.  
Implementation detail lives in the linked documents only.

---

## 1. Goals

| ID | Goal | Success signal |
|----|------|----------------|
| G1 | Trade at **swing cadence** (2–15 days), not intraday noise | Avg hold ≥ 2 trading days; no forced EOD flat by default |
| G2 | **Reduce trade frequency** to discretionary levels | ≤ 3 new entries per day; most days 0 trades |
| G3 | Make **risk rules executable**, not YAML decoration | Every rule in [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md) has a runtime check + test |
| G4 | Make every trade **auditable** | Entry/exit/hold decisions logged with reason codes |
| G5 | Make **position lifecycle explicit** | Every open position has a known state and allowed transitions |

---

## 2. Non-Goals

V12 explicitly does **not** pursue:

| Non-goal | Rationale |
|----------|-----------|
| High-frequency or sub-minute trading | Conflicts with swing thesis and broker/API limits |
| Intraday pattern / YOLO-driven entries | V11 root cause of over-trading |
| Multi-strategy portfolio (pairs, options, sector rotation) | Scope creep; one edge first |
| Distributed event bus (Kafka) for daily jobs | Unnecessary complexity at this cadence |
| Black-box ML signal generation | LLM assists analysis; decisions remain rule-gated |
| Autonomous “always trade” behavior | **Silence is the safe default** on failure |

---

## 3. System Boundary

### In scope

- US equities, single-account swing book
- Daily-bar screening + pre-market catalyst filter
- Paper and live execution via IBKR (bracket orders)
- Redis-backed state; single-host scheduler on **NUC (Linux WSL on Windows)** — see [DEPLOYMENT.md](DEPLOYMENT.md) for the production host topology; the Mac mini is the dev/test surface only
- Post-close review and structured journal

### Out of scope

- Options, futures, crypto
- Multi-account / fund-level allocation
- Real-time sub-minute signal generation
- Training pipelines, backtest harness (remain in V11/archive)

### External dependencies

| Dependency | Role | Failure impact |
|------------|------|----------------|
| Redis | State, counters, watchlists | **Stop new orders** |
| IB Gateway / TWS | Execution, bracket orders | **Stop new orders**; existing brackets remain at broker |
| Polygon (or equivalent) | Daily bars, news | Degrade per [§6](#6-failure-degradation-policy) |
| LLM API | Catalyst + entry narrative | L2/entry may PASS symbols; no blind entries |

---

## 4. Architecture (Module Boundaries)

> **2026-06-19 更新**：03 Entry Layer 已 🟢 封板（完整真实组件 wiring + Risk Gate + Lifecycle 闭环）。详见 Obsidian `03-Entry-Layer.md`。

**Rule**: Each module has **one job**. No module may “fix” another module’s omission.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Scheduler (orchestrator)                  │
│  Triggers jobs only — no trading logic                          │
└────────────┬────────────────────────────────────────────────────┘
             │
   ┌─────────┼─────────┬──────────────┬──────────────┐
   ▼         ▼         ▼              ▼              ▼
SwingScanner  CatalystScorer  EntryOrchestrator  DailyReview
 (candidates)   (rank/filter)   (pre-eval + entry)  (hold/trim/exit)
   │         │         │              │
   └────┬────┘         │              │
        ▼              ▼              ▼
   watchlist:*    actionable:today   position actions (queued)
        │              │
        └──────┬───────┘
               ▼
        LLMAnalyst (structured analysis only — no sizing)
               ▼
        RiskGatekeeper (allow/deny + risk-based size)  ← sole entry authority
               ▼
        BracketOrderService (broker orders)
               ▼
        PositionManager (state transitions)
               ▼
        TradeJournal (append-only audit)
```

### Single responsibility matrix

| Module | Owns | Must NOT do |
|--------|------|-------------|
| **SwingScanner** | `watchlist:tier1` candidates from daily bars | Place orders; apply portfolio limits |
| **CatalystScorer** | `watchlist:tier2` ranking + catalyst metadata | Size positions; override risk |
| **EntryOrchestrator** | Per-symbol pre-eval + 7-stage entry pipeline (gap check, regime, cooldown, SR-01..SR-07, YOLO, R-03/R-04/R-05/R-06) → `ctx.actionable_symbols: Set[str]` flows in-process from caller (e.g. backtest harness reads `watchlist:tier2`); no Redis intermediate. `EntryContext` / `EntryResult` dataclasses live in `stockapp_v12.pipeline.types` (Sprint J5 — leaf module, no services dependency, breaks the services↔pipeline cycle) | Execute trades (delegated to BracketOrder) |
| **LLMAnalyst** | Structured `AnalystOutput` (see pipeline contract) | Set position size; bypass Gatekeeper |
| **RiskGatekeeper** | All hard gates + risk-based `size_pct` | Pick symbols; manage trailing stops |
| **BracketOrderService** | Submit entry + SL (+ trail registration) | Re-score candidates |
| **PositionManager** | State machine + trail ratchet + min-hold enforcement + MFE/MAE tracking (`lowest_close_since_entry` mirror of `highest_close_since_entry`) + atomic `_close_position` via redis pipeline() | Change scanner filters |
| **DailyReview** | HOLD / TRIM / EXIT recommendations | Silent forced exit without rules |
| **TradeJournal** | Immutable decision log | Influence live decisions |
| **Scheduler** | Cron + timeouts + failure hooks | Trading logic |

Detail: [TRADING_PIPELINE.md](TRADING_PIPELINE.md), [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md), [POSITION_LIFECYCLE.md](POSITION_LIFECYCLE.md), [SCHEDULER.md](SCHEDULER.md).

---

## 5. Core Principles

1. **Config = code** — Parameters in YAML must be loaded and enforced; undocumented constants are forbidden for risk limits.
2. **Fail closed** — On ambiguity or infrastructure failure, **do not trade** (see §6).
3. **Risk before alpha** — Gatekeeper runs after analyst but **cannot** be overridden by LLM output.
4. **Mechanical exits, discretionary entries** — SL at entry is mandatory; entries require confidence + gates.
5. **One parameter, one authority** — See parameter registry in [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md#parameter-authority-registry); pipeline-specific params in [TRADING_PIPELINE.md](TRADING_PIPELINE.md#parameter-authority-pipeline).

**Canonical rules (non-negotiable):**

- **LLMAnalyst outputs structured analysis only; it does not decide position size.**
- **Sizing is determined only by risk budget and stop distance; `confidence` does not enter the base sizing formula** (see [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md#position-sizing)).
- **Each position field has exactly one write owner; other modules may read or append to TradeJournal only.**

---

## 5.1 System Invariants

Global rules that **any** module must respect. Violations are bugs, not edge cases.

| ID | Invariant | Enforced by |
|----|-----------|-------------|
| INV-1 | At most **one entry evaluation** per `(mode, symbol, trading_date)` | Entry orchestrator + `entry:evaluated:{mode}:{date}` SET |
| INV-2 | No new entry if position `state ∈ {OPEN, TRAILING}` | PositionManager |
| INV-3 | No BUY outside entry window (R-09) | RiskGatekeeper |
| INV-4 | `size_pct` comes **only** from Gatekeeper risk formula | RiskGatekeeper |
| INV-5 | Every PASS / DENY / BUY / TRIM / EXIT logs explainability fields | TradeJournal |
| INV-6 | Scheduled jobs are **idempotent** per trading day (overwrite same keys) | Scheduler |
| INV-7 | Only TradeJournal **appends** audit; it never mutates position state | TradeJournal |

Detail: [TRADING_PIPELINE.md](TRADING_PIPELINE.md#system-invariants), [POSITION_LIFECYCLE.md](POSITION_LIFECYCLE.md#redis-key-registry).

---

## 6. Failure Degradation Policy

System-wide rule: **prefer inaction over wrong action.**

| Failure | Behavior |
|---------|----------|
| L1 (SwingScanner) fails | Use previous `watchlist:tier1` if age < 48h; else empty watchlist |
| L2 (CatalystScorer) fails | Use tier1 unfiltered; log `L2_DEGRADED` |
| Pre-eval (tier2→orchestrator) fails | `ctx.actionable_symbols` = empty → **no entries today** |
| Redis unavailable | Block all new orders; alert |
| Broker disconnected | Block new orders; existing bracket/SL orders remain at broker |
| LLM unavailable | Catalyst/entry → PASS; no entries without analyst confidence |
| DailyReview fails | All positions **HOLD**; no queued exits |

Full job-level matrix: [SCHEDULER.md](SCHEDULER.md#failure-handling).

---

## 7. Target Metrics (V12)

| Metric | V11 (observed) | V12 (target) | Verified by |
|--------|----------------|--------------|-------------|
| Decision events / day | ~430 | 5–20 | Scheduler + journal count |
| New entries / day | 5–12 | 0–3 | `risk:daily_trades:{date}` |
| Avg holding period | < 1 day | 2–15 days | TradeJournal |
| Concurrent positions | 10+ | 3–8 | `position:*:index` |
| Max daily loss | -5% code / -2% yaml | **-2% enforced** | R-01 test |
| Max single position | 5% code | **3% enforced** | R-03 test |

---

## 8. V11 → V12 Summary (Not Implementation Detail)

| Area | V11 | V12 |
|------|-----|-----|
| Cadence | Intraday events (pattern poll, L3 patrol) | 5 scheduled jobs / day |
| Holding | EOD force flat | Multi-day; SL + trailing + DailyReview |
| Risk YAML | Partially ignored | Fully wired + tested |
| Entry trigger | `events.pattern` storm | `actionable:today` + entry window |
| Infra | Kafka + multi-container NUC | Redis + single host |

Migration and rollback: [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md).

---

## 9. Coexistence, Rollback, and Release

| Topic | Policy |
|-------|--------|
| **V11 preservation** | Frozen copy under `/V11/`; not modified during V12 work |
| **V12 workspace** | `/v12/docs/` = spec; implementation may land in repo root incrementally |
| **Parallel run** | Do not run V11 lifecycle scheduler and V12 scheduler against same Redis namespace |
| **Rollback** | Stop V12 services → restore files from `/V11/` → flush V12-only Redis keys (see migration guide) → restart V11 scheduler |
| **Redis namespaces** | Prefer `v12:` key prefix or separate DB index when testing alongside legacy |

Key cleanup list: [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md#rollback-procedure).

---

## 10. Document Map

| Document | Use when you need… |
|----------|-------------------|
| [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md) | Goals, boundaries, module ownership, policies (this file) |
| [TRADING_PIPELINE.md](TRADING_PIPELINE.md) | Stage I/O, scoring, entry thresholds |
| [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md) | Red lines, sizing, parameter registry (risk) |
| [POSITION_LIFECYCLE.md](POSITION_LIFECYCLE.md) | States, transitions, Redis contract |
| [SCHEDULER.md](SCHEDULER.md) | Job times, dependencies, failures |
| [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md) | Phases, file map, acceptance tests, rollback |

Reference (non-normative): [_reference/INDUSTRY_REFERENCES.md](_reference/INDUSTRY_REFERENCES.md).

Open spec gaps: [TODO.md](TODO.md).

Change log: [../CHANGELOG.md](../CHANGELOG.md) — recent council-review remediation (Sprints F–K, 2026-07-09 → 2026-07-10) restructured the code (new `pipeline.types`, `services._notifier_compat`, `core.StageFallbacks`, `core.nyse_calendar`); this spec document is normative and tracks structural changes only when they affect ownership, invariants, or the dependency graph. See [reviews/council-review-2026-07-09/](reviews/council-review-2026-07-09/) for sprint-by-sprint code-level records.

---

## 11. Definition of Done (Program Level)

V12 is **release-ready** when all items in [MIGRATION_GUIDE.md — Acceptance Checklist](MIGRATION_GUIDE.md#acceptance-checklist) pass in `paper_ibkr` mode for ≥ 5 consecutive trading days without manual override of risk rules.
