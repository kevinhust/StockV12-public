# Changelog

All notable changes to StockApp V12 are recorded here. Dates are project-local (KST/EST as appropriate).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) (the V12
suffix denotes the major version).

Sprint-by-sprint records of the 2026-07-09 council-review remediation live in
[`docs/reviews/council-review-2026-07-09/`](docs/reviews/council-review-2026-07-09/).

---

## [Unreleased] — 2026-07-09 → 2026-07-10

### Changed (council review Sprints F–K, 2026-07-09 → 2026-07-10)

#### Structural — new canonical homes

- **`stockapp_v12.pipeline.types` (Sprint J5)** — `EntryContext` and
  `EntryResult` moved out of `services/entry_orchestrator.py` into a new leaf
  module `src/stockapp_v12/pipeline/types.py`. The leaf-module position
  breaks the services↔pipeline circular import that previously required a
  deferred local import inside `entry_orchestrator.py`.
- **`stockapp_v12.services._notifier_compat` (Sprint J7)** — new canonical
  shim for the notifier singleton. Old `services/notification_service.py`
  remains as a one-line re-export for backward compatibility.
- **`stockapp_v12.core.limits.StageFallbacks` (Sprint J2)** — pipeline-level
  fallback values (`analyst_confidence`, `atr_pct`) promoted from local
  constants in `pipeline/stages.py` to a frozen dataclass in `core/limits.py`
  so operators can tune them via config and unit-test them in isolation.
- **`stockapp_v12.core.nyse_calendar` (Sprint I, new file)** — NYSE
  holiday / half-day guard. Used by `scripts/nyse_guard.sh` to prevent
  cron jobs from firing on market closures. Falls back to a built-in
  holiday table (2024–2028) when `exchange_calendars` is not installed.
- **`stockapp_v12.core._v11_compat` (Sprint J6, new file)** — explicit
  bridge layer that injects the V11 archive path onto `sys.path` for the
  three V11 modules V12 still depends on (`HardRiskGatekeeper`,
  `lifecycle_scheduler`, `LongbridgeDataService`). Replaces scattered
  `sys.path.insert` calls with a single idempotent helper
  `ensure_v11_archive_on_path()`.

#### Documentation

- **`docs/DEPLOYMENT.md` (new, 2026-07-10)** — canonical deployment
  topology. Closes a gap that was caught by the Round-2 council: the
  production deploy target is the **NUC (Linux WSL on Windows)** at
  `/home/app/Stockapp/`; the Mac mini (`/Users/dev/Stockapp/`)
  is the dev/test surface only. The hardcoded `/home/app/Stockapp/...`
  paths in the runner scripts are correct for the NUC target. WSL2
  specifics documented (Redis on port 6380 because 6379 has a stuck
  reservation; `app` user, not `trainer`).
- **`docs/SYSTEM_OVERVIEW.md:46`** — corrected "single-host scheduler on
  Mac Mini" to point to NUC (Linux WSL) with a link to `DEPLOYMENT.md`.
- **`docs/ENVIRONMENT.md`** — added header note clarifying this is the
  training-env inventory only; live deploy is on the NUC, documented in
  `DEPLOYMENT.md`.
- **`docs/reviews/council-review-2026-07-09/Council-Round-2.md`** —
  added a post-review correction note invalidating the Round-2 L1
  finding (the "Linux paths on Mac" issue) once the deployment topology
  was clarified. Revised Sprint L priority to remove L1; L3 (shadow-gate
  fixes) is now the highest-leverage action.

#### Bridge — new rule checks

- **`bridge/live_guard.py` R-03_SINGLE_CAP and R-04_EXPOSURE_CAP (Sprint I)**
  — semantic checks for single-position cap and portfolio exposure cap.
  Promote what was previously a YAML-only check to a runtime guard with
  explicit rule codes. Other R-codes (R-01, R-02, R-05, R-06) unchanged.

#### Persistence

- **`audit/audit_ledger.py` fsync (Sprint H2)** — every audit write is now
  followed by `fh.flush() + os.fsync(fh.fileno())` so the docstring's
  atomicity claim is actually honored.
- **`services/position_manager.py` atomic close (Sprint J1)** — `_close_position`
  now commits `srem` (index) + `setex` (cooldown) + `set/expire` (record)
  as a single Redis pipeline transaction. Pre-fix, a crash between any two
  writes could leave the index and record out of sync.

#### Numeric discipline

- **`services/daily_review.py` and `services/position_manager.py` Decimal
  precision (Sprint H3)** — float `round()` replaced with
  `core.precision.D`, `round_pct`, `round_dollar`, `round_shares` for all
  PnL / size / share calculations. Eliminates IEEE-754 representational
  drift that compounds over multi-day positions.

