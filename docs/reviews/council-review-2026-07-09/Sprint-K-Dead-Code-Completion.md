# Sprint K — Dead-Code Cleanup (Council Plan Tail)

> **Date:** 2026-07-09
> **Companion to:** `docs/reviews/council-review-2026-07-09/Council-Synthesis.md`
> **Test progression:** 615 → 616 passing, 0 failures
> **Commits:** `f4ce383d` (K1) · `553e8b2a` (K2) · `818ad694` (K3)

This note records the final tail of the 2026-07-09 council review
remediation — the small dead-code cleanup that the council flagged as
"worth doing" but did not list under P0/P1/P2. Three sub-sprints shipped
in sequence; each is small on its own but together they remove 196 dead
imports, deduplicate a test helper, and formally deprecate the legacy
notifier shim path.

---

## 1. K1 — F401 unused-import sweep (commit `f4ce383d`)

**Scope:** every `F401` (imported but unused) violation in `src/` and `tests/`.
**Tool:** `ruff check --select F401 --fix src/ tests/`

### Numbers

| Tree        | F401 found | Files touched |
|-------------|-----------:|--------------:|
| `src/`      |         28 |            16 |
| `tests/`    |        138 |            49 |
| **Total**   |    **166** |        **65** |

Net diff: 65 files changed, 31 insertions, 107 deletions.

### Highlights in `src/`

- `services/entry_orchestrator.py` — the largest cleanup. Dropped
  `dataclass`, `pandas as pd`, `Set`, `ForecastOutput`, `YoloFilterDecision`,
  `SignalRuleResult`, and `increment_daily_trade_count`. These were left
  over from the Sprint J5 refactor that moved `EntryContext` / `EntryResult`
  to `pipeline/types.py` and from the P0-4 wiring that moved
  `increment_daily_trade_count` to `bridge/gatekeeper.py`.
- `pipeline/stages.py` — dropped unused `pandas as pd`.
- `bridge/live_guard.py`, `core/sizing.py`, `services/position_manager.py`
  and others — clean up stale `typing.Tuple` / `typing.Dict` imports.

### Highlights in `tests/`

49 test files cleaned up. The biggest signal was in
`tests/test_veto_alarm.py` (9 lines removed) and `tests/test_yolo_filter_sr06.py`
(3 lines removed) — both had speculative SR_* constants from earlier
exploration that were never actually referenced.

### Verification

```
$ ruff check --select F401 src/ tests/
All checks passed!

$ pytest tests/ -q
=========== 615 passed, 5 skipped, 92 deselected, 7 xfailed ===========
```

---

## 2. K2 — Consolidate duplicate `_register` test helper (commit `553e8b2a`)

**Scope:** the simple "register a position with sensible defaults" helper
that lived in three test files with slightly different defaults.

### Before

| File                              | Helper name              | Distinguishing default            |
|-----------------------------------|--------------------------|-----------------------------------|
| `tests/test_j1_close_atomicity.py`  | `_register(pm, **kwargs)` | (none)                          |
| `tests/test_p2_exit_audit.py`       | `_register(pm, **kwargs)` | `regime="BULL_LONG"`            |
| `tests/test_p1_3_broker_reconcile.py` | `_register_with_pending(...)` | also sets `pending_action`   |

### After

`tests/conftest.py` exposes a single `register_test_position(pm, **kwargs)`
helper. Each test file keeps a thin `_register` shim that calls the shared
helper and applies only the per-file additional default. `_register_with_pending`
in `test_p1_3_broker_reconcile.py` now delegates the actual `register()` call
to the shared helper, then applies its unique `pending_action` mutation.

### Why a shim, not a direct rename

Call sites in all three files use `_register(pm, symbol="AAPL", ...)` — keeping
the shim means zero call-site changes. The shim approach also preserves the
historical `regime="BULL_LONG"` default in `test_p2` without smuggling it
into the shared helper (which would be wrong for `test_j1` and `test_p1_3`).

### Verification

```
$ pytest tests/test_j1_close_atomicity.py tests/test_p2_exit_audit.py tests/test_p1_3_broker_reconcile.py
============================== 20 passed in 0.07s ==============================

$ pytest tests/ -q
=========== 615 passed, 5 skipped, 92 deselected, 7 xfailed ===========
```

### Scope NOT taken

