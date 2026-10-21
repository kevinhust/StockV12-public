# v3 Lessons Learned + Empirical Exploration Plan (Stage 2.x)

> **Purpose:** Capture v3 retrain lessons and define Stage 2.x — a small-step,
> many-experiment exploration that replaces the "single 1M-step run" pattern
> with empirical breadth + cheap validation gates.
> **Author:** Grok (Council-aligned)
> **Date:** 2026-06-29
> **Status:** ⏳ Round 1 design complete, runner + configs in progress.
> **Related docs:** [V3_POSTMORTEM_AND_TRAINING_PLAN.md](V3_POSTMORTEM_AND_TRAINING_PLAN.md) ·
> [H2H_V3_REPORT.md](H2H_V3_REPORT.md)

---

## Part 1 — v3 lessons (the foundation for Round 1 design)

The 1M-step chunked retrain (`phase2_v3`) failed as a promotion candidate but
yielded 4 operational lessons that drive the new design.

### Lesson 1 — In-sample MASE is leaky. OOS H2H is the only truth.

AutoGluon's `eval_metric` validation is computed on the **last
`prediction_length=10` days of the training data** — an in-sample holdout by
construction. During v3 training, in-sample `-MASE` improved monotonically
(-13.17 → -14.20 over 100 chunks), but OOS MASE got *worse* (2.5152 for v2 vs
2.65–2.69 for v3 ckpts on the same 10-symbol panel).

**Design implication:** every experiment in Round 1 must end with an OOS H2H
eval. In-sample numbers are recorded but **never** used to decide promotion.

### Lesson 2 — Long warm-init training memorizes noise. Bold ≠ right.

v3 ckpts produced larger-magnitude forecasts (mean |forecast| = 1.4-1.5% vs
v2's 0.58% at 20d cutoff) but had *worse* hit_rate on the same forecasts.
"More confident, less right" — classic overfit pattern, but we couldn't
see it until 16 GPU hours had been spent.

**Design implication:** Round 1 experiments use short training budgets
(≤ 5k step, ~3-5 min GPU) so overfit manifests fast and cheaply.

### Lesson 3 — Variance / sample noise is unmeasured.

We never ran v2 with multiple seeds, so we don't know if v2's OOS hit_rate
of 0.30 is a real signal or sampling artifact on a 10-symbol panel. Without
a noise floor, we can't tell if E04's hit_rate of 0.45 is meaningfully
different from v2.

**Design implication:** E01 (5-seed v2 baseline) is the **first**
experiment — it sets the noise floor every other experiment compares
against. If std(hit_rate) across seeds is > 0.10, then 10-symbol panel is
too small to draw conclusions and we should expand to 50 syms in Round 2.

### Lesson 4 — The right question is "backbone", not "training".

The most uncomfortable v3 finding is that **v2 itself fails the 0.53
hit_rate gate**. No amount of training data, steps, or hyperparameter
tuning fixes a backbone that wasn't designed for sign correctness (Chronos
is quantile regression, optimized for accurate point/interval forecasts,
not directional calls).

**Design implication:** Round 1 includes E07 (classification head — predict
sign class directly) and E08 (regime-conditioned fine-tune) which are
backbone-level swaps. If E01-E06 all cluster around 0.30 ± noise, that's
confirmation that **the bottleneck is the backbone**, not the training
recipe, and Round 2 should focus on E07-style changes.

### Lesson 5 — No tooling for "many small experiments".

v3 was a single bespoke 16-hour run with no parallel comparison. The repo
had no infrastructure to run N small experiments, compare results, and
make a promotion decision.

**Design implication:** Stage 2.x ships `v12/scripts/run_experiment.py` —
a CLI + YAML-config runner that automates: train → H2H smoke → H2H full →
JSON report → index update. Each experiment is one YAML file; the runner
is the same for all 8. This makes Round 2 (and any future exploration)
mechanical, not bespoke.

---

## Part 2 — Round 1 design (the 8 experiments)

### What we're measuring

For each experiment, we record 5 numbers:

| Metric | What it tells us |
|---|---|
| OOS MASE | Precision (v2 baseline: 2.5152) |
| OOS hit_rate | Sign correctness (v2 baseline: 0.30) |
| OOS signal_hit_rate | Directional edge when forecast is non-flat (v2 baseline: 0.31) |
| Train time | Cost in GPU minutes |
| Reproducibility (E01 only) | Seed-to-seed std of hit_rate — establishes noise floor |

### The 8 experiments

