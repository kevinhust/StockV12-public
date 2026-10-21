# Stage 2.x — Round 3: Chronos Multi-Seed Reproducibility Sweep

> **Purpose:** Round 3 closes the residual single-seed risk in the
> Round 2 promotion decision. Day-30 gate PASSED on 2026-07-01 with
> `phase2_v2` + `E05_p3_coldstart_seed42` (one seed per candidate).
> Round 3 runs each candidate variant across **multiple seeds** and
> re-evaluates the promotion gate on the multi-seed distribution
> instead of a single sample.
> **Author:** Grok (Council-aligned)
> **Date:** 2026-07-01
> **Status:** ⏳ Awaiting kickoff — design only.
> **Related docs:** [V3_LESSONS_AND_EXP_PLAN.md](V3_LESSONS_AND_EXP_PLAN.md) ·
> [ADR-009-WalkForward-Backfill-Addendum.md](ADR-009-WalkForward-Backfill-Addendum.md)

---

## Why Round 3 exists

### Day-30 gate was earned on N=1 seed

The 30-day walk-forward backfill PASSED both candidates:

| Candidate                       | pnl   | mase | win_rate | n_trades | gate  |
|---------------------------------|-------|------|----------|----------|-------|
| `phase2_v2` (live, existing)    | +0.55 | 2.06 | 0.50     | 30       | ✅ PASS |
| `E05_p3_coldstart_seed42`       | +0.55 | 2.06 | 0.50     | 30       | ✅ PASS |

But both samples came from a **single training seed**. The V3_LESSONS
"Lesson 3" lesson explicitly said variance / sample noise is unmeasured
and that std(hit_rate) across seeds is the only thing that converts
a 10-symbol panel result from "noisy observation" to "real signal".

Round 3 is the 5-seed extension that converts Round 2's gate verdict
from "this seed is good" to "this **variant** is good, with std < 0.05
across seeds".

### What Round 1/2 did not measure

- Std(hit_rate) across seeds for the ctx=512 + cold-start variant
- Sensitivity of Day-30 gate metrics to seed (pnl swing, win_rate swing)
- Whether the cold-start config (P3) collapses to random or beats v2
  reliably or only on lucky seeds
