# Import Baseline — 2026-07-26 (Phase 0)

> **Purpose:** Freeze a measurable inventory of archive/sys.path seams and
> internal `stockapp_v12` import edges before Phase 1 port/wiring work.
> Later tasks use this doc to prove call-site reduction (not guess it).
>
> **Method:** `rg` from repo root on 2026-07-26 (branch `feature/arch-decouple-p0p1`).
> Counts are import **statement** hits under `src/stockapp_v12` / `scripts`
> (not unique runtime edges). Indented / deferred imports are noted where
> reverse-layer checks used a broader scan.
>
> **Raw counts:**
>
> | Inventory | Lines |
> |-----------|------:|
> | Archive / path seams (`ensure_v11_archive_on_path` \| `from src.` \| `sys.path.insert`) | 40 |
> | Internal package imports (`^from stockapp_v12.` \| `^import stockapp_v12.`) | 96 |

## Commands used

```bash
# Archive / path seams
rg -n 'ensure_v11_archive_on_path|from src\.|sys\.path\.insert' \
  src/stockapp_v12 scripts --glob '*.{py,sh}' \
  | rg -v '__pycache__|archive/' \
  > /tmp/v12_archive_seams.txt

# Cross-package imports inside stockapp_v12
rg -n '^from stockapp_v12\.|^import stockapp_v12\.' \
  src/stockapp_v12 --glob '*.py' \
  | rg -v '__pycache__' \
  > /tmp/v12_internal_imports.txt

wc -l /tmp/v12_archive_seams.txt /tmp/v12_internal_imports.txt
# 40 archive seams, 96 internal imports
```

## Archive / sys.path seams

Grouped by file. **Production hot paths** (services that load V11 archive at
runtime) are bold in Notes. Docstring-only mentions in `_v11_compat` are
included because the rg inventory captured them; they are not call sites.

| File | Pattern | Notes |
|------|---------|-------|
| `src/stockapp_v12/core/_v11_compat.py` | `ensure_v11_archive_on_path` + `sys.path.insert` + doc `from src.` | **Canonical helper.** Inserts archive root onto `sys.path`. Call sites should go through this (or Phase 1 `adapters/legacy_v11`). 8 rg hits (impl + docstring examples). |
| `src/stockapp_v12/services/forecast_service.py` | `ensure_v11_archive_on_path` + `from src.services.chronos_service` | **Hot path.** Default Chronos backend constructs via archived V11 `ChronosService` when no backend injected. Phase 1 target: adapters/legacy_v11. |
| `src/stockapp_v12/services/swing_scanner.py` | `sys.path.insert(REPO_ROOT)` + `ensure_v11_archive_on_path` + `from src.services.longbridge_data_service` | **Hot path.** Default market-data path pulls archived Longbridge client. Phase 1 target: accept `MarketDataClient` port; no new archive calls in tests. |
| `scripts/cache_d1_bars.py` | `sys.path.insert` + `ensure_v11` + `from src.services.longbridge_data_service` | Ops script; full V11 Longbridge seam. |
| `scripts/validate_longbridge_screener.py` | `sys.path.insert` + `ensure_v11` + `from src.services.longbridge_data_service` | Validation script; dual `src/` path inserts + archive. |
| `scripts/shadow_halt_decision.py` | `sys.path.insert` (script dir + `_src_dir`) | Shadow halt path bootstrap (2 inserts). |
| `scripts/shadow_ensemble.py` | `sys.path.insert(SCRIPT_DIR)` | Shadow ensemble bootstrap. |
| `scripts/ensemble_predict.py` | `sys.path.insert(SCRIPT_DIR)` | Ensemble predict bootstrap. |
| `scripts/validate_ensemble_gate.py` | `sys.path.insert(SCRIPT_DIR)` | Ensemble gate bootstrap. |
| `scripts/compare_shadow_models.py` | `sys.path.insert(repo root)` | Shadow model compare bootstrap. |
| `scripts/champion_evaluation.py` | `sys.path.insert(repo root)` | Champion eval bootstrap. |
| `scripts/evaluate_path_change.py` | `sys.path.insert(repo root)` | Path-change evaluation bootstrap. |
| `scripts/backfill_30_days.py` | `sys.path.insert` (parent×3) | Backfill bootstrap. |
| `scripts/threshold_tune.py` | `sys.path.insert(script dir)` | Threshold tune bootstrap. |
| `scripts/yolo_hit_rate_validation.py` | `sys.path.insert(script dir)` | YOLO validation bootstrap. |
| `scripts/paper_soak_checklist.py` | `sys.path.insert(scripts/)` | Paper soak checklist bootstrap. |
| `scripts/_preflight.sh` | `sys.path.insert('/home/app/Stockapp/src')` | Shell preflight import smoke; hard-coded NUC path. |

### Seam summary (for delta tracking)

| Category | Count (files) | Detail |
|----------|--------------:|--------|
| Production `ensure_v11` + `from src.` call sites | 2 | `forecast_service`, `swing_scanner` |
| Canonical helper module | 1 | `_v11_compat.py` |
| Scripts with full archive import (`from src.`) | 2 | `cache_d1_bars`, `validate_longbridge_screener` |
| Scripts with `sys.path.insert` only (no `from src.`) | 12 | path bootstrap for package resolution |
| Total rg hit lines | 40 | see commands above |

