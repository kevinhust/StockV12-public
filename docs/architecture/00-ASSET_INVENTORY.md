# V12 Asset Inventory — Phase 1 Discovery

> **⚠ SUPERSEDED (2026-07-09):** This document was the Phase 1 discovery
> artifact. It is preserved for historical traceability but is no longer
> the source of truth for V12 component inventory. For the current
> architecture, see [MODULE_MAP.md](../MODULE_MAP.md) and the live
> `src/stockapp_v12/` source tree.

**Status:** Drafted 2026-07-02 (workflow: Discovery → Council → Design → Implementation → Test)
**Audience:** Internal (self + future reviewers). Not a public-facing design doc.
**Update cadence:** Append-only. New components → new row in § 2. Coupling findings
evolve with phases 1.5 → 2, never silently overwritten.

> Purpose of this document: a single source of truth for **what exists** in V12
> today, **how it is wired**, and **where the boundaries are weak**. The next phases
> (council, design, implementation, test) all read from here. Update only when
> new code lands in `v12/src/stockapp_v12/` or when a coupling finding is
> confirmed/rejected by the council.

> **⚠ Scope authority:** § 5 (Coupling) and § 6 (Priority Queue) are
> **superseded** by `01-COUNCIL-LOG.md` § 3.4 (council verdict). They are
> preserved here for traceability. Pre-launch scope decisions read
> `01-COUNCIL-LOG.md`, not this file.

---

## 0. Reading Guide

The doc is organized to answer, in order:

1. **What is the repo surface?** (where files live)
2. **What are the components?** (12 grouped by domain, with LoC and test count)
3. **Are components clean or tangled?** (8 coupling findings ranked)
4. **What is the data flow?** (master pipeline diagram + Redis contract)
5. **What must be fixed first?** (priority queue B1..B5)
6. **What is out of scope?** (frozen/archived/external)
7. **Open questions** to bring into Phase 2 Council

Each component row in § 2 uses the same schema: name → module(s) → LoC est. →
boundary clarity (✅ / ⚠️ / ❌) → test coverage (✅ / ⚠️ / ❌). Consistency makes
the table machine-greppable later.

Boundary clarity:

- ✅ **Clean** — has a Protocol/dataclass contract, single responsibility, no cross-layer imports
- ⚠️ **Mixed** — internally clean but pulls in cross-layer dependencies (Network, Redis, etc.)
- ❌ **Tangled** — god-object or fan-out > 5 callers; refactor target

---

## 1. Repo Surface Map

```
/Users/dev/Stockapp/
├── v12/                           ← ACTIVE refactor target
│   ├── config/        1 file (README.md only — all real configs live in repo_root/config/)
│   ├── docs/          19 specs/ADRs/runbooks/notes
│   ├── scripts/       29 .py + 4 .sh
│   ├── src/stockapp_v12/   ← 14 modules across 5 packages (see § 2)
│   ├── tests/         27 tests
│   └── wsl_archive/   (frozen NUC snapshot, untouched)
│
├── src/              ← SHARED layer (V11 legacy + scheduler + data + chroma + monitoring)
│   ├── services/      (lifecycle_scheduler.py, chronos_service.py live here)
│   ├── data/          chroma_db
│   └── monitoring/, llm/, selection/
│
├── config/           ← 11 real config files (risk_regimes.yaml, swing_scanner.yaml, etc.)
│
├── data/             ← D1 features, intraday images, YOLO charts, polygon raw, redis AOF
│
├── models/chronos/   ← baseline_v1, baseline_v1_zeroshot, phase2_v1, phase2_v2, _experiments/, _shadow/
├── models/yolo/      ← phase2_v1, phase2_v1_gpu, pilot_v1
│
├── docs/_archive/    ← V11-era history (read-only reference)
├── archive/          ← V11, V3-migration, legacy scripts (frozen for historical diff)
├── V11/              ← frozen last-gen implementation (do NOT touch)
├── deploy/ + docker/ + logs/ + prometheus/ + reports/ + scripts/(root)/   ← infra (unchanged)
└── AutogluonModels/  ← training cache (ag-20260621...)
```

