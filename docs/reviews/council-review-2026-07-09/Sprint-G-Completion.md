# Sprint G — Completion Record (2026-07-09)

**Goal:** Execute all 4 P1 items from the council review. After this sprint
the system has atomicity guarantees + missing R-rules wired with fail-closed posture.

## P1 commits (chronological)

| ID | Commit | What changed |
|---|---|---|
| P1-2 | `a0028a15` | R-06 `check_symbol_cooldown`: catch all `Exception` (was only `TypeError, ValueError`). Redis errors now block the trade instead of propagating uncaught. |
| P1-1 | `6c5e02c4` | `entry_guard.begin_entry_evaluation`: atomic Lua `SADD+EXPIRE` via `EVAL`. Eliminates check-then-act race where two concurrent orchestrators could both pass INV-1. Includes concurrency regression test with barrier-synchronized threads. |
| P1-7 | `6f03c370` | `TradeJournal.append`: `strict=True` raises `JournalWriteError` on Redis failure. `log_allow`/`log_buy` strict (post-execution). `log_pass`/`log_deny` lenient (pre-execution). `stages.py` catches `JournalWriteError` from `log_allow` → DENY; from `log_buy` → CRITICAL log + BUY (position already live). |
| P1-9 | `6b0d1413` | Three new red-line checks: R-01 `check_daily_loss_limit` (reads `risk:daily_pnl:{date}`), R-02 `check_drawdown_limit` (reads `risk:peak_equity` + `risk:current_equity`), R-09 `check_entry_window` (pure clock: 09:35–15:30 ET Mon-Fri). All fail-closed on Redis errors. |

## Tests added (44 new)

| Test file | Tests |
|---|---|
| `test_p1_2_r06_fail_closed.py` | 7 |
| `test_p1_1_atomic_entry_guard.py` | 7 |
| `test_p1_7_journal_fail_closed.py` | 9 |
| `test_p1_9_red_line_rules.py` | 21 |

## Full suite: 526 passed (+44 from Sprint F baseline of 482)

## Sprint H (next)

P1-3 (process_pending_action reconcile), P1-4 (Decimal precision), P1-6 (audit_ledger fsync), P1-8 (bars sort assertion).
