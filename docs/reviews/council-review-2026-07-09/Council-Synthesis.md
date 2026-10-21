# V12 Pipeline Council Review — Synthesis Report

> **Date:** 2026-07-09
> **Reviewers:** Architect · Engineer · Risk/Ops · Trader (4 voices, parallel)
> **Scope:** Full V12 pipeline (16 modules × 6 stages) + entry cron wiring + decision explainability
> **Method:** Each voice audited source files end-to-end against spec docs; this synthesis merges.

---

## TL;DR Verdict

| Voice | Verdict |
|---|---|
| Architect | **Acceptable with concerns** — boundaries mostly clean; rule-code labels are wrong; circular-import workaround. |
| Engineer | **REQUIRES WORK** — 2 P0 bugs (universe truncation + executor leak); 5 P1 atomicity/concurrency. |
| Risk/Ops | **Conditionally unsafe** — 5 of 10 R-rules have no runtime enforcement; entry cron doesn't exist. |
| Trader | **Would not deploy with real capital yet** — Catalyst stage is a stub; live path reads removed key; regime hardcoded. |

**Consolidated verdict:** The V12 *modules* are individually well-designed and tested. The V12 *system* is not wired together: there is no production entry path, R-01/02/08/09 have no runtime check, the live scheduler hardcodes `market_regime="BULL_LONG"` and reads a removed Redis key, and the swing scanner silently truncates the universe to 10 hardcoded names. **Not safe for live_ibkr today. Safe for paper-soak once P0-1 through P0-5 below are fixed.**

---

## P0 — must fix before any live deployment (5 items)

### P0-1. Swing scanner silently truncates universe to 10 names
**File:** `src/stockapp_v12/services/swing_scanner.py:38-39`
```python
_HERE = Path(__file__).resolve()
_V12_ROOT = _HERE.parent.parent.parent        # = <repo>/src
_REPO_ROOT = _V12_ROOT.parent.parent          # = /Users/dev  (WRONG — should be repo root)
```
**Symptom:** `_REPO_ROOT` resolves to `$HOME`, not `/Users/dev/Stockapp`. Consequences:
- `swing_scanner.yaml` is read from `/Users/dev/config/swing_scanner.yaml` → missing → falls back to embedded defaults (matches spec by coincidence).
- `universe.yaml` is read from same wrong path → missing → falls back to **10 hardcoded names** (`AAPL, MSFT, NVDA, AMZN, META, GOOGL, TSLA, AMD, AVGO, SMCI`).
- `sys.path.insert(0, "/Users/dev")` pollutes `$HOME` at module import.
**Impact:** All production scans evaluate only 10 tickers. Zero market coverage. Lost alpha.
**Fix:**
```python
from stockapp_v12.core.paths import REPO_ROOT as _REPO_ROOT
```
Then drop the `sys.path.insert(0, str(_REPO_ROOT))` (only `conftest.py` should manage sys.path).
**Severity:** P0 monetary bug.

### P0-2. ThreadPoolExecutor never shut down on process exit
**File:** `src/stockapp_v12/notifiers/webhook_notifier.py:161-175`
The async webhook executor is created in `__init__` but no `atexit.register` or signal handler calls `shutdown(wait=True)`. On graceful cron SIGTERM, in-flight `notify_order_submitted` posts are killed mid-flight. Also: `flush()` (line 167) recreates the executor without a lock → race between `submit` and the new assignment.
**Impact:** Lost notifications, orphan TCP sockets, possible `RuntimeError` on flush.
**Fix:**
```python
import atexit
atexit.register(self._executor.shutdown, wait=True)
# + threading.Lock around submit() and flush()
```
**Severity:** P0 resource leak + delivery-lost.

### P0-3. No production entry path wired
**Files:** `scripts/install_pipeline_cron.sh`, `archive/v11-legacy-2026-07-09/src/services/lifecycle_scheduler.py:615,771`
The live V12 lifecycle scheduler reads Redis LIST `actionable:today` at line 615 — a key that **was explicitly removed in hygiene-I** (see MIGRATION_DEBT). It hardcodes `market_regime="BULL_LONG"` at line 771. Result: **live system logs "no actionable symbols today" and exits without ever calling `EntryOrchestrator.evaluate_and_maybe_enter`**. The "entry" cron doesn't exist in `install_pipeline_cron.sh`.
**Impact:** Zero entries per day in production. No signal to operator (because the veto counter is incremented inside the orchestrator, which never runs).
**Fix (full wiring needed):**
1. Replace Redis LIST read with in-process `ctx.actionable_symbols` populated from `watchlist:tier2`.
2. Replace `market_regime="BULL_LONG"` with read from `market_regime:current` Redis key.
3. Add `run_entry_window.sh` to `install_pipeline_cron.sh` between `09:35` and `15:30` ET.
**Severity:** P0 — entire entry path is dead in production.

