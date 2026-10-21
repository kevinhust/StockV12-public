# Architecture Decoupling Upgrade — Design Spec

| Field | Value |
|-------|--------|
| **Project** | StockApp V12 |
| **Date** | 2026-07-26 |
| **Status** | Accepted 2026-07-26 |
| **Author role** | Software Architect (industry practice) |
| **Related** | `docs/MODULE_MAP.md`, `docs/SYSTEM_OVERVIEW.md`, `docs/architecture/02-DECOUPLING-PLAN.md`, ADR-010, Gate Decision Architecture |
| **Decision** | User chose: **full architecture blueprint + phased migration plan** (no big-bang code move yet) |

---

## 0. Executive Summary

StockApp V12 is already a **modular monolith with partial ports** (Protocols, DI orchestrator, staged pipeline). It is **not** a fully decoupled system: runtime V11 archive, Redis key sprawl, reverse `pipeline→services` imports, and a fat `services/` bag remain the main upgrade friction.

**Target end-state (industry best practice for this scale):**

> **Hexagonal Modular Monolith** — one deployable unit, hard package boundaries, ports for I/O, Strangler Fig elimination of archive, no microservices until operational pain proves the need.

**Success means:** replace Chronos backend / market data / broker / notifier / store without editing entry orchestration; delete `archive/` from production PYTHONPATH; upgrades ship behind adapters and versioned contracts.

---

## 1. Context & Constraints

### 1.1 Current strengths (preserve)

- Domain SR matrix in `SYSTEM_OVERVIEW` (Scanner → Orchestrator → Gatekeeper → Bracket → PM → Journal)
- `EntryOrchestrator` constructor injection
- `contracts.ChronosBackend`, `contracts.Notifier`, `BrokerAdapter`
- `pipeline.types` leaf DTOs (partial cycle break)
- `eval/` isolation for shadow / H2H / gates
- Fail-closed risk culture, INV-1..7, ADR gate discipline

### 1.2 Current debts (drive the upgrade)

| ID | Debt | Impact on upgrade |
|----|------|-------------------|
| D1 | Runtime `archive/` + `sys.path` for Chronos / Longbridge / risk | Cannot delete legacy; path-sensitive deploys |
| D2 | `services/` is a capability bag | High blast radius for any service change |
| D3 | `pipeline` still imports `services` types / notifier | Stage layer not replaceable alone |
| D4 | `MarketDataClient` Protocol unwired | Data-source swaps edit scanner guts |
| D5 | Redis used as shared global DB without store ports | State schema changes touch many files |
| D6 | `scripts/` second code plane + path hacks | Ops logic drifts from library API |
| D7 | Scheduler outside V12 package | Deploy topology drifts from app version |

### 1.3 Hard constraints (non-negotiable)

1. **Paper-soak / shadow continuity** — no silent change to ADR-008 metrics, decision path ensemble A+B, HaltGate paper-only.
2. **INV-1..7 and R-01..R-10** remain green at every phase gate.
3. **No big-bang rewrite** of trading semantics in a single PR.
4. **Fail closed** stays the default degradation policy.
5. Single-host NUC deployment remains the production topology for this plan (microservices not required).

### 1.4 Explicit non-goals

- Kafka / event-bus redesign
- Multi-service Kubernetes split
- Rewriting Chronos training stack
- Changing Gate Decision Architecture L0/L1/L2/D semantics
- Auto-promote models without L2 + human

---

## 2. Approach Options (evaluated)

### Option A — Microservices split

Split scan / forecast / risk / execution into separate processes + network APIs.

| Pros | Cons |
|------|------|
| Independent deploy | Ops complexity ≫ team size |
| Language/runtime freedom | Latency + partial failure modes |
| | Rewrites INV/Redis semantics |

**Verdict:** Reject for V12. Cadence is daily swing; single host is intentional (SYSTEM_OVERVIEW).

### Option B — Hexagonal Modular Monolith + Strangler Fig (**Recommended**)

One package namespace tree, **enforced import rules**, ports at edges, adapters hold I/O and legacy.

| Pros | Cons |
|------|------|
| Matches industry practice for domain apps | Requires import lint discipline |
| Incremental, soak-safe | Takes multiple sprints |
| Keeps one deployable | Not “polyglot free” |

**Verdict:** **Adopt.** Best ROI for trading monorepo at this maturity.

### Option C — Plugin host (entry points / setuptools plugins)

Dynamic plugins for backends via entry points.

| Pros | Cons |
|------|------|
| True third-party extension | Overkill; weak typing |
| | Harder safety review for risk code |