| ID | Name | Single-line description |
|---|---|---|
| E01 | 5-seed v2 baseline | Re-run v2 (1k step, full corpus) with 5 seeds to establish hit_rate noise floor |
| E02 | v2 @ 5k step | Same recipe as v2 but 5x training; check if more steps helps or hurts |
| E03 | v2 @ 1k step + LR/10 | Same step count, 10x lower LR; check if slow learning is more stable |
| E04 | warm-init from v2 + 1k step | Fresh warm-init fine-tune (the v3 idea but 100x shorter); does the v3 warm-init idea even work at 1k? |
| E05 | context_length=512 | Halve Chronos context window; reduce noise from distant history |
| E06 | feature add: volume-norm returns | Add volume-normalized returns as feature; current Chronos only sees close |
| E07 | classification head | Replace quantile regression with sign classification; biggest backbone-level change |
| E08 | regime-conditioned (BEAR-only fine-tune) | Fine-tune only on BEAR-regime training data; regime-specific model |

### Stage gates per experiment

```
After 5k-step training:
  1. Run H2H smoke (3 sym × 1 cutoff, ~5 min) — cheap validation
     ├── smoke fails or numbers clearly worse → archive + skip full H2H
     └── smoke pass + numbers interesting → continue
  2. Run H2H full (10 sym × 2 cutoff, ~30 min) — rigorous validation
     └── write report JSON + 1-line MD summary
  3. Update experiments/index.json (the dashboard)
```

**Promotion gate to Round 2:** any experiment with `hit_rate > 0.55`
(v2 baseline 0.30 + 0.25 margin) is flagged as a Round 2 candidate.

### Resource budget

| Phase | GPU minutes | Notes |
|---|---|---|
| Round 1 training (8 × 5k step) | ~40 min | Negligible |
| Round 1 H2H smoke (8 × 5 min) | ~40 min | Auto-gated |
| Round 1 H2H full (8 × 30 min, only if smoke pass) | ~4 hours | Worst case |
| **Round 1 total** | **~5 GPU hours** | Spreads over 1-2 days |

### What's preserved

- v2 stays live throughout. P6 paper-soak continues. SOAK-7 keeps measuring.
- All experiment outputs land in `models/chronos/_experiments/<exp_id>/`
  (NOT in the live `models/chronos/phase2_v2/` path).
- The H2H harness from Stage 1.1 is the validation primitive — no new
  eval infrastructure needed.

---

## Part 3 — Round 2 design (scale-up, conditional on Round 1 signal)

Round 2 only runs if Round 1 surfaces 2-3 candidates worth scaling.

### Round 2.1 — Scale up

For each Round 2 candidate (max 3):
- Run 100k step chunked retrain (~5 GPU hours each)
- Same H2H full eval at the end
- Add E01-noise-floor confidence interval to comparison

### Round 2.2 — Council review

Council 4-voice convenes with Round 2 reports as input:
- **Architect:** does the change actually make sense in production
  (compute, deploy, observability)?
- **Skeptic:** is the OOS improvement beyond E01 noise floor? Any cherry-picking?
- **Pragmatist:** what's the rollout cost? P6 impact?
- **Critic:** are we testing the right thing? Should we wait for bigger panel?

### Round 2.3 — Promotion decision

Promote if and only if:
1. OOS MASE ≤ v2 (or within 5% with significant hit_rate gain)
2. hit_rate > 0.55 (Round 1 promotion gate)
3. Std across seeds (E01-style reproducibility) < 0.05
4. Council votes 3/4 in favor

If no candidate passes: **stay on v2** and expand OOS panel to 50 syms
before next attempt.

---

## Part 4 — Open questions / risks

1. **E07 classification head** requires modifying Chronos fine-tune code
   more than the others. If it doesn't fit in the existing
   `train_chronos_baseline.py`, we may need to skip E07 in Round 1 and
   defer it to Round 2 as a separate effort. Will be visible at config-time.

2. **GPU box is shared.** Round 1's 5 GPU hours should be spread across
   days, not run in a single burst. The runner supports
   `--train-only` / `--smoke-only` / `--full-only` flags so each phase can
   be run independently at convenient times.

3. **Time-of-day for experiments.** The GPU box is also used for v12
   paper-soak scheduler (09:35-16:30 EST weekdays). Experiments should
   run outside those hours or on weekends.

4. **E05 context_length change** may require a Chronos config flag we
   haven't exposed in `train_chronos_baseline.py` yet. If so, document as
   a "won't fix in Round 1, defer to infra work" outcome.

---

## Part 5 — Success criteria for Stage 2.x as a whole

Stage 2.x is **successful** if any of these is true at end of Round 2:
- A new live candidate is promoted that clears all 4 promotion gates, OR
- We have rigorous evidence that no in-framework Chronos variant can
  clear the 0.55 hit_rate bar, which informs a deeper rethink (new
  backbone, narrower universe, or different signal class entirely).

Stage 2.x is **failure** if we end up in the same state as v3: 16 hours
burned, no actionable signal, no clear next step.