#### Tracking

- **`services/position_manager.py` MFE/MAE tracking (Sprint I)** — added
  `lowest_close_since_entry` field to `PositionRecord` (mirroring the
  existing `highest_close_since_entry`). `update_trail_on_tick` now
  updates both. `_close_position` computes and journals
  `pnl_pct` / `mfe_pct` / `mae_pct` / `regime_at_entry` on every close.

#### Defaults

- **`services/daily_review.py` `apply_immediately` default → `False`
  (Sprint J3)** — the default is now opt-in. Previously `True` could
  trigger a TRIM/EXIT the moment a DailyReview ran, even when the
  scheduler was supposed to queue it for the next cycle.
- **`services/daily_review.py` `regime` default for tests
  (Sprint J2 / K2 cleanup)** — `regime="BULL_LONG"` is now a test-side
  default applied by the shared `register_test_position` shim in
  `tests/conftest.py`, not a production-side default.

#### Pipeline integrity

- **`pipeline/stages.py` bars sort assertion (Sprint H1)** — `assert
  bars.index.is_monotonic_increasing` at the entry of `extract_price_atr`
  so the `iloc[-1]` "today's close" assumption is enforced.
- **`pipeline/stages.py` catalyst aliasing comment (Sprint J4)** —
  explicit comment in `run_stage_4_signal_rules` documenting that
  `forecast.catalyst` is read for legacy compatibility and the canonical
  catalyst signal now lives on `analyst.catalysts`.

#### Tests

- **621 tests passing** (547 baseline → 562 F → 562 G → 594 H → 605 I →
  615 J → 616 K). 0 tests deleted; 0 tests weakened.
- **`tests/conftest.register_test_position` (Sprint K2)** — shared
  "register a position with sensible defaults" helper extracted from three
  test files that had near-duplicate `_register` shims.
- **`tests/test_j7_notifier_shim.py::test_legacy_path_emits_deprecation_warning`
  (Sprint K3)** — locks the deprecation contract for the legacy
  `services/notification_service` path.

### Deprecated

- **`stockapp_v12.services.notification_service` (Sprint K3, 2026-07-09)**
  — this module is now formally deprecated. Importing it emits a
  `DeprecationWarning` pointing to:
  - `stockapp_v12.services._notifier_compat` (singleton shim — same
    default behavior as the old path)
  - `stockapp_v12.notifiers` (explicit backend selection: log / redis
    pubsub / webhook)

  The legacy file is kept as a one-line re-export so any straggler imports
  keep working. Removal target: next major version bump. Migration is
  completed in `src/` (3 production files moved to `_notifier_compat`);
  only the two shim-specific tests still use the old path on purpose.

### Fixed

- **`services/forecast_service.py` V11 Chronos bridge (Sprint J6)** —
  import error when `chronos_service.py` lived in the V11 archive instead
  of the V12 source tree. Fixed via `ensure_v11_archive_on_path()` called
  from `services/forecast_service.py` at first use. The accompanying
  test `test_j6_chronos_import.py` locks the bridge contract.

### Security

- No new security advisories. Existing INV-1..INV-7 invariants unchanged.

---

## [12.0.0] — 2026-07-09

Initial V12 release baseline (pre-council-review). Includes the
post-migration code structure: `src/stockapp_v12/` promoted to repo root,
`tests/` flattened, `pyproject.toml` `pythonpath = ["src", "scripts"]`,
`testpaths = ["tests"]`.

Sprints F and G (the earliest council-review fixes) shipped in this
window:
- **Sprint F** — signal-rule labels + fail-closed posture in `R-06`
- **Sprint G** — runbook + documentation completeness for P1-5 / P1-10

See [Sprint-F-Completion.md](docs/reviews/council-review-2026-07-09/Sprint-F-Completion.md)
and [Sprint-G-Completion.md](docs/reviews/council-review-2026-07-09/Sprint-G-Completion.md)
for the F/G-specific record.

---

## Change-log conventions

- **Added** — new files, public classes, public functions, public dataclasses
- **Changed** — signature changes, default-value changes, file moves
- **Deprecated** — public APIs kept for backward compatibility but
  scheduled for removal
- **Removed** — public APIs deleted (rare; usually only after a deprecation
  cycle)
- **Fixed** — bug fixes
- **Security** — security-relevant changes
- **Structural** — internal file moves / renames that don't affect the
  public API (folded into Changed)

All entries must reference the council-review sprint that produced them
(when applicable) so readers can cross-link to the per-sprint record in
`docs/reviews/council-review-2026-07-09/`.