**Verdict:** Defer. May use later for research-only model backends; not for risk/execution.

---

## 3. Target Architecture

### 3.1 Style

**Hexagonal (Ports & Adapters) inside a Modular Monolith**, layered as:

```
┌──────────────────────────────────────────────────────────────────┐
│  Application shell (composition root / DI wiring / CLI / cron)   │
│  stockapp_v12.app  ·  scripts/* thin wrappers only               │
└────────────────────────────┬─────────────────────────────────────┘
                             │ depends on ports only
┌────────────────────────────▼─────────────────────────────────────┐
│  Application services (use-cases)                                 │
│  entry · scan · review · shadow_eval                             │
└───────────────┬─────────────────────────────┬────────────────────┘
                │                             │
┌───────────────▼─────────────┐   ┌───────────▼────────────────────┐
│  Domain (pure-ish)          │   │  Ports (Protocols + DTOs)      │
│  rules · sizing · states    │   │  MarketData · Chronos · Broker │
│  signal · risk limits       │   │  Notifier · PositionStore …    │
└─────────────────────────────┘   └───────────┬────────────────────┘
                                              │ implemented by
                                  ┌───────────▼────────────────────┐
                                  │  Adapters (I/O + legacy)       │
                                  │  redis · longbridge · chronos  │
                                  │  paper/ibkr · webhook · archive│
                                  └────────────────────────────────┘
```

**Dependency rule (enforced):**

```
adapters → app → application → domain + ports
                ↘              ↗
                 (never reverse)
```

- `domain` and `ports` import **nothing** from `adapters` / `app` / Redis / HTTP.
- `application` depends only on `domain` + `ports`.
- `adapters` implement ports; may import vendor SDKs and (temporarily) archive.
- `app` is the **only** composition root that wires concrete adapters.

### 3.2 Target package map (logical → physical)

Keep top-level name `stockapp_v12` for continuity; reorganize internals:

```
src/stockapp_v12/
├── domain/                 # pure rules + value objects
│   ├── risk/               # limits, sizing, red lines (today core/limits+sizing)
│   ├── signal/             # SR-01..SR-07 pure evaluation
│   ├── forecast/           # ForecastOutput schema (today schemas/forecast)
│   ├── position/           # state machine transitions (no Redis)
│   └── calendar/           # nyse_calendar
│
├── ports/                  # Protocols only (today contracts/ + expansions)
│   ├── market_data.py
│   ├── chronos_backend.py
│   ├── broker.py
│   ├── notifier.py
│   ├── position_store.py
│   ├── journal_store.py
│   ├── watchlist_store.py
│   └── clock.py            # injectable time for tests
│
├── application/            # use-cases / orchestration
│   ├── entry/              # EntryOrchestrator + stages (sequencing only)
│   ├── scan/               # SwingScanner orchestration (no fetch impl)
│   ├── review/             # DailyReview
│   └── eval/               # shadow/H2H use of domain metrics
│
├── adapters/               # ALL I/O and legacy
│   ├── marketdata/
│   ├── forecast/           # EnsembleChronos, ChronosServiceAdapter
│   ├── broker/
│   ├── notify/
│   ├── redis_store/        # PositionStoreRedis, JournalStoreRedis, …
│   └── legacy_v11/         # temporary Strangler cage — delete at end
│
├── app/                    # composition root
│   ├── wiring.py           # build_container(env)
│   └── cli/                # optional entrypoints
│
├── pipeline/               # TRANSITIONAL — folds into application/entry
└── services/               # TRANSITIONAL — splits into domain/application/adapters
```

> **Migration note:** Prefer **move + re-export shims** for one release window (existing project pattern). Do not break import paths until consumers migrate.

### 3.3 Ports catalog (canonical seams)

| Port | Methods (minimal) | Primary consumers | Default adapter |
|------|-------------------|-------------------|-----------------|
| `ChronosBackend` | `detect`, `detect_batch` | ForecastService | Ensemble / ChronosAdapter |
| `MarketDataClient` | `fetch_daily_bars`, `fetch_latest_close`, `universe` | Scan, features | Longbridge/Polygon adapters |
| `BrokerAdapter` | `submit_bracket_order`, `get_order_status`, `cancel_order` | BracketOrderService | PaperBroker → IBKR later |
| `Notifier` | existing 8-method surface | stages, PM, review | Log / Redis / Webhook |
| `PositionStore` | `get`, `save`, `list_open`, `atomic_close` | PositionManager | Redis hash/pipeline |
| `JournalStore` | `append`, `range` | TradeJournal | Redis RPUSH / NDJSON |
| `WatchlistStore` | `set_tier`, `get_tier` | Scanner, entry caller | Redis keys |
| `Clock` | `now_et`, `today_et` | guards, windows | SystemClock |