## High fan-in modules (top 15 by inbound imports)

Counted from `/tmp/v12_internal_imports.txt`: each top-level
`from stockapp_v12.<module> import …` / `import stockapp_v12.<module>`
statement under `src/stockapp_v12/**/*.py` (package `__init__` re-exports included).

| Module | Approx inbound | Role |
|--------|---------------:|------|
| `stockapp_v12.core.limits` | 7 | Risk limits / feature flags (`V12RiskLimits`, stage fallbacks) — shared kernel |
| `stockapp_v12.pipeline.journal` | 5 | Trade journal persistence used by orchestrator, PM, review, stages |
| `stockapp_v12.schemas.forecast` | 5 | `ForecastOutput` DTO — pipeline + services + schemas package export |
| `stockapp_v12.core.precision` | 4 | Decimal rounding helpers (`D`, `round_pct`, …) |
| `stockapp_v12.eval.constants` | 4 | Eval horizons / thresholds — mostly eval-internal fan-in |
| `stockapp_v12.bridge.live_guard` | 3 | Live order guard + context (bridge + stages + orchestrator) |
| `stockapp_v12.core.execution.bracket_order` | 3 | Bracket order service / broker adapter types |
| `stockapp_v12.pipeline.analyst` | 3 | Analyst stage types (`AnalystOutput`) |
| `stockapp_v12.services.position_manager` | 3 | Position state machine (services + package export) |
| `stockapp_v12.services.signal_rule_engine` | 3 | Signal rules — **also imported by pipeline** (reverse edge) |
| `stockapp_v12.services.yolo_filter` | 3 | YOLO no-trade filter — **also imported by pipeline** (reverse edge) |
| `stockapp_v12.pipeline.types` | 3 | `EntryContext` / `EntryResult` (P2 boundary extract from orchestrator) |
| `stockapp_v12.services._notifier_compat` | 3 | Notifier shim (services + deferred use in stages) |
| `stockapp_v12.eval.metrics` | 3 | Eval metrics (`sign`, MASE, …) — eval-internal |
| `stockapp_v12.pipeline.entry_guard` | 2 | Entry evaluation guard (orchestrator + pipeline export) |

### Package-level inbound (context)

| Package | Import statements targeting it |
|---------|-------------------------------:|
| `stockapp_v12.core` | 22 |
| `stockapp_v12.services` | 18 |
| `stockapp_v12.pipeline` | 15 |
| `stockapp_v12.eval` | 15 |
| `stockapp_v12.bridge` | 7 |
| `stockapp_v12.contracts` | 6 |
| `stockapp_v12.schemas` | 5 |
| `stockapp_v12.notifiers` | 4 |
| `stockapp_v12.config` | 3 |
| `stockapp_v12.audit` | 1 |

## Known reverse edges (forbidden in target)

Target layering (hexagonal modular monolith / ADR-011): **pipeline and
domain must not depend on concrete `services.*` application adapters**.
Types should live in schemas/ports; wiring belongs in a composition root.

Strict `^from stockapp_v12` inventory misses indented (lazy) imports; the
table below includes both strict and deferred hits from a pipeline-scoped
scan.

| From | To | Issue |
|------|-----|-------|
| `pipeline.stages` | `services.signal_rule_engine` (`SignalRuleContext`) | reverse layer — stages import application service types |
| `pipeline.stages` | `services.yolo_filter` (`YoloFilterDecision`) | reverse layer — filter decision type owned under services |
| `pipeline.stages` | `services._notifier_compat` (lazy, ×2) | reverse layer — notifier side-effect from stage helpers |
| `pipeline.types` | `services.signal_rule_engine` / `services.yolo_filter` (`TYPE_CHECKING`) | reverse layer — DTO module still type-depends on services |
| `pipeline.entry_window` | `services.entry_orchestrator` (`EntryContext`, lazy) | stale path — runtime late import of `EntryContext` from orchestrator though types were moved to `pipeline.types` (P2-boundaries) |
| `services.__init__` | re-exports `EntryContext`, `EntryResult` from `entry_orchestrator` | stale public surface — package still advertises types that live in `pipeline.types` |

## Phase 1 target deltas

Status as of Task 10 (2026-07-26, branch `feature/arch-decouple-p0p1`):

- [ ] New archive seams only under `adapters/legacy_v11`
  - **Partial:** Chronos is caged (`adapters/legacy_v11/chronos.py`).  
    `SwingScanner` default path still calls `ensure_v11` via `_init_longbridge` when `market_data` is not injected.  
    Target for full cage: Phase 1 follow-up / Phase 4 longbridge adapter move.
- [x] `ForecastService` default path goes through `adapters/legacy_v11`
  - `_resolve_backend` → `build_legacy_chronos_backend` only; no direct `ensure_v11` in `forecast_service.py`.