### P0-4. R-05 daily trade counter never incremented in production
**File:** `src/stockapp_v12/bridge/gatekeeper.py:125-132` (defines `increment_daily_trade_count`)
**Call sites:** Only `scripts/backtest_v12_paper.py:779`. **Never called in production entry path.**
**Impact:** R-05 cap of 3 entries/day is silently bypassed; an entry loop bug could fire dozens of trades.
**Fix:** In `pipeline/stages.py:run_stage_7_register` (after `pm.register` succeeds), call `increment_daily_trade_count(redis_client, mode)` (with the same fail-closed posture as H1's `get_daily_trade_count`).
**Severity:** P0 risk control gap.

### P0-5. Catalyst stage is a stub; live path reads removed key
**Files:** `scripts/run_catalyst_scorer.sh:1-3` (self-declares STUB); `archive/v11-legacy-2026-07-09/src/services/lifecycle_scheduler.py:615`
The spec says SCAN → CATALYST → PRE-EVAL → ENTRY. CATALYST is shell-stub. PRE-EVAL reads a removed key. End-to-end: SCAN writes tier1 → stub exits 0 → tier2 is empty → live entry sees `actionable_raw = []` → returns. **0 entries, 0 events, 0 trades, no operator signal.**
**Fix (choose one):**
- (a) Implement `services/catalyst_scorer.py` per spec (`docs/TRADING_PIPELINE.md` Stage 2, weights 0.40/0.35/0.15/0.10), or
- (b) Have `run_catalyst_scorer.sh` write `watchlist:tier2 = watchlist:tier1` passthrough and log `L2_DEGRADED` (5 lines of shell) — accepts that catalyst filtering is deferred, but unblocks the rest of the pipeline.
**Severity:** P0 system-deadlock.

---

## P1 — fix in current sprint (10 items)

| ID | File:Line | Issue | Fix |
|---|---|---|---|
| P1-1 | `pipeline/entry_guard.py:65-71` | `should_skip_entry` + `mark_evaluated` is a check-then-act race. Two concurrent orchestrators can both pass INV-1. | Collapse into a single Lua script `EVAL` doing `SADD` + `EXPIRE` and returning `(added, reason)`. |
| P1-2 | `bridge/gatekeeper.py:142-148` | R-06 `check_symbol_cooldown` is **fail-open** on Redis errors (catches only `TypeError, ValueError`). Inconsistent with H1's R-05 fail-closed posture. | Mirror H1 pattern: catch all `Exception`, treat as in-cooldown. |
| P1-3 | `services/position_manager.py:428-466` | `process_pending_action` is destructive (closes/trims position) and runs unconditionally in stage_1 of every entry eval. TRIM path mutates `sl_price` locally without broker reconcile. | Wrap call in try/except; on broker-mutate paths, require a reconcile ack before applying local mutation. |
| P1-4 | `services/daily_review.py:78-79` + `position_manager.py:189-203` | Float math for `pnl_atr`/`unrealized` and `round(x, 2)`. Spec says use Decimal (`core/precision.py`). | Use `round_pct`/`round_dollar` from `core.precision`. |
| P1-5 | `bridge/live_guard.py:170,186` | Rule code labels are **wrong**: code emits `R-02_SINGLE_CAP` (spec's R-02 is drawdown; this is R-03) and `R-08_EXPOSURE_CAP` (spec's R-08 is correlation; this is R-04). Operators reading journal see "R-02 fired" and look up wrong rule. | Rename to `R-03_SINGLE_CAP` and `R-04_EXPOSURE_CAP`. Add migration note in journal envelope. |
| P1-6 | `audit/audit_ledger.py:99-101` | `fh.write` is buffered; no `flush()` / `fsync()` after each write. Docstring claims "atomicity via single write+flush" — code doesn't deliver. | Add `fh.flush(); os.fsync(fh.fileno())`. Better: cache file handle across calls. |
| P1-7 | `pipeline/journal.py:62-68` | `TradeJournal.append` fails OPEN on Redis errors — every decision silently drops from the audit trail. INV-5 violation. | Raise (or return False) on Redis failure; have `stages.py` short-circuit to system-halt. |
| P1-8 | `pipeline/stages.py:454-464` `extract_price_atr` | `iloc[-1]` assumes bars are sorted ascending by date. No assertion. If caller passes unsorted, wrong day's close feeds SL math. | Add `assert ctx.bars.index.is_monotonic_increasing` at entry. |
| P1-9 | `bridge/gatekeeper.py:26-32` | R-01 (daily loss), R-02 (drawdown), R-09 (entry window) have **zero runtime enforcement** in V12. `red_line_thresholds()` is defined but never called. | Implement fail-closed `check_daily_loss_limit`, `check_drawdown_limit`, `check_entry_window`. Mirror H1. |
| P1-10 | `scripts/install_pipeline_cron.sh` | No NYSE calendar check (cron is `* * 1-5`). On holidays jobs will fire. No half-day detection (closing at 13:00 ET). | Wrap each cron with a Python `exchange_calendars.XNYS.is_session(date)` check. Set half-day entry close to 12:30 when applicable. |

---

## P2 — clean-up (8 items, grouped)

### Atomicity & concurrency
- `services/position_manager.py:297-333` `_close_position` makes 3 separate Redis calls without pipeline. Crash mid-call leaves index out of sync with record + cooldown. → Wrap in `pipeline()` for atomicity.
- `notifiers/webhook_notifier.py:167-176` `flush()` race: recreate executor before old is shut down. → Use lock.

### Spec conformance
- `services/daily_review.py:58` `apply_immediately=True` default violates spec ("execution next session via pending_action"). → Set default to `False`; add `force_immediately=False` opt-in for paper-soak.
- `pipeline/stages.py:50-53` `FALLBACK_ANALYST_CONFIDENCE=0.75` and `FALLBACK_ATR_PCT=0.05` are hardcoded. → Move to `core/limits.py` or `SignalRuleConfig`.
- `pipeline/stages.py:405` `catalyst_type=(analyst.thesis_tags[0] if analyst else None)` silently aliases catalyst type. → Either add `catalyst_type` field to `AnalystOutput` or document the alias contract.

### Module boundaries
- `services/notification_service.py` is a re-export shim (the real implementations are in `notifiers/`). Module map says B7 (NotificationService) lives in `services/`. → Either move shim out of `services/` or rename to `services/_notifier_compat.py` to make the boundary explicit.
- `services/entry_orchestrator.py:174` locally imports `pipeline.stages` to break a circular import. → Move `EntryContext`/`EntryResult` to a `pipeline.types` module.
- `services/forecast_service.py:52-65` `from services.chronos_service import` uses bare `services.*`. → Resolve via `archive_root / "src/services/chronos_service.py"` using `_v11_compat.ensure_v11_archive_on_path()`.

### Exit audit completeness
- `services/position_manager.py:299-323` `_close_position` writes `extra={"days_held", "entry_price"}` only. Spec requires `mfe_pct`, `mae_pct`, `pnl_pct`, `regime_at_entry`, `regime_at_exit`. → Track `mfe_pct`/`mae_pct` in `update_trail_on_tick` running max/min; call `journal.log_exit()` (already exists in `pipeline/journal.py:195-224`) instead of raw `append`.

---

## What is GOOD (preserve these)

1. **Risk-based sizing with Decimal precision (Phase A5).** `core/sizing.py:50-110` enforces spec exactly: `confidence` does NOT enter the core formula; `regime_multiplier` is the only multiplicative knob; post-quantize R-03 cap prevents 0.0299999 → 0.03 rounding past cap. Tested in `tests/test_decimal_precision.py`, `tests/test_sizing_no_confidence_in_core.py`.
2. **Trail ratchet is genuinely ratchet-only.** `services/position_manager.py:277-284` only updates `trail_price` when `new_trail > rec.trail_price`. Never loosens.
3. **R-05 fail-closed on Redis outage (hygiene-D H1).** `bridge/gatekeeper.py:99-122` returns `cap` (not 0) on Redis exception, correctly blocking all new buys until the counter is readable again. Pinned by `tests/test_fail_closed_safety.py:28-58`. **This is the model to copy for R-01/02/08/09 wiring.**
4. **INV-1 single-eval-per-day.** `pipeline/entry_guard.py:65-71` SADD + 48h TTL on first eval; second call short-circuits with `INV-1_ALREADY_EVALUATED`. Tested.
5. **7-stage EntryOrchestrator decomposition.** `pipeline/stages.py:60-466` is cleanly testable, mirrors the pre-B3 monolith line-for-line, and survives 47 stage tests.
6. **SR-07 regime gate fail-closed posture.** `signal_rule_engine.py:195-225` returns PASS-safe rather than proceed-without-context.
7. **TradeJournal atomicity (PR-A fix).** `pipeline/journal.py:62-66` uses `RPUSH + EXPIRE`. MD-01 closed.
8. **`_AuditJournalProxy` whitelist (PR-A fix).** `KNOWN_JOURNAL_METHODS` rejects unknown attribute access. MD-02 closed.

---

## Recommended Action Plan (5 sprint slices)

| Sprint | Items | Themes | Target state |
|---|---|---|---|
| **Sprint F (this week)** | P0-1, P0-2, P0-3, P0-4, P0-5 | Wire entry + fix universe + executor | System can actually trade in paper. |
| **Sprint G** | P1-1, P1-2, P1-7, P1-9 | Atomicity + missing R-rules | R-01/02/06/09 wired with fail-closed posture. |
| **Sprint H** | P1-3, P1-4, P1-6, P1-8 | Decimal discipline + persistence durability + trail correctness | No float math on capital; broker reconcile; bars sort invariant. |
| **Sprint I** | P1-5, P1-10, P2-exit-audit | Spec conformance + ops | Correct rule codes; NYSE calendar; MFE/MAE on every close. |
| **Sprint J** | Remaining P2s | Module boundaries + dead code cleanup | No more circular workarounds, no bare sys.path insertion, `notification_service.py` shim flagged. |

---

## Test Gap Summary (from Engineer + Risk/Ops voices)

The 11 pre-existing test failures are migration debt / fixture issues, not product regressions:

- `test_writer_contract.py` — fixed by PR-D (D1).
- `test_daily_review_chain.py` — fixed by PR-D (D3).
- `test_min_hold_blocks_early_exit` — test bug: uses `min_hold_days=1` but clock is 2.20 days ahead. Fix: inject `now_provider` or `limits=…(min_hold_days=5)`.
- `test_paper_soak_sign_agreement.py` — test data dated 2026-06-29 is 10 days out of fixture window as of 2026-07-09. Refresh fixture dates.
- `test_shadow_ensemble.py::test_constants_match_ensemble_backend` and `test_shadow_trade.py` — import from `v12.src.…` (pre-migration path). Fix imports.
- `test_fail_closed_safety.py::test_forecast_service_chronos_init_failure_logs_warning` — uses bare `from services.chronos_service import`. Conftest sys.path issue (MD-18).

**None of the 11 indicate V12 product regressions.** They are fixture/import cleanup.

Real test gaps to add (none exist today):
- `position_manager.process_pending_action` two-thread contention
- `entry_guard` Lua atomicity under 8 concurrent threads
- `webhook_notifier.flush()` race
- `_close_position` MFE/MAE invariants

---

## Cross-Voice Consensus (where all 4 reviewers agree)

1. **The system is not wired for production** — Trader/Risk/Ops/Engineer all flag.
2. **The 11 pre-existing test failures are not real bugs** — Engineer + Risk/Ops both flag.
3. **Decimal discipline in sizing is correct** — Architect + Engineer + Trader all approve.
4. **The 7-stage decomposition is the right shape** — Architect + Engineer + Trader all approve.
5. **The trail ratchet logic is correct** — Engineer + Trader + Architect all approve.
6. **The H1 fail-closed R-05 pattern should be the model for R-01/02/08/09** — Risk/Ops + Engineer + Architect all recommend.

---

## Open architectural questions (need product owner decision)

1. **Catalyst stage:** implement full LLM catalyst scoring (~2 weeks) **or** passthrough stub (5 lines, unblocks everything)?
2. **DailyReview mode:** strict pending-action queue (spec-compliant) **or** keep `apply_immediately=True` paper-soak convenience?
3. **Outcome journal envelope:** want every CLOSED record to carry `mfe_pct`/`mae_pct`/`pnl_pct`/`regime_at_exit`? If yes, backend must persist per-tick price series.
4. **Rule code labels:** rename `R-02_SINGLE_CAP` → `R-03_SINGLE_CAP` etc. — accept the journal backward-compat note?
5. **Live deployment gate:** what test coverage threshold for promoting `live_ibkr`? (Current is 527 passing / 11 failing / 5 skipped / 7 xfailed.)

---

**Bottom line:** V12 has the architecture and most of the implementation right. It needs two things in priority order:
1. **Wire it up** (Sprint F — the 5 P0 items, particularly the entry cron + universe fix + R-05 increment).
2. **Fill in the R-rule enforcement + atomicity gaps** (Sprints G/H/I).

Once those land, this is a system a real trader could trust with capital.