The four `_make_pm(fake_redis)` factories that build a stateful fake Redis
are also near-duplicates, but they have meaningful differences
(pipeline-call tracking in `test_j1`, `V12RiskLimits` injection in
`test_position_lifecycle`). Consolidating those was deliberately deferred —
the risk/reward did not justify a bigger refactor for a tail-end cleanup.

---

## 3. K3 — Formally deprecate `notification_service.py` legacy shim (commit `818ad694`)

**Scope:** make the P2-boundaries migration from Sprint J7 visible. The
canonical shim home (`services/_notifier_compat.py`) was created during
Sprint J7, but the legacy `services/notification_service.py` remained a
silent re-export. This sprint adds the deprecation signal.

### What changed

| File | Change |
|------|--------|
| `src/stockapp_v12/services/notification_service.py` | + `DeprecationWarning` fires on import, pointing stragglers to `_notifier_compat` (singleton) or `notifiers` (backend selection). Docstring marked DEPRECATED. |
| `src/stockapp_v12/pipeline/stages.py` | 2 imports moved to `_notifier_compat` |
| `src/stockapp_v12/services/daily_review.py` | 1 import moved to `_notifier_compat` |
| `src/stockapp_v12/services/position_manager.py` | 1 import moved to `_notifier_compat` |
| `tests/test_j7_notifier_shim.py` | + `test_legacy_path_emits_deprecation_warning` regression test |

### Production code no longer triggers the warning

The 3 `src/` files were migrated to the new shim so that normal operation
emits zero `DeprecationWarning`. The warning only fires from the two test
files that specifically exercise the legacy path — and that is the
intended visibility: the warning appears in test output as a permanent
migration reminder.

### The deprecation contract is now locked

`test_legacy_path_emits_deprecation_warning` in `test_j7_notifier_shim.py`
performs a fresh `importlib.import_module` and asserts that a
`DeprecationWarning` fires. This prevents a future refactor from silently
dropping the deprecation signal.

### Verification

```
$ pytest tests/ -q
===== 616 passed, 5 skipped, 92 deselected, 7 xfailed, 1 warning in 3.93s =====
```

The single warning is the expected `DeprecationWarning` from
`test_notifier_protocol.py:20` (which exercises the legacy import on
purpose to verify identity with the new path).

---

## 4. Final K-verify

```
$ ruff check --select F401 src/ tests/
All checks passed!

$ pytest tests/ -q
===== 616 passed, 5 skipped, 92 deselected, 7 xfailed, 1 warning in 3.76s =====
```

Test count progression across the full council review:

| Stage                | Passing | Delta | Source                          |
|----------------------|--------:|------:|---------------------------------|
| Pre-council          |     ~547 |   —   | baseline                        |
| After Sprint F       |     562 |  +15  | signal-rule + risk-default gates |
| After Sprint G       |     562 |    0  | doc + runbook (no test churn)   |
| After Sprint H       |     594 |  +32  | audit + Decimal + lifecycle     |
| After Sprint I       |     605 |  +11  | rule labels + NYSE + MFE/MAE    |
| After Sprint J       |     615 |  +10  | 7 P2 items                     |
| **After Sprint K**   | **616** | **+1**| deprecation regression test     |

**Test loss: 0.** No test that passed at the start of the council review
was deleted or weakened by the K-cleanup.

---

## 5. Follow-up notes (not in K scope)

- **F841 unused local variables (10 issues)** — `e` in `core/limits.py:85`,
  `journal` in `scripts/preflight.py:167`, `forecast_ret_pct_pct` in
  `ensemble_chronos_backend.py:275`, `sym` in `entry_orchestrator.py:115`,
  `cls_json` in `yolo_filter.py:147`, plus 5 in `tests/`. These are real
  dead code but they are a different category from the user's stated
  "unused imports" scope. Worth a follow-up sweep.
- **Notifier shim removal** — the legacy `services/notification_service.py`
  is now formally deprecated. Once all internal callers have migrated
  (only the two regression tests in `test_j7_notifier_shim.py` and
  `test_notifier_protocol.py` still use the path on purpose), the file
  can be deleted in a future major version bump.
- **`_make_pm` consolidation** — the four stateful-fake-Redis patterns
  in `test_j1_close_atomicity.py`, `test_p2_exit_audit.py`,
  `test_p1_3_broker_reconcile.py`, and `test_position_lifecycle.py` are
  near-duplicates. A future refactor could extract a `stateful_redis`
  fixture to `conftest.py` with each test adding its own per-suite
  pipeline-tracking / limits-injection layer.
