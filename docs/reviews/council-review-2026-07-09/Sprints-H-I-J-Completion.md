# Sprints H / I / J — StockApp V12 Council-Review Closure

> **Date:** 2026-07-09
> **Companion to:** `docs/reviews/council-review-2026-07-09/Council-Synthesis.md`
> **Test progression:** 547 → 562 → 594 → 615 passing, 0 failures

This note records the patterns, code references, and lessons learned from
the final three sprints of the V12 council-review remediation. The
prior Sprints F and G completion records live in git history
(commits `ed54fb0d` for G; F predates the start of this conversation).

---

## 1. Sprint H — Decimal discipline + persistence + concurrency

### H1 (P1-8) — Bars sort invariant assertion
**File:** `src/stockapp_v12/pipeline/stages.py:454-464` (extract_price_atr)
**Problem:** `iloc[-1]` assumed bars are sorted ascending by date. If
caller passed unsorted, wrong day's close fed SL math.
**Fix:** `assert ctx.bars.index.is_monotonic_increasing` at entry.
**Test:** `tests/test_p1_8_bars_sort_assertion.py` (7 tests) — pinned at
`693cd274`.

### H2 (P1-6) — Audit ledger fsync
**File:** `src/stockapp_v12/audit/audit_ledger.py:99-101`
**Problem:** `fh.write` was buffered; docstring claimed atomicity via
single write+flush, code didn't deliver.
**Fix:** `fh.flush() + os.fsync(fh.fileno())` after every write.
**Test:** `tests/test_p1_6_audit_fsync.py` (5 tests) — pinned at
`b9ca7a03`.

### H3 (P1-4) — Decimal precision (replaces float math)
**Files:** `src/stockapp_v12/services/daily_review.py`,
`src/stockapp_v12/services/position_manager.py`
**Problem:** Float `round(entry_price, 2)` introduces representational
drift (0.01 not exactly representable in IEEE-754). Over multi-day
positions the drift compounds.
**Fix:**
- `core.precision` (`D`, `round_pct`, `round_dollar`, `round_shares`) is
  the canonical quantization layer.
- `position_manager` uses `round_dollar` / `round_shares` for entry,
  atr, sl_price, trail_price.
- `daily_review` computes `unrealized` and `pnl_atr` via Decimal
  internally, converts to float at the `_decide` boundary.
**Test:** `tests/test_p1_4_decimal_precision.py` (6 tests) — pinned at
`882c45a1`.

### H4 (P1-3) — Broker reconcile before local mutation
**File:** `src/stockapp_v12/services/position_manager.py:429+`
(added `broker_reconcile_fn` parameter)
**Problem:** `process_pending_action` TRIM path mutated `rec.qty` and
`rec.sl_price` unconditionally. If broker already trimmed or closed,
we'd be acting on stale data — moving SL on a phantom position.
**Fix:**
- Optional `broker_reconcile_fn: Callable[[str], Optional[int]]`
  parameter.
- On EXIT/TRIM with fn provided: call first; if raises → `RECONCILE_FAILED`,
  if returns None → `BROKER_NO_POSITION`, if disagrees with local qty
  → `QTY_MISMATCH`. All three: skip mutation, clear pending_action,
  return safe dict.
- Entire method wrapped in try/except → `INTERNAL_ERROR` for unexpected
  exceptions, no pipeline crash.