**Observation:** V12 owns the strategy + signal + risk + execution surface.
V11/`src/` owns infrastructure (scheduler, data clients). The two are
**physically separate** but **logically coupled via `sys.path` imports** (see T1 in § 7).

---

## 2. Component Inventory (12 components, grouped by domain)

| # | Component | Module(s) | LoC est. | Boundary | Test cov. |
|---|---|---|---:|:---:|:---:|
| **C1** | **Data layer** | `services/feature_loader.py` (~132)<br>`scripts/feature_engineering.py`<br>`scripts/cache_d1_bars.py`<br>`scripts/backfill_30_days.py`<br>`scripts/backfill_sector_spy_features.py` | ~1000 | ✅ | ✅ |
| **C2** | **Scan layer** | `services/swing_scanner.py` (~540)<br>`services/longbridge_screener.py` (~190) | ~730 | ⚠️ | ✅ |
| **C3** | **Predict layer** | `services/forecast_service.py` (~190)<br>`services/ensemble_chronos_backend.py` (~250)<br>`schemas/forecast.py` (~110)<br>`scripts/eval_chronos_h2h_v3.py`<br>`scripts/eval_chronos_headtohead.py`<br>`scripts/train_chronos_*` (3 files)<br>`scripts/ensemble_predict.py` | ~3500 | ⚠️ | ✅ |
| **C4** | **Signal layer** | `services/signal_rule_engine.py` (~200)<br>`scripts/threshold_tune.py` | ~600 | ✅ | ✅ |
| **C5** | **Entry orchestration** | `services/entry_orchestrator.py` (~280)<br>`pipeline/entry_guard.py` (~110)<br>`pipeline/analyst.py` (~135)<br>`services/yolo_filter.py` (~330) | ~850 | ❌ | ✅ |
| **C6** | **Risk layer** | `bridge/gatekeeper.py` (~115)<br>`core/limits.py` (~80)<br>`core/sizing.py` (~85) | ~280 | ✅ | ✅ |
| **C7** | **Execution layer** | `core/execution/bracket_order.py` (~135)<br>`core/execution/paper_broker.py` (~70) | ~205 | ✅ | ✅ |
| **C8** | **Position layer** | `services/position_manager.py` (~500)<br>`services/daily_review.py` (~190) | ~690 | ⚠️ | ✅ |
| **C9** | **Observability** | `services/notification_service.py` (~45)<br>`pipeline/journal.py` (~205) | ~250 | ⚠️ | ⚠️ |
| **C10** | **Shadow / backtest harness** | `scripts/shadow_trade.py`<br>`scripts/shadow_ensemble.py`<br>`scripts/backtest_chronos_daily.py`<br>`scripts/backtest_v12_paper.py`<br>`scripts/eval_chronos_h2h_v3.py`<br>`scripts/eval_chronos_headtohead.py`<br>`scripts/run_experiment.py`<br>`scripts/validate_ensemble_gate.py`<br>`scripts/validate_longbridge_screener.py`<br>`scripts/validate_regime_gate.py`<br>`scripts/yolo_hit_rate_validation.py`<br>`scripts/v12_soak_daily.py`<br>`scripts/v12_deployment_health.py`<br>`scripts/paper_soak_audit.py`<br>`scripts/paper_soak_checklist.py` | ~6000+ | ❌ | ✅ |
| **C11** | **Training infra** | `scripts/train_chronos_baseline.py`<br>`scripts/train_chronos_v3_1M.py`<br>`scripts/train_yolo_pilot.py`<br>`scripts/prepare_yolo_dataset.py` | ~3000 | ✅ | ⚠️ |
| **C12** | **Cross-layer glue** | `src/services/lifecycle_scheduler.py` (system-wide cron)<br>`v12/src/stockapp_v12/__init__.py` (facade) | ~3000 | ❌ | ⚠️ |