- Whether `phase2_v2` itself survives a reproducibility test (it was
  originally a single-seed run; Round 3 establishes v2's noise floor)

---

## Scope

### In scope

- 2 variants, each run across **5 seeds**, on the GPU box, in series
  (no concurrency — GPU is shared with cron).
- Identical feature set (`v12/scripts/feature_engineering.py --freq daily
  --phase 1`, regenerated nightly by the new polygon cron).
- Identical OOS panel — the same 10-symbol set used in Round 2:
  `AAPL NVDA TSLA JPM MSFT AMZN AMD META GOOGL SPY`.
- Identical evaluation harness — `v12/scripts/eval_chronos_h2h_v3.py`
  for OOS H2H; `v12/scripts/backfill_30_days.py` for walk-forward.
- All 5 seeds per variant must clear the **multi-seed promotion gate**
  (see below) for the variant to advance.

### Out of scope

- New model architectures (E07 classification head, regime conditioning).
  These were Round 1/2 candidates that did not survive single-seed.
  Re-promoting them here would defeat the point of the round.
- Expanding the OOS panel beyond 10 symbols. Already discussed in Round 2:
  larger panel would let us shorten the multi-seed count, but it changes
  the comparability of results. Deferred to a Round 4 if needed.
- Retraining from scratch on new data. Both candidates are already
  trained through Round 2 and only the seed dimension is missing.

---

## Variants

### Variant A — `E05_ctx512_p3_coldstart`

The Round 2 winner. Config:

```yaml
base_model: chronos-bolt-small
context_length: 512            # E05 hypothesis
fine_tune_steps: 5000
learning_rate: 1e-5            # P3 cold-start recipe
random_seed: <seed>             # 5 different seeds
universe: phase1
```

Family reconfirmation: this is the same variant as
`E05_p3_coldstart_seed42` from Round 2, just re-trained with seed ∈
{7, 17, 23, 42, 71} so the median sample is interpretable.

### Variant B — `phase2_v2_repro`

The existing live model, retrained with the same recipe (1k step,
default LR) across the same 5 seeds. This establishes the **noise
floor** that Variant A's std is measured against.

```yaml
base_model: chronos-bolt-small
context_length: 1024           # Phase 2 default
fine_tune_steps: 1000
learning_rate: 1e-4            # Phase 2 default
random_seed: <seed>
universe: phase1
```

`phase2_v2` is currently live and was trained with seed=42 historically.
This Round 3 run does **not** replace v2 — it produces 5 sibling
checkpoints under `models/chronos/_experiments/_round3/v2_repro/`
that exist purely for std measurement.

---

## Seed selection

5 seeds chosen to avoid arithmetic relationships and to bracket a
reasonable spread:

```
seeds = [7, 17, 23, 42, 71]
```

- `42` is the Round 2 reference seed (already exists, included for
  baseline).
- `7, 17, 23, 71` are small primes with no obvious correlation to the
  recipe's internal init states.

The set is hand-curated, not random — reproducibility matters more
than randomness at this small N.

---

## Experiment runner integration

Round 3 is mechanical: no new harness needed. The existing
`v12/scripts/run_experiment.py` already accepts `--seed`:

```bash
for variant in E05_ctx512_p3_coldstart phase2_v2_repro; do
  for seed in 7 17 23 42 71; do
    PYTHONPATH=v12/src:src:. python3 v12/scripts/run_experiment.py \
      --config "v12/experiments/round3/${variant}.yaml" \
      --seed "$seed" \
      --train-only
    # H2H eval runs separately so it can fan out overnight
  done
done
```

YAML config stubs to add (under `v12/experiments/round3/`):

```
v12/experiments/round3/
├── E05_ctx512_p3_coldstart.yaml
├── phase2_v2_repro.yaml
└── sweep_index.json   # generated at end-of-round
```

Each sweep produces 5 × 2 = **10 short training runs** (~3-5 min
each on RTX 4090, identical to Round 1 budget). Total training cost
under 1 GPU hour.

---

## Evaluation

### Step 1 — OOS H2H per seed

For each (variant, seed) pair, run H2H on the standard 10-symbol ×
2-cutoff panel. Reuse Round 2 eval logic. Each pair takes ~30 min.
10 pairs × 30 min = **~5 GPU hours**, spread across 2-3 days to
respect the cron.

Output per pair:
```
models/chronos/_experiments/_round3/<variant>/seed=<seed>/h2h.json
```

### Step 2 — Walk-forward 30-day per seed

For each seed that passes H2H, run `backfill_30_days.py` to produce the
same walk-forward evidence that closed Round 2. 10 walk-forwards × 30-50
min = **~5-8 GPU hours** worst-case, can be skipped for `phase2_v2_repro`
if H2H indicates v2 noise floor is well-known.

### Step 3 — Per-seed gate verdict

For each (variant, seed), apply the **single-seed gate**:

| Metric     | Threshold | Notes                                          |
|------------|-----------|------------------------------------------------|
| pnl        | > 0       | sign-correctness over 30-day window            |
| mase       | ≤ 4.011   | ADR-008 baseline; loosened from 2.40 because gate was too tight for chr-bolt-small alone |
| win_rate   | ≥ 0.40    | baseline 0.50 ± 0.10 noise                     |
| n_trades   | ≥ 30      | mechanical; distinct eval_date enforced by ADR-009 Bug #1 fix |

Record pass/fail per (variant, seed). Goal is **5/5 seeds pass for
Variant A** and at least **3/5 seeds pass for Variant B** (it
historically does and we want the noise floor, not a promotion signal).

### Step 4 — Sweep-level gate verdict

| Metric                  | Threshold                              |
|-------------------------|----------------------------------------|
| Median pnl              | > 0 (Variant A)                        |
| Median mase             | ≤ 4.011 (Variant A)                    |
| Median win_rate         | ≥ 0.40 (Variant A)                     |
| Std(win_rate) across seeds | < 0.05 (both variants)              |
| P(per-seed gate pass)   | ≥ 0.80 (4/5 seeds, Variant A only)     |
| Std(mase) across seeds  | < 0.50 (both variants — sweep stability) |

Promotion rule:
- **Variant A promoted if and only if all 5 sweep-level metrics pass AND
  ≥4/5 individual seeds pass.** Round 2's seed=42 verdict confirms one
  data point; Round 3 needs 4 more to call it reproducible.
- **Variant B not promoted** — it's a measurement artifact, not a candidate.
  Used only to set the noise floor that Variant A's std is measured against.

---

## Council review gate

A 4-voice council reviews the sweep-level report before any production
promotion:

- **Architect:** does `E05_ctx512_p3_coldstart` actually work in
  production (compute, deploy, observability), not just in eval?
- **Skeptic:** is `Std(win_rate) < 0.05` meaningful given N=5 seeds ×
  30 trades = 150 trades, which is ~half the power to detect 5pp
  differences? Are we committing a small-N pseudo-precision?
- **Pragmatist:** what does it cost to keep both v2 and the new model
  live? v12 paper-soak bandwidth? Cron load?
- **Critic:** have we eliminated all data and code alignment bugs from
  ADR-009 (eval_date slicing, partition merge)? Any reason to suspect
  the new variant's std includes bug noise?

Promotion requires **3/4 council votes**, same rule as Round 2.

---

## Resource budget

| Phase                                | GPU minutes     | Notes                          |
|--------------------------------------|-----------------|--------------------------------|
| Variant A 5× training (5k step)      | ~25 min         | ~3 min each on RTX 4090        |
| Variant B 5× training (1k step)      | ~15 min         | ~3 min each                    |
| H2H eval (10 pairs × 30 min)         | ~300 min        | Parallelism: 1 GPU, serial     |
| Walk-forward (5 Variant A pairs)     | ~150 min        | Skip for Variant B             |
| Aggregation + council prep           | ~30 min         | local CPU                      |
| **Round 3 total**                    | **~9 GPU hours**| Spread over 2-3 days           |

Compare to Round 2's per-seed backfill (already done) — Round 3 is
**~2x** the per-candidate eval cost but with **5x seed evidence**,
reducing the re-run risk if seeds are unstable.

---

## What Round 3 does NOT change

- `phase2_v2` remains the **live paper-soak** model throughout Round 3.
  No production promotion during the round.
- `E05_p3_coldstart_seed42` does NOT become the live model just because
  it passed seed=42 walk-forward. It stays a candidate until the
  sweep-level gate is met.
- Shadow-trade cron (16:05 NYC) keeps running. SOAK-7 keeps measuring.
- ADR-008 walk-forward gate unchanged.

---

## What changes if Round 3 fails

If Variant A fails the sweep-level gate (e.g., Std > 0.05, or only 3/5
seeds pass individually):

1. **Stop promotion.** Stay on v2.
2. **Council convenes** on whether the failure is meaningful (real
   variance) or whether it's a 5-seed small-N artifact.
3. **Possible Round 4:** larger OOS panel (50 symbols × same 5 seeds) to
   either confirm variance is real (switch to a more stable backbone)
   or refute it (round was underpowered, retry).
4. **Do not** retry the same variant with 10 seeds hoping for a pass.
   That's p-hacking.

If Variant B (v2_repro) Std is unexpectedly large, that's also a
finding — it means the historical `phase2_v2` may be a lucky seed
too, and the noise floor of the platform is wider than we thought.

---

## Success criteria for Round 3

Round 3 is **successful** if any of:
- Variant A clears the sweep-level gate → promote to live alongside v2,
  then start dual-live shadow and re-evaluate at N=30 days.
- Variant A fails sweep gate but the failure pattern (which seeds, which
  metric) gives a concrete next-action signal (different ctx, different
  LR, different backbone).

Round 3 is **failure** if both variants produce washout results
(everything in noise) with no actionable signal — that means the
Chronos approach has hit its complexity ceiling for this universe,
and we should be honest about it.

---

## Open questions / risks

1. **5 seeds is small.** Std estimates from N=5 are noisy themselves.
   We're betting that the cheapest defensible answer is "≥4/5 seeds
   pass and the median is clean", not "frequentist 95% CI is below
   the threshold". If the council objects, Round 3 may need to expand
   to 10 seeds before promotion.
2. **OOS panel size.** 10 symbols × 30 trades = 300 observations per
   sweep. Median estimator noise on N=300 trades is ±0.05 in win_rate
   at the 50% level (binomial SE). The Std(win_rate) < 0.05 gate
   includes this noise. Council review should account for it.
3. **Daily cron stability.** Round 3 is built on the assumption that the
   new polygon cron (added in commit `517bd1d9`) keeps data fresh and
   feature regeneration stable. If the cron fails mid-sweep, features
   drift and the sweep results become non-comparable. The cron failure
   path is FATAL (per `run_polygon_ingest_cron.sh`), which means we'd
   notice — but we'd also need to re-run any post-failure eval slices.

---

## Open question: do we seed `E05` with cron-accumulated data?

The existing `E05_p3_coldstart_seed42` was trained on the data as of
2026-06-29. Since then, daily polygon cron has added:
- 2026-06-26 (re-stated)
- 2026-06-29 (re-stated)
- 2026-06-30 (new)
- 2026-07-01 (new)

For the **new** Round 3 seeds (7, 17, 23, 71), training should happen
on the latest features (refreshed by the polygon cron up to "today")
because the model's regime assumption includes recent days. But this
makes the comparison slightly uneven — the original seed=42 sees old
data, the new seeds see fresh data.

**Recommendation:** re-train seed=42 in the same sweep using the new
features, so all 5 seeds share the same training data window. This
shifts the Round 2 reference to "the version as of 2026-07-01 data",
which is the data any future live candidate will see anyway. Document
the data-window shift in the sweep report.

---

## References

- `v12/scripts/run_experiment.py` — multi-seed experiment runner
- `v12/scripts/eval_chronos_h2h_v3.py` — OOS H2H evaluation
- `v12/scripts/backfill_30_days.py` — walk-forward backfill orchestrator
- `v12/scripts/shadow_trade.py::check_shadow_gate` — gate logic
- `scripts/run_polygon_ingest_cron.sh` — daily data refresh (ensures
  training data is consistent across seeds)
- ADR-008 walk-forward promotion gate (the single-seed gate repeated here)
