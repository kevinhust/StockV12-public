# V12 Council Review — Round 2 (post Sprints F–K)

> **Date:** 2026-07-10
> **Reviewers:** Architect · Skeptic · Pragmatist · Critic (4 voices, parallel)
> **Companion to:** [`Council-Synthesis.md`](Council-Synthesis.md) — Round 1, 2026-07-09
> **Method:** Each voice audited the post-Sprint-K code end-to-end against the
> Round-1 findings; this synthesis merges the 4 positions into a prioritized
> Sprint L plan.

---

## TL;DR Verdict

| Voice | Verdict |
|---|---|
| Architect | **Framing is stale.** The Round-1 P0 wiring items are fixed at the code level. The next round is about operational reality, fix-induced regression, and coverage gaps that were laundered as "pre-existing failure". |
| Skeptic | **The 92 deselected tests are an unverified gap.** Many may actually pass on the post-F-J code. The cheapest next sprint is to run them, not write more fixes. |
| Pragmatist | **Paper-soak is silently broken on the Mac Mini.** Runner scripts hardcode Linux paths; bash preflight exits 0 on failure; cron fires every 5 min and writes SKIP log entries while doing nothing. |
| Critic | **The "INV-5 fix" moved the atomicity window, it didn't close it.** `pipe.execute()` first, `journal.append(strict=True)` second — a journal blip raises after the position is already mutated in Redis. |

**Consolidated verdict:** The in-process library is in good shape (616 tests pass, P0/P1/P2 issues from Round 1 are all addressed). The **operational wiring is not**. The system that "can trade in paper" per Sprint F is actually silently no-op'ing in cron because the bash wrappers are Linux-pathed and the preflight swallows import failures. Live_ibkr promotion is blocked by 5 shadow-gate test failures, not by the 92 deselected headline number. Round 1 also introduced a fix-induced INV-5 regression in `_close_position` / `register` where journal errors propagate after Redis mutation.

---

## Round 1 → Round 2 deltas

| Area | Round 1 (2026-07-09) | Round 2 (2026-07-10) |
|------|----------------------|----------------------|
| 5 P0 wiring | All addressed in Sprint F | Verified addressed in code; **operational wrappers still broken** |
| 10 P1 atomicity / R-rules / Decimal | All addressed in Sprints G/H/I | **`position_manager._close_position` journal-after-pipe is a NEW atomicity gap** |
| 8 P2 boundaries / cleanup | All addressed in Sprints J/K | `notification_service.py` deprecation can be filtered; **no operator alerting on L2_DEGRADED** |
| 4 missing concurrency tests | Flagged in Round 1 | **Still missing** (not in L1–L4 priority; deferred to L9) |
| 11 pre-existing test failures | "Not real bugs" per Round 1 | **5 of them (shadow-gate) ARE the live_ibkr gate** |
| Coverage threshold for live_ibkr | Open architectural question | **Still unresolved** |

---

## Sprint L — Recommended plan (9 items, prioritized)

### L1 (P0-blocker) — Fix hardcoded Linux paths in `scripts/*.sh`
**Voice:** Pragmatist · **Effort:** ½ day
**Files:** `scripts/run_entry_window.sh:44-45`, `run_daily_review.sh:35-36`,
`run_swing_scanner.sh:34-35`, `run_catalyst_scorer.sh:12`, `_preflight.sh:49,54`
**Problem:** Every runner script hardcodes `/home/app/Stockapp/`. On the
actual Mac Mini (`/Users/dev/Stockapp`) the preflight import check
fails → `PRE_RC != 0` → wrapper does `exit 0` cleanly. Cron fires every
5 min and writes `SKIP — venv_missing` to `logs/pipeline/skip.log` while
doing literally nothing.
**Fix:** Path-agnostic resolution via `$(dirname "$0")/../resolve_paths.sh`
or env var `STOCKAPP_ROOT` with `${STOCKAPP_ROOT:-$(realpath ..)}` fallback.
**Why first:** Without this, every other L-item is moot. The system is
silently no-op in paper-soak right now.