**Rule:** Production code may depend on **port types**, never on adapter classes (except `app/wiring.py`).

### 3.4 Domain purity rules

Domain modules **may**:

- Use stdlib, pydantic/dataclasses, pandas frames as **data**, pure functions

Domain modules **must not**:

- Import `redis`, `requests`, broker SDKs, `sys.path`, archive
- Open files under `models/` or network
- Read env vars for behavior (config injected from application)

Position state transitions become pure functions:

```text
transition(state, event, limits, now) → (new_state, effects[])
```

Effects (`persist`, `notify`, `submit_exit`) executed by application layer via ports.

### 3.5 Application layer rules

- `EntryOrchestrator` remains the **sequencer only** (already the intent of `pipeline/stages.py`).
- Stages receive **DTO + injected ports**, not concrete services.
- Types like `SignalRuleResult`, `YoloFilterDecision` live in `domain` or `ports` DTOs — **never** force `pipeline` to import `services`.

### 3.6 Composition root

Single factory:

```python
# stockapp_v12.app.wiring
def build_runtime(settings: Settings) -> Runtime:
    stores = RedisStores(settings.redis_url)
    md = build_market_data(settings)
    backend = build_chronos(settings)
    broker = build_broker(settings)
    notifier = make_notifier_from_env()
    return Runtime(
        entry=EntryOrchestrator(...),
        scanner=SwingScanner(market_data=md, watchlist=stores.watchlist),
        ...
    )
```

Cron scripts become:

```bash
python -m stockapp_v12.app.cli shadow-run --profile observation
```

instead of ad-hoc `sys.path.insert`.

---

## 4. Cross-cutting architecture

### 4.1 Configuration

- Keep YAML as source of truth for risk (`config/risk_regimes.yaml` pattern).
- One `Settings` object loaded at composition root (env + YAML).
- No deep modules call `os.getenv` for behavioral switches (adapters may for credentials only).

### 4.2 Observability

- Structured logs with `component=`, `symbol=`, `reason_code=`
- Audit remains append-only (INV-7)
- Metrics for adapter health (data freshness, backend load fail, broker disconnect) — feed fail-closed policy

### 4.3 Testing strategy (industry standard pyramid)

| Layer | What | Tooling |
|-------|------|---------|
| Domain unit | pure transitions, SR rules, sizing | pytest, no Redis |
| Port contract tests | adapter satisfies Protocol + soft-fail rules | shared test suite per port |
| Application | orchestrator with fakes | in-memory stores |
| Adapter integration | Redis testcontainer / fakeredis optional | marked `integration` |
| Soak / shadow | ADR-008, determinism baseline | existing scripts, unchanged hashes |

**Golden rule:** Every port has a **Fake\*Adapter** used by application tests. Production adapters never required for unit tests.

### 4.4 Import boundary enforcement

Add a lightweight gate (choose one):

1. **import-linter** contracts in CI (recommended), or  
2. AST script in `scripts/check_import_boundaries.py` already pattern-friendly

Example contracts:

- `domain` cannot import `adapters`, `app`, `services`, `pipeline`
- `ports` cannot import anything except typing/stdlib/pandas types
- `application` cannot import `adapters`
- Only `app` and `adapters` may import `legacy_v11`

Fail CI on violation.

### 4.5 Versioning & compatibility

- Ports are **semver-stable** within v12.x (positional args frozen — already documented for ChronosBackend).
- Deprecated modules keep re-export shims **one minor release**, emit `DeprecationWarning` (pattern already used for `notification_service`).
- Archive cage: `adapters/legacy_v11` only; public code never calls `ensure_v11_archive_on_path` directly.

---

## 5. Strangler Fig — V11 archive elimination

### 5.1 Pattern

```
Before:  ForecastService → sys.path → archive ChronosService
After:   ForecastService → ChronosBackend port
           ↑ wired by app
         ChronosServiceAdapter (in adapters/legacy_v11) wraps archive
           ↓ later
         NativeChronosAdapter (in-tree) replaces wrap; archive deleted
```

Same pattern for:

| Legacy import | Port | Strangler adapter | Final owner |
|---------------|------|-------------------|-------------|
| `src.services.chronos_service` | `ChronosBackend` | `LegacyChronosAdapter` | `adapters/forecast/` |
| `src.services.longbridge_data_service` | `MarketDataClient` | `LegacyLongbridgeAdapter` | `adapters/marketdata/` |
| `src.core.risk_controller` | `RiskGatePort` or gatekeeper_fn | `LegacyHardRiskAdapter` | `domain/risk` + `bridge` fold-in |

