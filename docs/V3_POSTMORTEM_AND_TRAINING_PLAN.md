# Phase2_v3 Retrain — Postmortem & Forward Training Plan

**Date:** 2026-06-29
**Author:** Grok (Council-aligned)
**Status:** v3 retrain **CLOSED — no promotion**. v2 remains live.

---

## Part 1 — What we did

We hypothesized that a longer fine-tune of Chronos-Bolt (1M steps, 10k per
chunk × 100 chunks) warm-init'd from the existing phase2_v2 (1k steps)
would improve swing-trading forecasts.

| Step | Action | Outcome |
|---|---|---|
| 1 | Build chunked warm-init retrain pipeline (per-chunk predictor, fine-tuned-ckpt) | Works. 100 chunks trained in ~16 GPU hours. |
| 2 | Train chunk 0..99 (10k steps each) | Each chunk's in-sample MASE improved monotonically (-13.17 → -14.20). |
| 3 | Spot-check at chunk 17 (~150k steps) | Saw degradation, kept training. |
| 4 | Finish to 1M | Peak in-sample at chunk 94 (-14.20), final at chunk 99 (-14.02). |
| 5 | **H2H eval on 10 mega-cap symbols × 2 cutoffs (10d, 20d)** | **v2 wins on mean OOS MASE (2.52 vs 2.65+ for all v3 ckpts).** |

---

## Part 2 — Why we failed

### 2.1 In-sample MASE was misleading

AutoGluon's internal `eval_metric=MASE` validation window is the **last
`prediction_length=10` days of the training data**. With 100×10k=1M
gradient steps, the model memorized that specific 10-day window. In-sample
MASE kept improving because we were tightening fit on a leak.

**Result:** the in-sample number we tracked during training was a leaky
proxy, not a generalization signal.

### 2.2 The v3 signature is classic overfit

| Symptom | Observed | Interpretation |
|---|---|---|
| In-sample MASE improves with steps | -13.17 → -14.20 | Memorizing val window |
| OOS MASE degrades | 2.91 → 2.65+ | Real generalization gets worse |
| Signal confidence rises | 5/10 → 10/10 above 0.5% thr | Bolder forecasts |
| Signal accuracy drops | Higher MASE on those bolder forecasts | "Confident but wrong" |
| 20-day extrapolation collapses | v3 loses by 15%+ at cutoff=20d | High-variance fits don't generalize |

### 2.3 H2H evidence is thin (caveat we should not over-rotate on)

- 10 mega-cap symbols only — small caps, sector ETFs underrepresented
- 2 cutoffs (10d, 20d) — gap of 0.13-0.18 MASE is well inside symbol-to-symbol noise
- 20-day forward window — could be an unusually easy or hard period
- Same 22 features, same backbone — confounds "is it data or model?"

**Implication:** v2's "win" is real but statistically fragile. We should not
treat this H2H as the final word on whether v2 is good. It IS a hard
signal that v3 is worse, and that is enough to not promote.

---

## Part 3 — Council: what to do next

We convened a 4-voice council (Architect, Skeptic, Pragmatist, Critic) on
2026-06-29 to set the forward path.

### 3.1 Skeptic (strongest premise challenge)

> Stop retraining. v2 won. MASE may be a non-problem — for swing trading we
> need **directional** signal, not magnitude accuracy. MASE 2.5 on 10
> mega-caps with 22 noisy features and a 200M foundation doing 99% of the
> work is plausible, not catastrophic. We never validated that lower MASE
> improves trading PnL. **Evaluate hit-rate and sign-correctness on OOS
> before any further training.**

### 3.2 Pragmatist (highest leverage action)

> Don't retrain. Ship P6 with v2 this week. The **H2H eval harness is the
> real asset** — lock it into CI so every future ckpt gets OOS-gated
> automatically. That's what stops the next 16-hour mistake.
>
> **GPU allocation next 1-2 weeks: 0% retrain, 100% P6 soak + CI
> integration.**

### 3.3 Critic (next concrete experiment, gated)

> v3 is overfit by definition. Don't repeat the recipe. The next move is a
> **small seed-ensemble of v2-style fine-tunes** (5 seeds × 1k steps) with
> strict OOS gates — not more steps, not v3 promotion. H2H evidence is too
> thin to be confident v2 is the right baseline.
>
> **Real upside is in (a) regime-conditioned inference, (b) seed/horizon
> ensembling, (c) widening the symbol universe.** Treating this as a
> training problem when it's a data-coverage problem is the trap.