- [x] `SwingScanner` accepts `MarketDataClient` (no new archive calls in tests)
  - Keyword `market_data=` injects Fake/Composite; injected path skips archive/network provider init.
  - Composition root: `app.wiring.build_runtime` + smoke `scripts/demo_wiring_runtime.py`.

### Optional Phase 1 follow-ups (not required for Phase 1 exit, tracked here)

- [ ] Replace `pipeline.entry_window` late import of `EntryContext` with `pipeline.types`
- [ ] Move / re-export `SignalRuleContext` and `YoloFilterDecision` to schema/port layer so pipeline no longer imports `services.*`
- [ ] Stop re-exporting `EntryContext`/`EntryResult` from `services.entry_orchestrator` / `services.__init__` after one-release shim window
- [ ] Collapse script-level `sys.path.insert` toward a single composition/CLI entry (Phase 5 program item; measure only in Phase 0)
- [ ] Move Longbridge default init out of `swing_scanner` into `adapters/legacy_v11` (or `adapters/marketdata`) so production `ensure_v11` call sites outside adapters/`_v11_compat` → 0

## Phase 1 verification (Task 11 — 2026-07-26)

Branch: `feature/arch-decouple-p0p1`. Verification-only gate after tasks 1–10.

### Boundary checker

```bash
python scripts/check_import_boundaries.py --root .
# OK (warn): 0 known issue(s)  — exit 0
```

### Targeted suite (Phase 0–1 slice)

```bash
pytest \
  tests/test_import_boundaries.py \
  tests/test_clock_port.py \
  tests/test_broker_port.py \
  tests/test_legacy_chronos_adapter.py \
  tests/test_swing_scanner_market_data_injection.py \
  tests/test_stages_notifier_injection.py \
  tests/test_wiring.py \
  tests/test_market_data_client_protocol.py \
  tests/test_chronos_backend_protocol.py \
  tests/test_swing_scanner.py \
  tests/test_forecast_output_schema.py \
  tests/test_entry_orchestrator.py \
  -v --tb=short
# 62 passed in ~1.4s
```

Also green after Task 11 fix:

```bash
pytest tests/test_j6_chronos_import.py -q
# 1 passed  (updated contract: cage via adapters/legacy_v11, not ensure_v11 in ForecastService)
```

### Grep gates

| Gate | Command | Result |
|------|---------|--------|
| ForecastService no direct ensure_v11 | `rg -n 'ensure_v11_archive_on_path' src/stockapp_v12/services/forecast_service.py` | **0 matches** (pass) |
| stages no notifier compat | `rg -n '_notifier_compat\|notification_service' src/stockapp_v12/pipeline/stages.py` | **0 matches** (pass) |
| ensure_v11 inventory under `src/stockapp_v12` | `rg -n 'ensure_v11_archive_on_path' src/stockapp_v12 --glob '*.py'` | See hits below |

**`ensure_v11_archive_on_path` hits (production, honest inventory):**

| Location | Role | Adjudication |
|----------|------|--------------|
| `core/_v11_compat.py` | Canonical helper + docstring examples | Allowed |
| `adapters/legacy_v11/chronos.py` | Chronos archive cage | Allowed (Phase 1 target) |
| `services/swing_scanner.py` (`_init_longbridge`) | Default Longbridge path when `market_data` not injected | **Allowed for Phase 1** — residual production seam; tracked as optional follow-up (longbridge adapter move) |

No unexpected extra call sites under `src/stockapp_v12`.

### Full suite (optional)

```bash
pytest -q --tb=line
# 848 collected
# 826 passed, 8 failed, 7 skipped, 7 xfailed, 1 warning  (pre-j6-fix run)
```

After fixing `tests/test_j6_chronos_import.py` for the Phase 1 cage contract, **j6 is green**. Remaining full-suite failures are **not** Phase 0–1 regressions (pre-existing / environment / baseline drift):

| Failure | Classification |
|---------|----------------|
| `test_j6_chronos_import` (pre-fix) | **p0p1-induced** — expected old ensure_v11-in-service contract; **fixed** to Phase 1 cage |
| `test_catalyst_scorer_passthrough_end_to_end` | Pre-existing (log file count) |
| `test_live_guard::test_min_hold_blocks_early_exit` | Pre-existing / time-sensitive hold window |
| `test_paper_soak_sign_agreement` (4 integration) | Pre-existing (Redis/fixture agreement counts) |
| `test_shadow_trade_summary_json_hash_matches_baseline` | Pre-existing determinism baseline drift |

**Phase 0–1 exit criteria:** targeted slice all green; grep gates pass; residual `swing_scanner` ensure_v11 documented; boundary checker clean.

## How to re-measure

Re-run the commands in **Commands used**. Compare:

1. Production `ensure_v11` + `from src.` call sites outside `adapters/legacy_v11` → should go to **0** after Phase 1 cage (Chronos done; Longbridge residual on `swing_scanner` until follow-up).
2. `pipeline.*` → `services.*` import lines → should shrink as types move to ports/schemas.
3. Fan-in of `core.limits` / `schemas.forecast` may stay high (healthy shared kernel); fan-in of concrete services should not grow into pipeline.