### L2 (P0-blocker) — Wire `L2_DEGRADED` to `WebhookNotifier.notify_degraded()`
**Voice:** Pragmatist · **Effort:** ¼ day
**Files:** `scripts/run_catalyst_scorer.sh:44,76,81`; `notifiers/webhook_notifier.py`
**Problem:** `L2_DEGRADED` is a log marker, not a pager event. The
`WebhookNotifier` exists and is wired to order events; no path in
`run_catalyst_scorer.sh` calls `notify_degraded()` on tier1-passthrough.
**Fix:** Add `python -m stockapp_v12.notifiers.cli notify-degraded` call
on the L2_DEGRADED branch. Verify webhook_url env is set in production.
**Why first:** Operator in the loop is a pre-condition for live_ibkr.

### L3 (live_ibkr gate) — Repair 5 shadow-gate test failures
**Voice:** Pragmatist · **Effort:** 1 day
**Files:** `tests/test_shadow_trade.py::test_shadow_gate_*` (4 failures),
`tests/test_shadow_ensemble.py::test_constants_match_ensemble_backend` (1 failure)
**Problem:** 58 of 63 shadow tests pass. The 5 that fail are the
`check_shadow_gate` decision tests — the live_ibkr promotion gate.
**Why first:** Until these pass, live_ibkr promotion is unverifiable.
The "92 deselected" headline obscures this; the real blocker is 5.

### L4 (P0-blocker) — Catch `JournalWriteError` at position-mutating paths
**Voice:** Critic · **Effort:** ½ day
**Files:** `src/stockapp_v12/services/position_manager.py:209-232` (register),
`:320-355` (_close_position)
**Problem:** Both methods call `pipe.execute()` first, then
`journal.append(strict=True)`. Neither catches `JournalWriteError`. A
Redis blip on the journal raises after the position is already mutated
in Redis → mid-market trail-loop crash + silent INV-5 violation.
**Fix:** Either (a) wrap the journal call in try/except and write-back
on failure, or (b) journal BEFORE the Redis mutation (and have the
mutation re-fetch state). Option (b) is safer; option (a) is faster.
**Why first:** Fix-induced regression from H3 / J1 atomicity improvements;
worse than the original Round-1 issue.

### L5 (1-day) — Re-run the 92 deselected tests
**Voice:** Skeptic · **Effort:** ¼ day
**Files:** `tests/test_paper_soak_sign_agreement.py`,
`tests/test_shadow_ensemble.py`, `tests/test_shadow_trade.py`,
`tests/test_live_guard.py::test_min_hold_blocks_early_exit`
**Problem:** The "pre-existing failure" label has been carried through
6 sprints without anyone re-running them. `test_paper_soak_sign_agreement.py`
imports cleanly and looks well-formed; `test_min_hold_blocks_early_exit`
passes today (clock-dependent, ≥2 days after July 7).
**Fix:** `git stash` the deselect markers, run the suite, read the diff.
For each test: either fix it (real coverage gap), pass (retire the
deselect), or document the actual blocker.
**Why now:** Cheapest signal we have. Flips the framing for free.