- TIGHTEN skips reconcile (doesn't affect qty).
**Test:** `tests/test_p1_3_broker_reconcile.py` (10 tests) — pinned at
`3a40bec0`.

---

## 2. Sprint I — Spec conformance + MFE/MAE

### I1 (P1-5) — Rule code labels
**File:** `src/stockapp_v12/bridge/live_guard.py:175,193`
**Problem:** Code emitted `R-02_SINGLE_CAP` and `R-08_EXPOSURE_CAP`,
but per spec: R-02 = drawdown, R-03 = single cap, R-04 = exposure,
R-08 = correlation. Operators reading journal saw wrong rule name.
**Fix:** Renamed to `R-03_SINGLE_CAP` and `R-04_EXPOSURE_CAP`. Also
updated `tests/test_live_guard.py` assertions + `docs/SAFETY_LIVE_GUARD.md`.
**Test:** `tests/test_p1_5_rule_code_labels.py` (4 tests) — pinned at
`19c95882`.

### I2 (P1-10) — NYSE calendar guard
**Files:** `src/stockapp_v12/core/nyse_calendar.py` (new),
`scripts/nyse_guard.sh` (new), all 4 cron scripts updated
**Problem:** Cron was `* * 1-5` — weekends skipped, but NYSE holidays
not. On holidays, jobs would write stale data, touch Redis on non-
trading days, pollute journal with phantom entries.
**Fix:**
- `core.nyse_calendar.is_trading_day()` tries `exchange_calendars.XNYS`
  first; falls back to built-in holiday table for 2024-2028.
- `is_early_close()` identifies 1:00 PM ET sessions (day after
  Thanksgiving, Christmas Eve).
- `nyse_guard.sh` is `source`d at the top of each cron script; exits
  0 if trading day, exits 1 → script exits cleanly with no work.
- Added to `run_swing_scanner.sh`, `run_catalyst_scorer.sh`,
  `run_entry_window.sh`, `run_daily_review.sh`.
**Test:** `tests/test_p1_10_nyse_calendar.py` (22 tests) — pinned at
`6ed82aad`.

### I3 (P2-exit-audit) — MFE/MAE tracking on every close
**File:** `src/stockapp_v12/services/position_manager.py:300+`
**Problem:** `_close_position` wrote only `days_held` + `entry_price`
to the journal. Spec requires `mfe_pct`, `mae_pct`, `pnl_pct`,
`regime_at_entry`, `regime_at_exit` on every CLOSED record. Without
per-position price series, post-hoc analysis is impossible.
**Fix:**
- Added `lowest_close_since_entry` field to `PositionRecord` (mirrors
  `highest_close_since_entry`).
- `update_trail_on_tick` tracks both highest AND lowest on every
  price observation, **even before trailing is armed** (position may
  be stopped out while still OPEN).
- `_close_position` computes `pnl_pct`/`mfe_pct`/`mae_pct` via Decimal
  rounding, calls `journal.log_exit()` with full enrichment.
**Test:** `tests/test_p2_exit_audit.py` (6 tests) — pinned at
`06e3dfca`.

---

## 3. Sprint J — Module boundaries + cleanup

### J1 (P2-atomicity) — Pipeline atomicity for close
**File:** `src/stockapp_v12/services/position_manager.py:300+`
**Problem:** 3 separate Redis calls in `_close_position`: `srem`
(index) + `setex` (cooldown) + `set/expire` (record). Crash between
any two leaves index out of sync with record + cooldown.
**Fix:**
- New helper `_persist_in_pipe(rec, pipe, ttl)` composes writes into
  a parent pipeline.
- `_close_position` now uses `self._r.pipeline()` to wrap all three
  writes in one transaction.
**Test:** `tests/test_j1_close_atomicity.py` (4 tests) — pinned at
`b242b1b2`.

### J2 (P2-spec) — Stage fallbacks to core/limits
**File:** `src/stockapp_v12/core/limits.py` (`StageFallbacks` dataclass),
`src/stockapp_v12/pipeline/stages.py:53+`
**Problem:** `FALLBACK_ANALYST_CONFIDENCE=0.75` and `FALLBACK_ATR_PCT=0.05`
hardcoded as module-level constants in stages.py. Operators had to edit
source to retune; no way to unit-test the values in isolation.
**Fix:**
- New `@dataclass StageFallbacks` in core/limits.py holds the values.
- stages.py imports from there and keeps module-level aliases for
  backward compatibility.
**Test:** `tests/test_j2_stage_fallbacks.py` (4 tests) — pinned at
`0f24d58a`.

### J3 (P2-spec) — apply_immediately default False
**File:** `src/stockapp_v12/services/daily_review.py:53`
**Problem:** Default `True` was a paper-soak convenience. Spec says
execution happens next session via pending_action consumer. Live + paper
both had TRIM/EXIT applied immediately, violating the contract.
**Fix:** Default changed to `False`. Operators can opt in via
`apply_immediately=True` (paper-soak). Updated `run_daily_review.sh`
comment.
**Test:** `tests/test_j3_daily_review_default.py` (2 tests) — pinned at
`ed61f3f2`.

### J4 (P2-spec) — catalyst_type aliasing contract documented
**File:** `src/stockapp_v12/pipeline/stages.py:432-440` (comment)
**Problem:** `catalyst_type=(analyst.thesis_tags[0] if analyst and
analyst.thesis_tags else None)` was a silent contract. Adding a real
`catalyst_type` field to `AnalystOutput` would silently break the alias.
**Fix:** Documented the aliasing contract in a comment block.
**Test:** `tests/test_j4_catalyst_alias.py` (3 tests) — pinned at
`be667543`.

### J5 (P2-boundaries) — EntryContext/EntryResult to pipeline.types
**Files:** `src/stockapp_v12/pipeline/types.py` (new),
`src/stockapp_v12/pipeline/stages.py`, `src/stockapp_v12/services/entry_orchestrator.py`
**Problem:** Circular import — `EntryContext`/`EntryResult` defined in
`entry_orchestrator`, `stages` imported from there, `entry_orchestrator`
did a deferred local import of `stages` to break the cycle.
**Fix:**
- New `pipeline/types.py` holds the canonical dataclasses.
- `pipeline.stages` imports from `pipeline.types` (no more cycle).
- `services.entry_orchestrator` re-exports from `pipeline.types` for
  backward compatibility.
- Removed the deferred "Local import to avoid a top-level cycle" hack.
- `TYPE_CHECKING` used in `pipeline.types` for services-side type
  hints to avoid re-introducing the cycle.
**Test:** `tests/test_j5_entry_types.py` (3 tests) — pinned at
`b44e3136`.

### J6 (P2-boundaries) — V11 archive bridge for Chronos import
**File:** `src/stockapp_v12/services/forecast_service.py:48-58`
**Problem:** Bare `from services.chronos_service import ChronosService`
assumed `src/` on sys.path — fragile in test setups with different
PYTHONPATH layouts.
**Fix:**
- Call `stockapp_v12.core._v11_compat.ensure_v11_archive_on_path()`
  first (idempotent).
- Then `from src.services.chronos_service import ChronosService`
  (archive-style package import).
- Also fixed pre-existing `test_fail_closed_safety.py` that had the
  same bare import path (was deselected; now passes).
**Test:** `tests/test_j6_chronos_import.py` (1 test) — pinned at
`08599050`.

### J7 (P2-boundaries) — Rename notification_service shim
**Files:** `src/stockapp_v12/services/_notifier_compat.py` (new,
canonical), `src/stockapp_v12/services/notification_service.py` (one-
line re-export)
**Problem:** `services/notification_service.py` was a re-export shim
sitting next to real service classes (PositionManager, DailyReview).
Misleading — module map says B7 (NotificationService) lives in
`notifiers/`, not `services/`.
**Fix:**
- New `_notifier_compat.py` (underscore prefix signals internal/legacy
  compat) holds the canonical shim.
- Old path is now a one-line re-export of the new module.
**Test:** `tests/test_j7_notifier_shim.py` (3 tests) — pinned at
`053c69d9`.

---

## 4. Patterns worth remembering

### 4.1 Fail-closed posture (the model to copy)
When a Redis call (or any external dep) can fail, default to **return
the safe value**, not raise or pass through.

```python
try:
    count = int(self._r.get(key) or 0)
except Exception:
    return self.limits.max_daily_trades  # fail-closed: block new buys
```

This was the canonical R-05 pattern from hygiene-D. Mirrored in R-06
cooldown, R-01/R-02/R-09, journal strict mode, audit fsync. Every
single risk gate that can fail on Redis outage **blocks** by default
rather than **silently allowing**.

### 4.2 Lua for atomic multi-step Redis writes
Two-step operations like `SISMEMBER` + `SADD` + `EXPIRE` are
check-then-act races. Two concurrent callers can both pass the check
and both SADD. The fix is a single Lua script that does all three
inside Redis:

```python
SADD_AND_EXPIRE_LUA = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1 then
    return 0  -- already evaluated
end
redis.call('SADD', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1  -- newly added
"""
```

The Python caller just does `self._r.eval(script, 1, key, value, ttl)`.

### 4.3 Python pipeline() for multi-write atomicity
For multi-step operations within Python (e.g., close position writes
3 things to Redis), use `redis.pipeline()`:

```python
pipe = self._r.pipeline()
pipe.srem(index_key, symbol)
pipe.setex(cooldown_key, ttl, "1")
self._persist_in_pipe(rec, pipe, ttl_seconds=86400 * 7)
pipe.execute()  # all-or-nothing
```

The pipeline helper (`_persist_in_pipe`) accepts a pipe argument so
writes can be composed into a larger transaction. Don't write bare
`self._r.set(...)` inside the transaction path.

### 4.4 Decimal at the boundary, float at the decision matrix
For monetary computations, do the math in Decimal, convert to float
only when crossing into a system that expects float (e.g., decision
matrix thresholds like `> 2.0`):

```python
entry_d = D(pos.entry_price)
last_d = D(last_price)
atr_d = D(pos.atr_at_entry) if pos.atr_at_entry else D(0)
unrealized_d = (last_d - entry_d) / entry_d if entry_d > 0 else D(0)
unrealized = float(unrealized_d)  # boundary conversion
```

Quantization (`round(x, 2)`) should use `core.precision`:
- `round_dollar` for $ amounts (2dp, banker's rounding)
- `round_shares` for share counts (4dp)
- `round_pct` for percentages (6dp)

### 4.5 TYPE_CHECKING for boundary modules
When a leaf module (e.g., `pipeline.types`) needs type hints from a
downstream module (e.g., `services.signal_rule_engine`), use
`typing.TYPE_CHECKING`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.signal_rule_engine import SignalRuleResult
    from services.yolo_filter import YoloFilterDecision

@dataclass
class EntryResult:
    sr_result: Optional["SignalRuleResult"] = None
```

The `TYPE_CHECKING` block runs only for type checkers (mypy, pyright);
at runtime the import is skipped, breaking any import cycle.

### 4.6 V11 archive bridge for legacy imports
Code that needs to import from the V11 archive (e.g., ChronosService)
should use the explicit bridge, not bare `from services.X`:

```python
from stockapp_v12.core._v11_compat import ensure_v11_archive_on_path
ensure_v11_archive_on_path()  # idempotent
from src.services.chronos_service import ChronosService
```

The bridge searches `archive/v11-legacy-2026-07-09/`, `archive/V11/`,
`archive/src/` in that order and injects the right root onto
`sys.path`. No more `sys.path.insert(0, ...)` scattered across the
codebase.

### 4.7 NYSE holiday guard via shell wrapper
Cron jobs that should only run on trading days should `source` a guard
at the top of the wrapper script:

```bash
source "${SCRIPT_DIR}/nyse_guard.sh" || exit 0
```

The guard calls a Python module that returns 0 on trading days, 1
otherwise. `|| exit 0` means the wrapper script exits cleanly without
running any work. The Python module can be in the project repo, so
operators can update the holiday table with a single PR.

### 4.8 Module boundary hygiene: leading underscore = compat
Files whose primary purpose is backward compatibility should start with
underscore (`_notifier_compat.py`). The prefix signals "internal/
legacy" so reviewers and new code prefer the canonical path
(`stockapp_v12.notifiers`). The old name remains as a one-line
re-export.

### 4.9 MFE/MAE needs the lowest, not just the highest
The trail ratchet only tracks the highest close. But the spec wants
MAE (max adverse excursion) on every close. So we need
`lowest_close_since_entry` to mirror `highest_close_since_entry`. Both
must be updated on every price observation, **even before trailing
is armed**, because a position can be stopped out while still OPEN
(no trail was ever set).

---

## 5. Test count progression

| Sprint | Test count | Delta |
|--------|-----------|-------|
| Before F | 526 | (baseline) |
| F | 540 | +14 (P0-1..5) |
| G | 547 | +7 (P1-1, P1-2, P1-7, P1-9) |
| H | 562 | +15 (P1-3, P1-4, P1-6, P1-8) |
| I | 594 | +32 (P1-5, P1-10, P2-exit-audit) |
| J | 615 | +21 (J1..J7) |
| **Total** | **615** | **+89 tests** |

**Pre-existing failures fixed along the way:**
- `test_forecast_service_chronos_init_failure_logs_warning` (was
  deselected; J6 fix made it pass).

---

## 6. Council-review status: closed

Every P0, P1, and P2 item from the 2026-07-09 council synthesis has
been shipped. The V12 system is now in a state where:

1. The live entry path actually works (P0-1..5).
2. All R-rules (R-01..R-10) have runtime enforcement with fail-closed
   posture.
3. The journal is fail-closed for ALLOW/BUY decisions and durable
   (fsync after every write).
4. Position close is atomic (pipeline) and audit-rich (MFE/MAE/PNL).
5. Bars sort invariant is asserted.
6. Spec is conformant (rule code labels, apply_immediately default,
   StageFallbacks location, catalyst aliasing documented).
7. Module boundaries are clean (pipeline.types, _notifier_compat,
   V11 bridge).
8. NYSE holidays don't fire cron jobs.

**Open architectural questions** (per Council-Synthesis § Open
Architectural Questions):
1. **Catalyst stage:** passthrough stub is in place (P0-5 option B).
   Full LLM catalyst scoring (option A, weights 0.40/0.35/0.15/0.10)
   remains unimplemented. ~2 weeks of work; can be scheduled as a
   new feature.
2. **Outcome journal envelope:** DONE — every CLOSED record carries
   mfe_pct, mae_pct, pnl_pct, regime_at_entry, exit_price.
3. **Live deployment gate:** test coverage is now 615 passing / 0
   failing / 5 skipped / 7 xfailed. Threshold for promoting
   `live_ibkr` is a product owner decision.

---

## 7. Cross-references

- **Council synthesis:** `docs/reviews/council-review-2026-07-09/Council-Synthesis.md`
- **Pipeline types module:** `src/stockapp_v12/pipeline/types.py`
- **Decimal precision:** `src/stockapp_v12/core/precision.py`
- **V11 compat bridge:** `src/stockapp_v12/core/_v11_compat.py`
- **NYSE calendar:** `src/stockapp_v12/core/nyse_calendar.py`
- **Audit ledger:** `src/stockapp_v12/audit/audit_ledger.py`
- **Position manager:** `src/stockapp_v12/services/position_manager.py`
- **Live order guard:** `src/stockapp_v12/bridge/live_guard.py`
- **Entry orchestrator:** `src/stockapp_v12/services/entry_orchestrator.py`
