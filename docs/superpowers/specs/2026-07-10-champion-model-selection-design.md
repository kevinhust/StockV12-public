# Champion Model Selection — Design

**Date**: 2026-07-10
**Topic**: Topic 1 of 3 in parallel-model portfolio exploration
**Status**: Draft for user review
**Related commits**: e4d79035, 8de356b2 (portfolio expansion), 10a2103f, fb5da3f6 (HaltGate Round 4)

## Context

Round 4 council expanded the shadow model portfolio from 2 to 9 models. The
30-day backfill results (2026-05-19 → 2026-06-29, 10 symbols, threshold=0.5%)
show dramatic PnL dispersion:

| Rank | Model | PnL% | Sharpe (approx) | Type |
|------|-------|------|------------------|------|
| 1 | E07_long_ctx (ctx=1024) | +296.09% | ~3.5 | Hyperparameter variant |
| 2 | zeroshot_bolt_small | +189.25% | ~2.5 | Baseline |
| 3 | E08_fewer_steps (100 steps) | +161.95% | ~2.3 | Hyperparameter variant |
| 4 | E09_higher_lr (5e-5) | +148.04% | ~2.2 | Hyperparameter variant |
| 5 | E05_p3_coldstart_seed7 (retrained) | +147.15% | ~2.4 | Retrained |
| 6 | E10_more_steps (500 steps) | +84.26% | ~1.5 | Hyperparameter variant |
| 7 | phase2_v2 | +46.11% | ~1.0 | Original |
| 8 | E06_short_ctx (ctx=256) | +5.17% | ~0.3 | Hyperparameter variant |
| 9 | E05_p3_coldstart_seed42 | -18.45% | ~-0.3 | Original |

The current production cron runs `phase2_v2 + E05_p3_coldstart_seed42`. The
worst model in the pool is in production. This design defines how to pick
and operate a single champion model going forward.

## Goals

1. Promote one champion model to replace the current 2-model production.
2. Operate champion + challenger pair for ongoing validation.
3. Use strict multi-window + statistical significance criteria.
4. Quarterly re-evaluation with real-time HaltGate kill switch.

## Non-Goals

- Ensemble methods (combination voting or weighted averaging).
- Dynamic per-regime model switching.
- Automated promotion without human approval.
- Modifying the HaltGate paper-trade mechanism itself (already shipped
  in commits 10a2103f and fb5da3f6).

## Architecture

### Roles

```
[ Champion ]   ──> participates in trading decisions (signal generation)
[ Challenger ] ──> OOS-only data collection, no decision impact
[ Archived ×7 ] ──> on disk only, quarterly pool for re-evaluation
```

**Invariants**:
- Exactly one champion at any time.
- Challenger must have ≥30 days of OOS data before becoming eligible.
- Archived models are never deleted; they remain on disk for future
  reactivation.

### Daily Pipeline (unchanged schedule, role-augmented)

```
16:05 ET  shadow_cron.sh     predict for {CHAMPION, CHALLENGER}
                              write per-date JSON to _shadow/<model>/<date>.json
16:10 ET  shadow_ensemble.sh read CHAMPION JSONs only, produce trading signal
16:15 ET  daily_review.sh    (unchanged)
16:20 ET  shadow_halt.sh     monitor CHAMPION 3-day rolling accuracy
                              if halt triggered → swap CHAMPION ↔ CHALLENGER
```

### Quarterly Re-evaluation (every 60 trading days, manual trigger)

1. Run `scripts/champion_evaluation.py --report`.
2. Output: leaderboard of all models (champion, challenger, archived)
   with PnL, Sharpe, MASE, win-rate, cross-period consistency.
3. User reviews and selects new champion + challenger.
4. Update `CHAMPION_CSV` / `CHALLENGER_CSV` env vars and write
   `_shadow/_state/champion_config.json`.

## Selection Criteria

### Champion Promotion Gate

All conditions must hold for a model to become champion:

| Metric | Threshold | Window |
|--------|-----------|--------|
| PnL (cumulative) | > 0 | 30-day backfill |
| Sharpe ratio (daily, not annualized) | ≥ 1.0 | 30-day backfill |
| Sharpe ratio (daily) | ≥ 0.5 | 7-day live cron (latest) |
| Win rate | ≥ 55% | 30-day backfill |
| Cross-period sign consistency | PnL sign same in both windows | backfill AND live |
| MASE | ≤ 2.1181 (ADR-008) | 30-day backfill |
| n_dates | ≥ 30 | 30-day backfill |
| n_trades | ≥ 150 | 30-day backfill |

**Sharpe definition**: `mean(daily_trade_pnl_pct) / std(daily_trade_pnl_pct)`,
where `daily_trade_pnl_pct` is the sum of per-trade PnL (sign × actual_ret)
for each (model, date) pair, in percent. Not annualized; not risk-free-rate
adjusted (cash model).

### Challenger Selection

- Rank non-champion models by 30-day Sharpe.
- Select the model with the second-highest Sharpe.
- Tiebreaker: prefer low correlation with champion (different
  hyperparameter family preferred over same family).
- Challenger does NOT need live cron data — it accumulates naturally.

### HaltGate Behavior

- Existing HaltGate (commit 10a2103f) monitors the **champion** only.
- 3-day rolling accuracy < 40% → halt flag fires.
- On halt:
  1. Write `halt_swap.json` indicating champion→challenger swap.
  2. Next shadow_cron reads `CHAMPION=` from env / config (latest swap).
  3. shadow_ensemble.sh begins using challenger JSONs.