### L6 (1-day) — Extend `nyse_calendar.py` to 2029+, add parity check
**Voice:** Critic · **Effort:** ¼ day
**Files:** `src/stockapp_v12/core/nyse_calendar.py:154-160` (the
`exchange_calendars` first-try path) + the built-in `_FULL_DAY_HOLIDAYS`
table (currently 2024–2028)
**Problem:** `date(2029, 1, 1)` (Monday, New Year's Day) returns `True`
from `is_trading_day` because the built-in table doesn't cover 2029. The
`exchange_calendars` library is tried first; if installed, its answer
wins with no parity check vs. the built-in.
**Fix:** Add 2029+ holidays to the built-in table; add an assertion that
both paths agree when both are available.

### L7 (1-day) — `StageFallbacks` → `frozen=True`; document notifier timing
**Voice:** Critic · **Effort:** ¼ day
**Files:** `src/stockapp_v12/core/limits.py:57` (StageFallbacks not
frozen); `src/stockapp_v12/services/_notifier_compat.py:34` (singleton
instantiated at module import)
**Problem:** StageFallbacks is a non-frozen dataclass; module-level
aliases `FALLBACK_ANALYST_CONFIDENCE` in `pipeline/stages.py:58-59` are
bound once at import. The `_Notifier()` singleton binds to whatever
logging handlers exist at module import time — if cron imports before
`logging.basicConfig()`, all notifications go to `lastResort`/stderr.
**Fix:** `@dataclass(frozen=True)` on StageFallbacks; document the
singleton-import-time constraint; add a smoke test in preflight that
imports `_notifier_compat` AFTER `logging.basicConfig`.

### L8 (later) — Decimal/float boundary at API seams
**Voice:** Skeptic · **Effort:** 1 day
**Files:** `src/stockapp_v12/bridge/live_guard.py:62` (`price: float`),
`src/stockapp_v12/services/position_manager.py:191-195`
(`float(round_dollar(entry_price))`)
**Problem:** The "Decimal everywhere" claim is inside-the-fence. At the
API seams, the round runs on a float that already lost precision.
**Fix:** Change `price` to `Decimal` in `LiveGuardContext`; add
`to_decimal_price` helper to `core/precision.py`.

### L9 (later) — Original Round-1 concurrency test gaps
**Voice:** original Council-Synthesis (carried over) · **Effort:** 1 day
**Files:** new tests for `process_pending_action` 2-thread contention,
`entry_guard` Lua atomicity under 8 threads, `webhook_notifier.flush()`
race, `_close_position` MFE/MAE invariants
**Problem:** Round 1 flagged 4 test gaps; only the MFE/MAE one was filled
in Sprint I. The other 3 were deferred.
**Fix:** Add 4 thread/concurrency tests; verify behavior under contention.

---

## Total effort estimate

| Bucket | Items | Days |
|--------|-------|------|
| P0-blockers (must fix before live_ibkr) | L1, L2, L4 | 1.25 |
| Live_ibkr gate | L3 | 1.0 |
| Hygiene (real coverage and robustness) | L5, L6, L7 | 0.75 |
| Lower priority (real but deferrable) | L8, L9 | 2.0 |
| **Total** | **9 items** | **~5 days** |

The first 1.25 days (L1, L2, L4) unblock the next 2.75 days. L3 (1 day)
is the actual live-promotion gate. L5–L7 (0.75 day total) are cheap
hygiene that flips the operational framing. L8–L9 can wait.

---

## What the council got right vs. wrong vs. hidden

### Right (Round 1)
- 5 P0 wiring items were real and necessary
- Decimal discipline in sizing was correct and tested
- Trail ratchet logic was correct
- R-05 fail-closed was the model to copy
- 7-stage EntryOrchestrator decomposition was the right shape

### Wrong or incomplete (Round 1)
- **"92 deselected" was the wrong headline.** The 5 shadow-gate failures
  are the actual blocker. The council conflated "deselected" with "broken".
- **"11 pre-existing failures are not real bugs"** (Council-Synthesis §Test
  Gap Summary) was overconfident. At least 5 of them are the live_ibkr
  gate and need to be repaired.
- **"Live deployment gate: test coverage threshold"** is still an open
  architectural question. The 616-passing number is misleading; the
  correct denominator is 720 (or whatever the actual collect count is
  with deselects removed) and the 5 shadow failures are real.

### Hidden (Round 1 didn't see)
- **Runner scripts are Linux-pathed and the preflight swallows failures**
  (Pragmatist L1) — the system is silently no-op in paper-soak.
- **Journal-after-pipe is a NEW atomicity gap** (Critic L4) — the
  "INV-5 fix" moved the window, didn't close it.
- **`StageFallbacks` is not `frozen=True`** (Critic L7) — mutability
  creates test pollution.
- **`nyse_calendar.py` covers 2024–2028 only** (Critic L6) — 2029 cron
  misfires are pre-baked.
- **DeprecationWarning can be filtered** (Skeptic) — operators adding
  `filterwarnings("ignore", module="stockapp_v12")` silently defeat
  the migration signal.
- **`audit_wrapped_journal` silently disables on import error** (Critic
  surprise) — only a warning log indicates audit is off; no operator
  watches that line.

---

## Cross-voice consensus

1. **The library is in good shape; the operational wiring is not.** (All 4)
2. **The 5 shadow-gate failures are the actual live-promotion blocker.**
   (Skeptic + Pragmatist; Architect and Critic agree in the implied
   "whatever gates live_ibkr" framing.)
3. **The "wire it up" framing is stale.** (Skeptic + Architect explicitly;
   Pragmatist and Critic implicitly via the new categories of issues
   they surface.)
4. **L1 (Linux paths on Mac) is the highest-leverage next action.**
   (Pragmatist; Architect and Skeptic agree it should come first
   because nothing else matters if the system is silently no-op.)
5. **L4 (journal-after-pipe) is a fix-induced regression.** (Critic;
   Architect agrees it's worse than the original.)

---

## Open questions for the user

1. **Accept the L1–L4 priority?** (Recommend yes — 1.25 days unblocks everything.)
2. **L3 (1 day for shadow-gate fixes) is the live_ibkr gate. Should
   it run in parallel with L1–L4?** (Recommend yes — different code paths,
   different test files.)
3. **L8 (Decimal/float boundary) and L9 (Round-1 concurrency tests)
   are 2 days total. Sprint M candidate?** (Recommend yes.)
4. **Mirror this Round-2 synthesis to Obsidian `Council-Round-2-2026-07-10`?**
   (Recommend yes — matches Round-1 pattern.)

---

**Bottom line:** The library is ready; the system is not. Sprint L is
~5 days of focused work that closes the operational gaps Round 1 missed
and the fix-induced regressions Round 1 introduced. After Sprint L the
system is in a state where a real trader could evaluate `live_ibkr` with
trust.

---

## Post-review correction (2026-07-10)

After this council record was committed, the user clarified that the
**production deploy target is the NUC (Linux WSL on Windows)** at
`/home/app/Stockapp/`, not the Mac mini where unit tests are run. The
hardcoded `/home/app/Stockapp/...` paths in `scripts/_preflight.sh`,
`scripts/run_entry_window.sh`, and friends are **correct for the
production target**, not a bug.

**This invalidates L1 (Pragmatist's highest-priority finding) and the
"silently no-op in paper-soak" framing.** The Pragmatist subagent did
not have the deployment-topology context and assumed the scripts run on
the Mac mini because that's where the test environment is.

**Corrected L1 finding (2026-07-10):** The deployment topology is not
documented anywhere in the repo. There is no single canonical doc that
says "NUC is the production target; Mac mini is dev only; the runner
scripts are correctly pathed for the NUC." A Round-2 reader (human or
agent) must discover this from scattered `NUC` references in
`docs/architecture/*.md`, the `_preflight.sh` paths, and the
`nuc_post_migration_refresh.sh` script name.

**Action taken:** Created [`docs/DEPLOYMENT.md`](../../DEPLOYMENT.md) as
the canonical deployment reference. Updated `docs/SYSTEM_OVERVIEW.md:46`
to point to it (was: "single-host scheduler on Mac Mini" — wrong).
Added a header note to `docs/ENVIRONMENT.md` clarifying it is the
training-env inventory only, not the prod deploy doc. Added deployment
pointers to `README.md` and `docs/README.md`.

**Revised Sprint L priority** (L1 removed; L2–L9 unchanged):

| Pri | Item | Voice | Days | Note |
|-----|------|-------|------|------|
| ~~L1~~ | ~~Fix hardcoded Linux paths~~ | ~~Pragmatist~~ | ~~0.5~~ | **Invalidated** — paths are correct for the NUC deploy target |
| L2 | Wire `L2_DEGRADED` to `WebhookNotifier.notify_degraded()` | Pragmatist | 0.25 | Still valid |
| L3 | Repair 5 shadow-gate test failures | Pragmatist | 1.0 | Still valid; **now the highest-priority** item |
| L4 | Catch `JournalWriteError` at position-mutating paths | Critic | 0.5 | Still valid; P0-blocker |
| L5 | Re-run 92 deselected tests, retire the "pre-existing" label | Skeptic | 0.25 | Still valid; now also re-evaluates "what's actually broken on the NUC" |
| L6 | `nyse_calendar.py` extend to 2029+, parity check | Critic | 0.25 | Still valid |
| L7 | `StageFallbacks` → `frozen=True`; notifier timing doc | Critic | 0.25 | Still valid |
| L8 | Decimal/float boundary at API seams | Skeptic | 1.0 | Still valid |
| L9 | Original Round-1 concurrency test gaps | carried | 1.0 | Still valid |

**Revised total:** ~4.5 days (was 5; L1 removed). L3 (shadow-gate
fixes) is now the highest-leverage action; the next sprint should
start there, not at the (now-invalidated) L1.