### 5.2 Exit criteria for deleting archive from prod PYTHONPATH

1. Zero production call sites to `ensure_v11_archive_on_path` outside `adapters/legacy_v11`
2. Integration tests pass with `STOCKAPP_DISABLE_V11_ARCHIVE=1`
3. NUC shadow + paper path run 5 consecutive trading days green
4. Docs / runbooks updated; `PYTHONPATH` no longer includes archive

---

## 6. Phased migration plan

Each phase is **shippable**, **soak-safe**, and has a **done gate**. No phase changes trading semantics unless explicitly marked.

### Phase 0 — Architecture freeze & measurement (3–5 days)

**Work**

- Accept this design (ADR-011 pointer)
- Import graph baseline (module fan-in/out report checked into `docs/architecture/07-IMPORT-BASELINE.md`)
- Inventory every `ensure_v11` / `from src.` / `sys.path.insert` call site (checklist)
- Enable import-linter in **warn** mode

**Done gate**

- [ ] Baseline artifact committed
- [ ] Call-site inventory complete with owner package tags
- [ ] CI reports boundary violations without failing (baseline known)

### Phase 1 — Ports complete + composition root (1–2 weeks)

**Work**

- Expand `ports/` (`PositionStore`, `JournalStore`, `WatchlistStore`, `Clock`, promote `BrokerAdapter`)
- Introduce `app/wiring.py` used by **one** script first (e.g. paper backtest or shadow)
- Wire `MarketDataClient` into SwingScanner via constructor (default adapter = current behavior)
- Move notifier usage off deferred imports inside stages (inject Notifier)

**Done gate**

- [ ] Scanner unit tests run with `FakeMarketData`
- [ ] ForecastService never imports archive when `backend=` injected
- [ ] Existing suite green; ADR-008 determinism baseline unchanged
- [ ] No change to decision ensemble membership

### Phase 2 — Split fat `services/` by move+shim (2–3 weeks)

**Work**

Logical packages (physical moves with re-exports):

| From | To |
|------|-----|
| signal_rule_engine, sizing-related pure | `domain/signal`, `domain/risk` |
| forecast_service, ensemble_* | `application` + `adapters/forecast` |
| yolo_filter | `adapters/forecast` or `adapters/vision` |
| position_manager, daily_review | `application` + store ports |
| swing_scanner, longbridge_* | `application/scan` + `adapters/marketdata` |
| entry_orchestrator, stages | `application/entry` |

**Done gate**

- [ ] Old import paths still work via shim
- [ ] `pipeline` no longer imports `services` at runtime (only domain DTOs)
- [ ] Coverage ≥ prior baseline; critical path tests 100% pass

### Phase 3 — Redis behind store ports (2 weeks)

**Work**

- Implement `RedisPositionStore`, `RedisJournalStore`, `RedisWatchlistStore`
- PositionManager / TradeJournal / EntryGuard talk only to ports
- Add `InMemory*` stores for unit tests
- Document key registry once in `docs/REDIS_KEY_REGISTRY.md` (or extend POSITION_LIFECYCLE)

**Done gate**

- [ ] Domain/application tests pass with zero Redis
- [ ] Integration tests with Redis green
- [ ] Key names unchanged (byte-compatible state) unless dual-write migration specified

### Phase 4 — Kill legacy cage (2–4 weeks, calendar-bound)

**Work**

- Native adapters replace `legacy_v11` wrappers one at a time
- `STOCKAPP_DISABLE_V11_ARCHIVE=1` in CI
- NUC cron PYTHONPATH drop archive
- Delete or quarantine archive from runtime docs

**Done gate**

- [ ] 5 trading days NUC green without archive on path
- [ ] Grep clean: no `from src.` in non-adapter code
- [ ] ADR-008 / observation pipelines healthy

### Phase 5 — Ops surface hardening (1–2 weeks)

**Work**

- Thin CLI modules; scripts become 10–30 line wrappers
- Remove `sys.path.insert` from first-party scripts
- Optional: package install `pip install -e .` with console_scripts
- Scheduler job definitions reference module entrypoints only

**Done gate**

- [ ] New engineer runs `pip install -e . && pytest` without path folklore
- [ ] Cron unit files document single entrypoint per job

### Phase 6 — Optional future (explicitly later)

- Extract **research-only** model backends as installable plugins
- Consider process split only if CPU/GPU isolation demands it
- Never split risk gate into a remote service without a formal ADR

---

## 7. Phase priority vs business value