### 3.4 Architect (synthesized verdict)

- **Consensus (3/3):** Do not promote v3. Do not repeat v3-style retrain.
  Keep v2 as live. Lock the H2H harness into CI immediately.
- **Strongest dissent:** Skeptic's premise challenge (MASE may be wrong
  metric) vs Critic's tactical next-step (seed ensemble).
- **Premise check: ✓** Skeptic really did challenge "why MASE" — this is
  the council's most valuable output.
- **Recommendation:** staged, with the metric question answered first.

---

## Part 4 — Action plan (staged)

### Stage 0 — Now through 2026-07-05 (P6 paper-soak)

| # | Action | Owner | DoD | Status |
|---|---|---|---|---|
| 0.1 | Continue P6 paper-soak with v2 (current live) | owner | 5 trading days completed, journal filed | in progress (v12/docs/P6_SOAK_CHECKLIST.md) |
| 0.2 | Archive v3 chunked retrain output to `models/chronos/_archive/phase2_v3_1M_2026-06-29/` | Grok | 100 ckpt dirs + progress.json archived | ✅ DONE (2026-06-29) — see H2H_V3_REPORT.md |
| 0.3 | Update Plans.md to mark v3 retrain task as **closed-no-promote** | Grok | Plans.md row updated + commit | ✅ DONE (2026-06-29, in this PR) |

### Stage 1 — This week (1-2 days of work, no GPU)

The two actions all 3 voices agreed on. Both are unblockers for any later
training decision.

| # | Action | Owner | DoD | Why | Status |
|---|---|---|---|---|---|
| 1.1 | Lock H2H eval into CI: `pytest v12/tests/test_eval_chronos_h2h_v3.py` must pass in PR gate; add a thin smoke variant that runs the actual eval on 3 symbols × 1 cutoff (gated on GPU presence) | Grok | CI step added; H2H runs <5 min for the smoke variant | Pragmatist: prevent next 16-hour mistake | ✅ DONE (2026-06-29) — `--smoke` flag + `.github/workflows/h2h_ci.yml` (pytest + self-hosted `stockapp-gpu` smoke job) + `scripts/run_h2h_ci.sh` local mirror |
| 1.2 | Add **directional-signal metrics** (hit-rate, sign correctness) to the H2H eval, alongside MASE. Primary report now includes: MASE + 5-class direction accuracy + |signal| > threshold rate. | Grok | `eval_chronos_h2h_v3.py` reports direction metrics; unit tests for the new pure functions | Skeptic: validate that v2 is winning on the metric that matters | ✅ DONE (2026-06-29) — see "Stage 1 Results" below |

#### Stage 1 Results (2026-06-29)

Directional metrics added to `eval_chronos_h2h_v3.py` (12 new TDD tests,
27/27 pass). Full H2H re-run on archive paths (v3 ckpts moved to
`_archive/phase2_v3_1M_2026-06-29/`). See `v12/docs/H2H_V3_REPORT.md`
"Directional (sign-correctness) metrics" section for full tables.

**Mean hit_rate ranking** (higher better, gate ≥ 0.53):

| Model | mean hit_rate | mean signal_hit_rate |
|---|---|---|
| v3_ckpt_940k | 0.45 | 0.50 |
| v3_ckpt_1000k | 0.45 | 0.4334 |
| v3_ckpt_950k | 0.40 | 0.4445 |
| **v2 (live)** | **0.30** | 0.3143 |

**Stage-1 gate verdict:** **NO MODEL CLEARS** the 0.53 hit_rate gate.
Best is v3_ckpt_940k at 0.45 (−8pp below gate). **v2 itself FAILS at
0.30 (−23pp below gate)** — a sobering finding: the live model's
sign-correctness edge is below 50% coin-flip on this 10-symbol panel.

### Stage 2 — Next sprint (only if Stage 1 confirms v2 is the right baseline)

**STATUS: CLOSED (2026-06-29) — Stage-1 gate FAILED for v2.**

Three options, pick based on what Stage 1 reveals:

| Option | Hypothesis | Cost | Risk |
|---|---|---|---|
| 2A. **Seed ensemble** of 1k-step fine-tunes (5 seeds) | Variance reduction at low step count | ~30 min GPU | May not improve OOS; H2H gates it |
| 2B. **Optuna HPO** (30 trials, 1k steps each) | Find better LR / batch / context | ~5-8 GPU hours | Same data may not have a better config |
| 2C. **Feature engineering** (volatility regimes, sector encodings, macro features) | Better inputs > better weights | Days of feature work, no GPU | May not help if signal is in the data already |