- Recovery: champion accuracy ≥ 50% does NOT auto-restore — wait for
  next quarterly review.

### Archive Policy

- Archived models are removed from `MODELS_CSV`.
- Files on disk retained indefinitely:
  - `models/chronos/<name>/`
  - `_shadow/<name>/`
- Re-activation: quarterly review can promote any archived model back
  into the active pool if it shows improved OOS.

## Data Model

### Shadow JSON (existing, no changes)

```json
{
  "model": "E07_long_ctx",
  "eval_date": "2026-07-09",
  "rows": [
    {
      "symbol": "AAPL",
      "forecast_ret_pct": 1.234,
      "actual_ret_pct": 0.567,
      "eval_source": "cron" | "backfill"
    }
  ],
  "source": "cron" | "backfill"
}
```

### Halt Swap File (new)

```json
// _shadow/_state/halt_swap.json
{
  "active_champion": "zeroshot_bolt_small",
  "halted_champion": "E07_long_ctx",
  "swapped_at": "2026-07-15T16:20:00-04:00",
  "reason": "halt_triggered",
  "halt_3day_accuracy": 0.38
}
```

`active_champion` = the model that should drive trading decisions
after this swap (i.e. the previously-challenger that just took over).
`halted_champion` = the model that was halted and demoted to challenger.

### Champion Config (new)

```json
// _shadow/_state/champion_config.json
{
  "champion": "E07_long_ctx",
  "challenger": "zeroshot_bolt_small",
  "promoted_at": "2026-07-10",
  "promotion_rationale": "Sharpe=3.5, all 8 gates passed",
  "next_review_date": "2026-09-25"  // ~60 trading days later
}
```

## Components

### 1. `champion_selector.py` (new)

Pure-function library. Given a list of per-model PnL/Sharpe metrics,
return the champion and challenger candidates. Stateless, no I/O.

```python
def select_champion(metrics: list[ModelMetrics]) -> ChampionSelection:
    """Apply the 8-gate champion promotion criteria. Return ranked candidates."""

def select_challenger(
    metrics: list[ModelMetrics],
    champion: str,
) -> str:
    """Pick the second-highest Sharpe model with low correlation preference."""
```

### 2. `champion_evaluation.py` (new CLI script)

Reads `_shadow/<model>/*.json` for all models, computes the 8 metrics,
prints leaderboard table. Triggered manually at quarterly review.

```bash
python scripts/champion_evaluation.py --report
python scripts/champion_evaluation.py --champion E07_long_ctx
```

### 3. Halt Swap Mechanism (extends existing `shadow_halt.sh`)

- After writing `halt_decisions.jsonl`, check if halt fired.
- If fired AND champion matches halted model → write `halt_swap.json`.
- `run_shadow_cron.sh` reads `halt_swap.json` BEFORE setting
  `CHAMPION=` env var; the latest swap wins.

### 4. `MODELS_CSV` Update

Replace space-separated model list with role-tagged format:

```bash
# Old: CHAMPION_CHALLENGER_CSV="E07_long_ctx zeroshot_bolt_small"
# New:
CHAMPION_CSV="${CHAMPION_CSV:-E07_long_ctx}"        # decision-making
CHALLENGER_CSV="${CHALLENGER_CSV:-zeroshot_bolt_small}"  # OOS only
```

`run_shadow_cron.sh` runs both, but `run_shadow_ensemble.sh` only
consumes champion JSONs.

## Testing

| Layer | Test |
|-------|------|
| Unit | `test_champion_selector.py` — 8-gate logic, challenger tiebreaker, edge cases |
| Unit | `test_halt_swap.py` — swap file write/read, swap precedence |
| Integration | `test_champion_evaluation.py` — reads real shadow JSONs, computes metrics |
| Integration | `test_shadow_cron_role.py` — verifies ensemble only uses champion |
| End-to-end | Synthetic 60-day data → run evaluation → assert promotion decision |

## Migration Plan

1. **Day 0 (today)**: Pick initial champion from current backfill
   (E07_long_ctx, +296%, Sharpe ~3.5).
2. **Day 0**: Pick initial challenger (zeroshot_bolt_small, +189%,
   different family = low correlation).
3. **Day 0**: Archive the remaining 7 models (remove from
   `CHAMPION_CSV` + `CHALLENGER_CSV`, keep on disk).
4. **Day 0+1**: Verify cron uses champion for ensemble signals.
5. **Day 60 (Q1 close)**: First quarterly review.

## Risks

| Risk | Mitigation |
|------|------------|
| 30-day window is short — +296% PnL may not generalize | Strict multi-window criteria + 60-day quarterly review |
| HaltGate swap creates audit gaps | HaltSwapFile timestamps every swap; quarterly review inspects |
| Champion overfits to recent regime | Challenger continues OOS to detect drift |
| ZeroShot ranks #2 — may indicate training regime shift | Quarterly review re-evaluates with fresh data |
| Manual quarterly review is slow / forgotten | Add day-55 cron reminder writing to `/home/app/Stockapp/logs/quarterly_review_reminder.txt` (sister-script reviewed by user, no email infra) |

## Open Questions

- (none — all key decisions resolved via clarifying questions)

## Success Criteria

- Champion model produces positive daily OOS PnL for ≥45 of next 60 trading days.
- HaltGate swap rate < 2 per quarter (allow some natural variation; avoid both too-fragile and too-stuck champions).
- Quarterly review completed within 5 trading days of trigger.
- Zero silent champion changes (all swaps logged in halt_swap.json).