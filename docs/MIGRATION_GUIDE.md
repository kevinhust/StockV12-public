# V11 → V12 Migration Guide

> **Scope**: Phases, file mapping, rollback, acceptance tests only.  
> Product goals and architecture: [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md).

> **⚠ Path convention (post-migration):** File paths in the checklist
> tables below refer to V11-era locations (now under
> `archive/v11-legacy-2026-07-09/`). The V12 equivalents live under
> `src/stockapp_v12/`. For example, `src/services/position_manager.py`
> → `src/stockapp_v12/services/position_manager.py`.

---

## Prerequisites

- V11 frozen at `/V11/` (do not edit during V12 implementation)
- Separate Redis DB or `v12:` key prefix for parallel testing
- `paper_ibkr` validated before any live toggle

---

## Migration Phases

| Phase | Duration | Outcome | Doc reference |
|-------|----------|---------|---------------|
| **P1** Gatekeeper + frequency | ~1 day | ≤3 trades/day enforced; patrol/pattern off | [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md) |
| **P2** Position + brackets | 2–3 days | SL at entry, min hold, trail | [POSITION_LIFECYCLE.md](POSITION_LIFECYCLE.md) |
| **P3** SwingScanner | 2–3 days | Daily L1/L2 cadence | [TRADING_PIPELINE.md](TRADING_PIPELINE.md) |
| **P4** Cleanup + acceptance | ~1 day | Tests green, dead code archived | This file |

**Total**: ~6–8 engineering days.

---

## Phase 1 — Checklist

| # | Task | File(s) |
|---|------|---------|
| 1 | Disable pattern scheduler | `scripts/pattern_detection_scheduler.py` |
| 2 | Remove L3 forced patrol crons | `src/services/lifecycle_scheduler.py` |
| 3 | Implement R-05 daily trade counter | `src/core/risk_controller.py` |
| 4 | Implement R-06 cooldown | `src/core/risk_controller.py`, `position_manager` (stub OK) |
| 5 | Align R-01 to -2%, R-03 to 3%, R-04 to 40% | `risk_controller.py`, `config/risk_regimes.yaml` |
| 6 | Raise `MIN_POSITION_SIZE` to 0.4 | `src/services/fusion_engine.py` (interim) |

---

## Phase 2 — Checklist

| # | Task | File(s) |
|---|------|---------|
| 1 | Disable EOD liquidation jobs | `lifecycle_scheduler.py`, `trading_modes.yaml` |
| 2 | Add `BracketOrderService` | `src/core/execution/bracket_order.py` |
| 3 | Add `PositionManager` | `src/services/position_manager.py` |
| 4 | Add `DailyReview` | `scripts/daily_review.py` |
| 5 | Add `TradeJournal` | `src/services/trade_journal.py` |
| 6 | Wire R-10 min hold | `risk_controller.py` + PositionManager |

---

## Phase 3 — Checklist

| # | Task | File(s) |
|---|------|---------|
| 1 | Add `SwingScanner` | `src/services/selection/swing_scanner.py` |
| 2 | Update CatalystScorer (1×/day, expulsion) | `catalyst_scorer.py` |
| 3 | Add `config/swing_scanner.yaml` | new |
| 4 | Add `config/scheduler.yaml` | new |
| 5 | Replace intraday L1 cron with 16:30 | `lifecycle_scheduler.py` |

---

## Phase 4 — Checklist

| # | Task | Action |
|---|------|--------|
| 1 | Archive pattern scheduler, eod_closing, liquidation_service | `archive/legacy/v12-removed/` |
| 2 | Run full acceptance checklist (below) | pytest |
| 3 | 5-day paper soak | manual log |

---

## File Mapping

| V11 | V12 | Action |
|-----|-----|--------|
| `intraday_scanner.py` | `swing_scanner.py` | New |
| `catalyst_scorer.py` | same | Modify |
| `pattern_detection_scheduler.py` | — | Archive |
| `eod_closing.py` | `daily_review.py` | New |
| `liquidation_service.py` | — | Archive (PANIC only in review) |
| `risk_controller.py` | same | Modify |
| `ib_executor.py` | + `bracket_order.py` | Extend |
| — | `position_manager.py` | New |
| — | `trade_journal.py` | New |
| `scanner_config.yaml` | `swing_scanner.yaml` | New |
| `risk_regimes.yaml` | same | V12 `global_limits` section |
| — | `scheduler.yaml` | New |

---

## Rollback Procedure

1. Stop V12 scheduler and all `stock_*` workers.
2. Copy runtime code from `/V11/` over repo `src/`, `scripts/`, `config/` (exclude `.env`).
3. Flush **V12-only** Redis keys:

```
watchlist:tier1*  watchlist:tier2*  actionable:today
position:*        journal:*         risk:daily_trades:*
risk:cooldown:*   (or entire DB if dedicated v12 index)
```

4. Restart V11 `lifecycle_scheduler` + previous docker compose if used.
5. Verify: pattern/L3 jobs restored per V11 config; paper mode only.

**Do not** delete `/V11/` snapshot.

---

## Doc ↔ Test Traceability