| Phase | Decoupling value | Trading risk | Recommend order |
|-------|------------------|--------------|-----------------|
| 0 | Enables measurement | None | First |
| 1 | Highest leverage | Low | Second |
| 2 | Maintainability | Medium | Third |
| 3 | Testability / storage swap | Medium | Fourth |
| 4 | Legacy freedom | Medium-High | Fifth (after soak) |
| 5 | Ops ergonomics | Low | Parallel after 1 |

**Do not start Phase 4 before Phase 1 ports are real.** Strangler without ports recreates spaghetti in a new folder.

---

## 8. Acceptance criteria (program-level “完胜”)

Architecture upgrade is **complete** when all of the following hold:

1. **Replaceability**
   - Swap Chronos backend, market data source, broker, notifier, and stores via wiring only.
2. **No prod archive**
   - Production runtime PYTHONPATH has no `archive/`; CI enforces.
3. **Directionality**
   - import-linter contracts green; zero `application → adapters` imports.
4. **Test isolation**
   - ≥80% of unit tests run without Redis/network; domain 100% pure.
5. **Behavioral continuity**
   - Shadow determinism baseline and paper risk invariants unchanged except intentional ADRs.
6. **Docs SSOT**
   - `MODULE_MAP` + `SYSTEM_OVERVIEW` updated to new map; old architecture docs marked historical.
7. **Human gate**
   - Path-change of architecture itself treated as L2-class change: review + explicit accept (aligns with Gate Decision Architecture culture).

---

## 9. Risk register

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Silent metric drift during moves | M | Determinism baseline + ADR-008 each phase |
| Shim hell forever | M | Calendar: remove shims ≤1 minor after move |
| Over-abstract ports | M | YAGNI — only ports in §3.3; no generic EventBus |
| Team skips import lint | H | CI fail after Phase 1 baseline |
| NUC path still points archive | H | Deploy checklist + health preflight flag |
| Big-bang temptation | M | Phase gates; reject PRs that span >1 phase without ADR |

---

## 10. ADR recommendation

Create **ADR-011: Hexagonal Modular Monolith + Strangler for V11 Archive** with:

- Decision: Option B
- Consequences: ports mandatory for I/O; archive confined; microservices rejected
- Links: this design, MODULE_MAP update task, import-linter

(Body of ADR can be thin; this design is the detail SSOT.)

---

## 11. What “完胜” is **not**

- Renaming folders without ports
- Adding more Protocols that nothing injects (`MarketDataClient` today is the anti-pattern to avoid repeating)
- Microservices “for cleanliness”
- Freezing features for months — phases interleave with ADR-010 observation work

---

## 12. Immediate next steps after approval

1. User approves this design (possibly with amendments).
2. Write **implementation plan** (`docs/superpowers/plans/2026-07-26-architecture-decoupling-upgrade.md`) for Phase 0–1 only (smallest shippable).
3. Open ADR-011 stub in `docs/`.
4. Execute Phase 0 (measurement) → Phase 1 (ports + wiring) under normal TDD + verification gates.

**This document intentionally does not modify production code.**

---

## 13. Spec self-review checklist

- [x] No TBD placeholders for core decisions
- [x] Options A/B/C evaluated; B selected with rationale
- [x] Phases have done gates
- [x] Non-goals and hard constraints explicit
- [x] Aligns with existing INV/fail-closed/ADR-010
- [x] Scope is program-level design, not a single PR dump
- [x] Ambiguity resolved: modular monolith not microservices; strangler not big-bang

---

## Appendix A — Industry references (principles, not cargo-cult)

- Hexagonal Architecture (Cockburn) — ports at edges  
- Modular Monolith (Newman / Spotify-style) — modules before services  
- Strangler Fig (Fowler) — incremental legacy replacement  
- Dependency Rule (Clean Architecture) — inward-only deps  
- Composition Root (DI) — one place wires concretes  
- Contract tests for adapters  

## Appendix B — Mapping from current tree (cheat sheet)

| Today | Target |
|-------|--------|
| `contracts/*` | `ports/*` |
| `schemas/forecast` | `domain/forecast` |
| `core/limits,sizing,precision` | `domain/risk` |
| `core/execution` | `domain` rules + `ports.broker` + `adapters/broker` |
| `bridge/*` | `application` risk use-cases + thin adapters |
| `pipeline/stages,types` | `application/entry` |
| `services/*` | split domain / application / adapters |
| `notifiers/*` | `adapters/notify` |
| `eval/*` | `application/eval` (+ domain metrics) |
| `core/_v11_compat` | `adapters/legacy_v11` only |
| `scripts/*` fat | thin → `app/cli` |