**Boundary summary:** 4 clean, 4 mixed, 4 tangled. The tangled ones (C5, C10, C12)
are the **refactor targets**; the clean ones (C1, C4, C6, C7, C11) are the
**stable backbone** around which to refactor.

### 2.1 What's *already good* (preserve during refactor)

- `schemas/forecast.py` — frozen Pydantic + `extra="forbid"` + `assert_no_trade_action_fields` guardrail
- `core/sizing.py` — confidence-abstraction-in-core is already done
- `core/limits.py` — single YAML loader + frozen dataclass, no mutation paths
- `core/execution/bracket_order.py` — `BrokerAdapter` Protocol defines a clean seam
- `pipeline/analyst.py` — strict JSON parse + rejects `side ∉ {BUY, PASS}`

### 2.2 Tests inventory (`v12/tests/`)

27 test files, organized roughly by component:

| Test file | Component | Covers |
|---|---|---|
| `test_feature_loader.py` | C1 | schema drift, missing parquets, lookback |
| `test_swing_scanner.py` | C2 | filters + scoring (light) |
| `test_forecast_output_schema.py`, `test_forecast_service_degraded_log.py`, `test_ensemble_chronos_backend.py`, `test_ensemble_forecast_service_integration.py`, `test_eval_chronos_h2h_v3.py` | C3 | schema + degraded path + ensemble fallback |
| `test_signal_rules_sr01_default.py`, `test_signal_rules_sr01_sr05.py`, `test_signal_rules_sr02_default.py`, `test_signal_rules_sr07_regime_gate.py` | C4 | SR-01..SR-05, SR-07 |
| `test_entry_orchestrator.py`, `test_yolo_filter_sr06.py`, `test_lifecycle_scheduler_yolo_filter.py`, `test_inv1_single_eval_per_day.py` | C5 | entry chain, YOLO, INV-1 |
| `test_sizing_no_confidence_in_core.py`, `test_gatekeeper_v12_sizing_meta.py` | C6 | sizing + bridge meta |
| `test_bracket_order_service.py`, `test_paper_soak_sign_agreement.py` | C7 | bracket + paper sign agreement |
| `test_position_lifecycle.py`, `test_daily_review_chain.py`, `test_lifecycle_entry_window_features.py`, `test_r05_daily_cap.py`, `test_r06_cooldown.py` | C8 | PM + R-05 daily cap + R-06 cooldown |
| `test_analyst_output_schema.py` | C5 (analyst pipeline) | AnalystOutput parse |
| `test_writer_contract.py` | C9 | journal writer contract |
| `test_shadow_trade.py`, `test_shadow_ensemble.py`, `test_run_experiment.py` | C10 | harness behavior |

**Gaps:** no live-data integration test, no Scheduler-level e2e test, no
mutated-state regression test for PM. These are the **test plan candidates**
for Phase 5.

---

## 3. Configuration Map

The V12 subdirectory has only one config file:

```
v12/config/README.md     ← placeholder pointing at repo_root/config/
```

All real configuration lives at `repo_root/config/` (`/Users/dev/Stockapp/config/`):

| File | Owner | Consumer(s) | Stability |
|---|---|---|---|
| `risk_regimes.yaml` | C6 (limits.py) | `gatekeeper.py`, `sizing.py`, `forecast_service.py` (regime hint) | Frozen for V12 (per V12RiskLimits defaults) |
| `swing_scanner.yaml` | C2 | `swing_scanner.py` | Tunable |
| `scanner_config.yaml` | C2 (legacy?) | unknown — verify use | candidate deprecate |
| `universe.yaml` / `universe.json` / `tickers_v19.json` | C2 | swing_scanner | overlap; pick one |
| `paper_soak.yaml` | C10 | backtest_v12_paper, paper_soak_* | Phase 5 |
| `trading_modes.yaml` | C5/C7 | lifecycle_scheduler? | verify |
| `polygon_data.yaml` | C2 | swing_scanner (Polygon fallback) | stable |
| `data_pipeline.yaml` | C11 | feature_engineering | stable |
| `dynamic_overrides_2026-02-17.json` | misc | historical override | deprecate? |

**Observations:**