| Doc section | Rule / ID | Test |
|-------------|-----------|------|
| [RISK_MANAGEMENT](RISK_MANAGEMENT.md) | R-01 | `v12/tests/test_r01_daily_loss.py` |
| [RISK_MANAGEMENT](RISK_MANAGEMENT.md) | R-05 | `v12/tests/test_r05_daily_cap.py` |
| [RISK_MANAGEMENT](RISK_MANAGEMENT.md) | R-06 | `v12/tests/test_r06_cooldown.py` |
| [RISK_MANAGEMENT](RISK_MANAGEMENT.md) | R-10 | `v12/tests/test_r10_min_hold.py` |
| [RISK_MANAGEMENT](RISK_MANAGEMENT.md) | Sizing no confidence in core | `v12/tests/test_sizing_no_confidence_in_core.py` |
| [POSITION_LIFECYCLE](POSITION_LIFECYCLE.md) | PL-2 trail ratchet | `v12/tests/test_trail_ratchet.py` |
| [POSITION_LIFECYCLE](POSITION_LIFECYCLE.md) | PL-4 cooldown | `v12/tests/test_cooldown_on_exit.py` |
| [TRADING_PIPELINE](TRADING_PIPELINE.md) | AnalystOutput schema | `v12/tests/test_analyst_output_schema.py` |
| [TRADING_PIPELINE](TRADING_PIPELINE.md) | INV-1 single eval | `v12/tests/test_inv1_single_eval_per_day.py` |
| [TRADING_PIPELINE](TRADING_PIPELINE.md) | Pre-eval empty set → 0 entries | `v12/tests/test_pre_eval_empty_set.py` _(renamed from `test_premarket_fail_closed` in hygiene-J)_ |
| [SCHEDULER](SCHEDULER.md) | Half-day window | `v12/tests/test_half_day_window.py` |
| [SCHEDULER](SCHEDULER.md) | Job registry | `v12/tests/test_job_registry.py` |

---

## Acceptance Checklist

Bind each item to a test before marking phase done.

### Risk (P1)

| ID | Criterion | Test file / name |
|----|-----------|------------------|
| RK-1 | 4th BUY denied (R-05) | `v12/tests/test_r05_daily_cap.py` |
| RK-2 | Re-entry within 48h denied (R-06) | `v12/tests/test_r06_cooldown.py` |
| RK-3 | Discretionary SELL day 0 blocked (R-10) | `v12/tests/test_r10_min_hold.py` |
| RK-4 | SL fill closes despite min hold | `v12/tests/test_sl_bypasses_min_hold.py` |
| RK-5 | Daily loss -2.1% blocks BUY (R-01) | `v12/tests/test_r01_daily_loss.py` |
| RK-6 | Registry params == loaded config | `v12/tests/test_config_registry.py` |
| RK-7 | Sizing: confidence not in core formula | `v12/tests/test_sizing_no_confidence_in_core.py` |

### Execution (P2)

| ID | Criterion | Test |
|----|-----------|------|
| EX-1 | Bracket creates parent + SL child | `test_bracket_submit_paper` |
| EX-2 | Trail ratchet never decreases | `test_trail_ratchet` |
| EX-3 | EOD jobs not scheduled | `test_scheduler_no_eod_liquidation` |

### Pipeline (P3)

| ID | Criterion | Test |
|----|-----------|------|
| PL-1 | tier1 size ≤ 20 | `test_tier1_cap` |
| PL-2 | tier2 expulsion rule | `test_tier2_expulsion` |
| PL-3 | Pre-eval empty set → 0 entries | `test_pre_eval_empty_set` _(was `test_premarket_fail_closed`; renamed in hygiene-J)_ |

### Scheduler

| ID | Criterion | Test |
|----|-----------|------|
| SCH-1 | Exactly 5 daily jobs + trail periodic | `test_job_registry` |
| SCH-2 | Half-day entry close 12:30 | `test_half_day_window` |

### Journal & Review (P2–P4)

| ID | Criterion | Test |
|----|-----------|------|
| JR-1 | Every entry has journal row | `test_journal_entry` |
| JR-2 | DailyReview sets HOLD/TRIM/EXIT | `test_daily_review_matrix` |

### Soak (P4)

| ID | Criterion | Method |
|----|-----------|--------|
| SOAK-1 | 5 trading days paper, no manual risk override | ops log |
| SOAK-2 | Avg new entries ≤ 3/day | `risk:daily_trades:*` |
| SOAK-3 | No V11 pattern events in logs | grep `events.pattern` |

---

## Definition of Done

Migration complete when:

1. All **RK**, **EX**, **PL**, **SCH**, **JR** tests pass in CI.
2. **SOAK-1..3** pass on `paper_ibkr`.
3. [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md) program-level Done criteria met.

---

## Out of Scope for This Guide

- Industry methodology references → [_reference/INDUSTRY_REFERENCES.md](_reference/INDUSTRY_REFERENCES.md)
- Module boundary definitions → [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md)
- Parameter semantics → [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md), [TRADING_PIPELINE.md](TRADING_PIPELINE.md)