**Decision gate:** ~~Stage 1 must show v2's directional hit-rate is
≥ 53% (50% = coin flip) on the 10-symbol OOS set.~~ **Gate failed
(0.30 actual). Stage 2 paused.**

**New direction question:** "Can any model produce a sign-correctness
edge ≥ 53% on this panel?" — if yes, that becomes the new live.
Candidate future work (NOT Stage 2 — broader rethink needed first):
1. **Re-examine backbone**: Chronos is quantile regression — try a
   classification fine-tune (predict sign class directly).
2. **Narrow symbol universe**: find the sub-panel where edge actually
   exists; trade only those.
3. **Add auxiliary signal**: YOLO no-trade filter already wired; maybe
   add momentum / vol regime conditioning before sending Chronos signal
   to SR engine.
4. **Re-run H2H with bigger panel** (50 syms) to reduce variance of
   hit_rate estimate before drawing big conclusions from a 10-symbol
   window.

### Stage 3 — Future (only after Stage 2 success)

- **Regime-conditioned inference**: condition forecast on detected
  vol regime (BULL_HIGH_VOL, BEAR_LOW_VOL, etc.) and only apply SR-01
  threshold when regime is favorable. Requires regime classifier, not a
  retrain.
- **Widen symbol universe**: train/eval on small caps + sector ETFs so
  the OOS test is less correlated.
- **Ensemble v2 + Cronos-Bolt zero-shot** (no fine-tune baseline) to
  bound how much fine-tuning is actually helping.

### Immediate safety net (added 2026-06-29)

Because Stage 1's surprising finding is that **the live v2 model itself
fails the 53% hit_rate gate** (actual: 0.30), the P6 paper-soak is now
running with a known-bad directional metric. To measure live behavior
without blocking the soak:

- **SOAK-7 sign-agreement check** — `v12/scripts/paper_soak_checklist.check_sign_agreement`
  + per-(date,sym) Redis keys written by `src/services/lifecycle_scheduler._run_entry_window`.
  WARN-ONLY. 28 unit tests in `v12/tests/test_paper_soak_sign_agreement.py`.
  See `docs/P6_SOAK_CHECKLIST.md` "Critical caveat (added 2026-06-29)"
  for the full runtime guidance.

**Action during P6**: if SOAK-7 is consistently ≤ 0.45 for 3+ days,
pause the soak and raise to Council. If ≥ 0.55 for 5 days, that's
positive signal — the OOS panel may have underestimated live edge.

---

## Part 5 — Lessons (for the team)

1. **Never trust in-sample validation on a leaky window.** AutoGluon's
   `eval_metric` window is the tail of the train data; for time-series
   forecasting you need truly OOS forward windows.
2. **"Confident but less right" is a worse failure mode than "uncertain
   and right"** for our use case. v3 made bolder forecasts that lost
   money on the magnitude even if direction was OK.
3. **Sunk cost is not a strategy.** The 16 GPU hours on v3 are a fact;
   the next 16 hours are a choice. Choose based on evidence, not on
   pipeline existence.
4. **Lock OOS eval into CI the day you build it.** A one-time eval is a
   postmortem; a CI gate is a guardrail.
5. **Question the metric before optimizing it.** MASE 2.5 might be fine
   for trading if the directional hit rate is good. We don't yet know.

---

## Part 6 — Artifacts

| File | Purpose |
|---|---|
| `v12/scripts/eval_chronos_h2h_v3.py` | OOS H2H eval script (now baseline for any new ckpt) |
| `v12/tests/test_eval_chronos_h2h_v3.py` | 15 unit tests for eval math + ranking |
| `v12/docs/H2H_V3_REPORT.md` | Detailed v3 vs v2 OOS H2H result (this report's data) |
| `models/chronos/h2h_v3_report.json` | Raw per-symbol, per-cutoff eval data |
| `models/chronos/phase2_v2/` | **Live model — unchanged** |
| `models/chronos/phase2_v3_ckpt_*/` | All 100 v3 checkpoints (to be archived) |
| `models/chronos/phase2_v3_progress.json` | Training state file |
| `logs/chronos_v3_1M_*.log` | Training logs |

---

## Decision

> **Phase2_v3 is closed without promotion. Phase2_v2 remains the live
> Chronos model. Future retraining decisions must include OOS H2H
> gates (now in CI) and direction-accuracy metrics before scaling to
> millions of steps. The eval harness, not the model, is the durable
> asset of this work.**