- **Config drift:** three overlapping universe files. Need to pick one source.
- **Location:** v12 logically owns configs but no file lives in `v12/config/`.
  Path resolution goes through `core/paths.py` which walks parents up to REPO_ROOT.
  This is a **migration aid** that should disappear in v1.0 (configs should live
  with the code they configure).

---

## 4. Data Flow (Master Pipeline)

```text
            (cron — drives everything)
                     │
                     ▼
   ┌──────────────────────────────────────────┐
   │ src/services/lifecycle_scheduler.py      │   ← C12 (GLUE — sys.path imports)
   │   AsyncIOScheduler + cron triggers (EST) │
   │   task jobs: scanner / entry / review /  │
   │   shadow_trade / shadow_ensemble         │
   └────────────┬─────────────────────────────┘
                │
     ┌──────────┼──────────┐
     ▼          ▼          ▼
[02:30 SwingScanner]  [03:55 macro_light]  [16:05 shadow_trade]  [16:10 shadow_ensemble]
   │            │                                  │                  │
   ▼            ▼                                  ▼                  ▼
watchlist:    market_regime:               models/chronos/_shadow/   models/chronos/_shadow/
tier1 ZSET    current (JSON)                  YYYY-MM-DD.json         ensemble/
   │            │                                  │                  │
   └─────┬──────┘                                  │                  │
         ▼                                         ▼                  ▼
[04:05 pre-eval caller] tier1→tier2→               ADR-008 gate (PnL>0, MASE≤v2×1.05,
   │  ctx.actionable_symbols (in-process)        win_rate≥0.40, n_trades≥30)
   ▼
[09:35 EntryOrchestrator] — for each symbol in ctx.actionable_symbols
   │
   ├─[1] EntryEvaluationGuard   ← C5 (INV-1 single eval/day, INV-2 no duplicate entry)
   ├─[2] ForecastService        ← C3 → ChronosBackend Protocol → chronos_service.py (sys.path)
   ├─[3] YoloNoTradeFilter      ← C5 (optional, t-1 chart, tuned-class thresholds)
   ├─[4] SignalRuleEngine       ← C4 (SR-01..SR-07)
   ├─[5] LLMAnalyst             ← C5 (optional narrative; BUY/PASS only)
   ├─[6] Gatekeeper.compute_position_size  ← C6 (R-05 daily cap, R-06 cooldown, regime multipliers)
   ├─[7] BracketOrderService    ← C7 → PaperBroker.submit_bracket_order
   ├─[8] PositionManager.register  ← C8 (writes position:{mode}:{sym}, :index)
   └─[9] TradeJournal.log_buy   ← C9 (Redis LIST journal:{date}:{sym})

[16:15 DailyReview] — Stage 6 (every open position)
   │
   ├─ PositionManager.list_open_positions
   ├─ decision matrix (regime + pnl_atr + days_held) → HOLD | TRIM | EXIT | TIGHTEN | ALERT
   ├─ if apply_immediately: PM.trim_position / PM.close_for_review  (C8)
   ├─ TradeJournal.append (stage="review")                            (C9)
   └─ notifier.notify_daily_review                                    (C9 — currently stub)

[Next day 02:00 DailyReview draft] — Stage 7
   └─ read pending_action, regenerate actionable:today (skip if PANIC)

[Shadow 16:05 + 16:10] — C10
   ├─ shadow_trade.py     → per-model results
   └─ shadow_ensemble.py  → A+B ensemble results
       ▼
   ADR-008 gate (PnL > 0, MASE ≤ v2 × 1.05, win_rate ≥ 0.40,
                 n_trades ≥ 30, n_dates ≥ 30)
```

### 4.1 Redis Key Contract

Read by every component; documented here so refactors don't break the wire format:

| Key | Type | Owner | Consumers |
|---|---|---|---|
| `watchlist:tier1` | ZSET | SwingScanner | (audit) |
| `watchlist:tier1:details` | HASH | SwingScanner | (audit) |
| `watchlist:tier2` | ZSET | (external) | (audit) |
| `watchlist:tier2:details` | HASH | (external) | (audit) |
| `market_regime:current` | STRING (JSON) | macro_light | Gatekeeper |
| `position:{mode}:{symbol}` | HASH | PositionManager | EntryOrchestrator, DailyReview |
| `position:{mode}:index` | SET | PositionManager | (audit) |
| `risk:daily_trades:{date}` | STRING (counter) | Gatekeeper | Gatekeeper (R-05) |
| `risk:cooldown:{symbol}` | STRING (epoch or duration) | Gatekeeper | Gatekeeper (R-06) |
| `entry:evaluated:{mode}:{date}` | STRING (TTL 48h) | EntryEvaluationGuard | EntryEvaluationGuard (INV-1) |
| `journal:{date}:{symbol}` | STRING (JSON list) | TradeJournal | (audit) |
| `lifecycle:last_run:{task}` | STRING | lifecycle_scheduler | (monitoring) |
| `lifecycle:stats:total:{task}` | COUNTER | lifecycle_scheduler | (monitoring) |
| `lifecycle:stats:failure:{task}` | COUNTER | lifecycle_scheduler | (monitoring) |
| `lifecycle:last_error:{task}` | STRING (JSON) | lifecycle_scheduler | (monitoring) |

**TTL convention:** every daily key is 48h / 86400s. Keys spanning multi-day
positions (cooldown, lifecycle stats) live until overwritten.

---

## 5. Coupling Analysis (initial — Phase 1.5 preview)

> Full multi-voice review happens in Phase 2 (Council); this section only
> flags concrete entanglement points and ranks them. Phase 2 will accept,
> reject, or re-rank each finding.

### 5.1 Tangled spots (ranked)

| ID | Title | Where | Severity (1-10) | One-line description |
|---|---|---|:---:|---|
| **T1** | **sys.path cross-package import** | `lifecycle_scheduler.py:23-30`<br>`forecast_service.py:52`<br>shadow scripts | **9** | V12 src reaches into V11/`src/services/` via sys.path hacks; packaging/tests/Docker all break |
| **T2** | **EntryOrchestrator is god object** | `entry_orchestrator.py` | **8** | Single `evaluate_and_maybe_enter()` fans out to 8 components; tests need 8 mocks |
| **T3** | **Shadow scripts duplicate production logic** | `shadow_trade.py`, `shadow_ensemble.py`, `eval_chronos_h2h_v3.py` | **8** | `predict_one_symbol_for_date`, `simulate_pnl`, `hit_rate`, `build_test_tsdf_full` re-implemented 3x |
| **T4** | **Scan layer mixes network clients** | `swing_scanner.py:48-65` | **7** | Direct `from polygon import RESTClient` + `from services.longbridge_data_service` inside the scanner — no data-client layer |
| **T5** | **DailyReview orchestrator design** | `daily_review.py` | **3** | Loops positions, talks to PM + journal + notifier; no shared `ReviewContext` base. *(PreMarket half resolved in hygiene-A `6d5a2bf8`: module removed; cron wrapper + Redis ownership scrubbed in hygiene-I.)* |
| **T6** | **Configuration scattered** | `core/paths.py`, `core/limits.py`, `swing_scanner.py` | **6** | v12 owns configs logically but configs live in repo_root; reverse path walk |
| **T7** | **Notifier is stub** | `notification_service.py` | **5** | All "alerts" are loguru; no persistence, no dedup, no metric |
| **T8** | **YOLO filter forward-ref string** | `signal_rule_engine.py` | **5** | `YoloFilterDecision` referenced by importlib forward-ref; typecheck & tests need sys.modules hack |

### 5.2 Why each one matters

**T1 — sys.path cross-package import.** The single biggest blocker for
shipping V12 as a real Python package. Three places — the scheduler, the
forecast service, and the shadow scripts — all assume `from services.chronos_service import ChronosService`
works because the user (or scheduler) puts `Stockapp/` on `sys.path` and then
the relative `src/services/` is reachable. Remove that assumption and the
production v12 package cannot import its prediction backend. The right fix
is a `MarketDataBackend` Protocol in v12 itself; V11 ChronosService becomes
an optional injected adapter (rather than the only path).

**T2 — EntryOrchestrator god object.** Single 60-line method doing 8 things
sequentially: guard, forecast, YOLO, SR, analyst, gatekeeper, bracket, register,
journal. Every change requires building 8 mocks. A refactor to a stage chain
(`guard → forecast → yolo → signals → analyst → size → bracket → register → journal`)
where each stage returns a typed decision object would make the orchestrator
3 lines per stage and let us test any single stage in isolation.

**T3 — Shadow scripts duplicate production.** `simulate_pnl` exists in
`shadow_trade.py` (the source of truth for live numbers), is re-imported by
`shadow_ensemble.py`, and `hit_rate` is imported by both from
`eval_chronos_h2h_v3.py`. Anyone editing one of these three must remember
the other two copies. The shadow values are the gate that decides whether
to promote code — drift here means promoting something whose numbers
were computed by a different implementation than what production will run.

**T4 — Scan layer + data clients = no seam.** If Polygon changes an SDK
major version, or Longbridge opens a new endpoint, we must edit the
scanner. A `MarketDataClient` Protocol + adapter (Polygon impl, Longbridge
impl) lets the scanner keep scoring logic stable while data plumbing
moves.

**T5 — One review, no shared base.** *(PreMarket half resolved in
hygiene-A `6d5a2bf8`: module + cron + Redis ownership removed; re-purposed
to caller-side pre-eval in hygiene-J `pending`.)* `DailyReview` is a
~190-line wrapper around `for pos in pm.list_open_positions(): do things`.
It talks to PM + journal + notifier directly. Co-evolution risk: when the
journal schema changes, this one file must update.

**T6 — Configs live in repo_root.** Logically odd; physically fragile. The
"reverse path walk" pattern in `core/paths.py` couples v12 to its specific
position in the repo. Refactor target: v12-owned config dir (we just made
`v12/config/` — empty except a README; that's where configs should land
before v1.0).

**T7 — Notifier stub.** Currently every "notify" is `logger.info(...)`.
We are confident in our logging on the macbook, but in production on the
NUC + with shadow going live, we need at least a structured sink (Redis
list or pubsub channel) for the user/DailyReview/Entry logs to be inspectable
post-hoc.

**T8 — YOLO forward-ref string.** Minor compared to the others. It's a
typing-time forward ref that needs `from __future__ import annotations` +
either explicit `TYPE_CHECKING` import or string literal. Fix is small
but the test (`test_yolo_filter_sr06.py`) currently relies on the
current pattern.

### 5.3 Stable seams (preserve, don't churn)

- `ChronosBackend` Protocol + `ForecastService` adapter (good Protocol design)
- `BrokerAdapter` Protocol (paper + future IBKR side-by-side)
- `V12RiskLimits` immutable dataclass + YAML loader
- `ForecastOutput` (forbidden action fields guard)
- `AnalystOutput` strict parse

These five are the **stable contracts**. Any refactor must keep their public
signatures intact (re-export shim if a module moves).

---

## 6. Priority Queue for Pre-Launch

Ranked by **what blocks "go to paper"** (not by line count).

| # | Title | Source coupling | Acceptable to defer? | Risk if deferred |
|---|---|---|:---:|---|
| **B1** | Define `v12.contracts.MarketDataBackend` Protocol; de-couple v12 src from `services.chronos_service` direct import | T1 | No (blocks packaging) | Cannot package v12 as wheel; Docker image fragile |
| **B2** | Extract `predict_one_symbol_for_date`, `simulate_pnl`, `hit_rate`, `build_test_tsdf_full` to `v12.evaluation/`; import from there in shadow scripts | T3 | No (blocks promotion correctness) | Shadow gate decision made on numbers that don't match production |
| **B3** | Refactor EntryOrchestrator into a stage chain with typed decision objects between stages | T2 | Yes (post-launch OK) | Hard to debug future signal changes |
| **B4** | Introduce `MarketDataClient` Protocol + adapter layer for SwingScanner | T4 | Yes (post-launch OK) | Future SDK upgrades require touching scoring code |
| **B5** | Replace Notifier stub with at least a Redis-pubsub sink; keep the logger pass-through for tests | T7 | Yes (post-launch OK) | Alerts invisible post-mortem |

**B1 and B2 are pre-launch required.** B3/B4/B5 are post-launch OK.

---

## 7. Out of Scope (do NOT touch in this review)

1. **`V11/`** — frozen last-gen snapshot. Read-only reference.
2. **`archive/`** — all subfolders (`backtest_legacy/`, `v3_migration_20260318/`,
   `legacy/`, `rl_layer/`, etc.). Frozen for historical diff.
3. **`data/`** — raw features + images + Redis AOF. Replaced by C1 backfill scripts.
4. **`models/`** — model artifacts. Owned by C11 training scripts.
5. **`AutogluonModels/`** — training cache; autogluon-managed.
6. **`prometheus/`, `docs/_archive/`, `reports/`, `deploy/`, `docker/`** —
   infra observability/docs; not V12 strategy.
7. **`src/services/`** is partly in-scope: `lifecycle_scheduler.py` and
   `chronos_service.py` are coupling sources. Either treat them as
   "thin shell to be migrated" (preferred) or stay coupled but mark it.
   Decision deferred to Phase 2 (Council).

---

## 8. Open Questions for Phase 2 Council

These are the questions to take into the multi-voice review. The council
will decide each; outcomes land in `01-COUNCIL-LOG.md`.

1. **Q1.** Should the V12 package own its own `ChronosBackend` Protocol that
   V11 ChronosService implements (drop-in adapter), OR should we freeze the
   current sys.path reliance until post-launch?
2. **Q2.** Should shadow scripts move into v12 src (`v12/evaluation/` + entry
   points) and become first-class V12 modules, OR keep them at `v12/scripts/`
   with re-exports from src helpers?
3. **Q3.** EntryOrchestrator refactor: stage chain with typed `StageDecision`
   objects, OR strategy-pattern with explicit `Stage` interface? (C5)
4. **Q4.** SwingScanner data client extraction scope: just Longbridge +
   Polygon, or include File/Synthetic adapters for testing? (C2)
5. **Q5.** Position layer (C8) cleanup: do we extract a `ReviewContext` base
   for Premarket+DailyReview, OR is the duplication acceptable as long as
   the test coverage holds?
6. **Q6.** Configuration lift: copy relevant yaml/json from `repo_root/config/`
   into `v12/config/` and switch path resolution, OR leave shared?
7. **Q7.** Notifier scope: in-memory structured log + Redis pubsub (light),
   OR full Slack/email/email-DLQ integration? (For pre-launch: pubsub only.)
8. **Q8.** Scope check: is the current shadow gate (ADR-008) acceptable as
   the post-refactor acceptance criterion, OR do we need a per-component
   test matrix on top of it?

---

## 9. Update Protocol

When code lands in `v12/src/stockapp_v12/` that changes component inventory:

1. Update § 2 row for affected component (LoC + boundary + tests).
2. If coupling finding from § 5 changes status, append a row to § 5.1 with
   the new state (do NOT edit the historical row).
3. If a new Redis key is added, append to § 4.1.
4. If a new cron job is added, append to § 4 data flow diagram.
5. If § 8 question is resolved, move to `01-COUNCIL-LOG.md` with the
   decision; in § 8 mark `resolved (see 01-COUNCIL-LOG.md)`.

When code lands in `src/services/` that touches v12:

1. Append a new § 1 entry for the cross-boundary mutation.
2. § 2 component whose seam moved (e.g. C12) gets a LoC bump.

---

## 10. Changelog

- **2026-07-02** — Initial draft covering repo surface, 12 components,
  initial coupling analysis (T1..T8), priority queue (B1..B5), 8 council
  questions. Authored from session-conversation synthesis. Source-of-truth
  for Phase 2 (Council).
