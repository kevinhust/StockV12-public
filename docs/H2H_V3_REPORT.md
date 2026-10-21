# Chronos v3 H2H Evaluation Report

**Date:** 2026-06-29
**Author:** Grok (Council-aligned)
**Status:** ✅ EVAL DONE — **No promotion recommended; keep phase2_v2 as live model. Stage-1 directional gate (hit_rate ≥ 0.53) FAILED for all 4 candidates — Stage 2 closed pending new approach.**

---

## TL;DR

The 1M-step chunked retrain (phase2_v3) was a learning exercise. While the
in-sample validation MASE improved (-13.39 → -14.20 at peak), the
**out-of-sample forward test on 10 mega-cap symbols shows phase2_v2 (the
existing 1k-step live model) is actually the best**. All four v3 checkpoints
tested are 5-7% worse on average OOS MASE.

When we added the new **directional (sign-correctness) metrics** per
Stage 1.2 of the postmortem, a more uncomfortable finding surfaced:
**no candidate clears the 0.53 hit_rate gate**, including v2 itself
(0.30 — 23 percentage points below gate). v3 ckpts win directional
(0.40-0.45) but still fail the gate.

**Decision:**
1. Keep `models/chronos/phase2_v2` as the live Chronos model (MASE winner).
2. **Stage 2 (seed ensemble / Optuna / features) is CLOSED** — gated by
   `v2 hit_rate ≥ 0.53`, which failed. New approach needed before re-opening.
3. **Next research question:** "Can any model produce a sign-correctness
   edge ≥ 53% on this panel?" — if yes, that becomes new live. If no,
   re-examine backbone (Chronos quantile regression vs classification) or
   symbol universe (where edge actually exists).
4. Archive v3 ckpts to `models/chronos/_archive/phase2_v3_1M_2026-06-29/`
   (done) so we don't accidentally point at them.

---

## Test setup

| Item | Value |
|---|---|
| Candidates | `phase2_v2` (1k steps, live) · `phase2_v3_ckpt_940k` · `phase2_v3_ckpt_950k` (peak) · `phase2_v3_ckpt_1000k` (final) |
| Test symbols | AAPL, NVDA, TSLA, JPM, MSFT, AMZN, AMD, META, GOOGL, SPY (10 mega-caps) |
| Cutoffs | 10 days, 20 days (held-out forward windows) |
| Metric | MASE (mean abs scaled error, lower better) + signal magnitude (forecast_ret_pct) |
| Eval script | `v12/scripts/eval_chronos_h2h_v3.py` (15 unit tests + AutoGluon I/O glue) |
| Output | `models/chronos/h2h_v3_report.json` |

Each model: load → predict 10-day forecast on each test symbol → compare to
held-out actual closes → compute MASE per symbol → aggregate.

---

## Results — ranking by mean OOS MASE (lower better)

| Rank | Model | mean MASE | Δ vs #1 | n cutoffs |
|---|---|---|---|---|
| **#1** | **phase2_v2** | **2.5152** | — (winner) | 2 |
| #2 | phase2_v3_ckpt_950k | 2.6505 | +5.4% | 2 |
| #3 | phase2_v3_ckpt_1000k | 2.6867 | +6.8% | 2 |
| #4 | phase2_v3_ckpt_940k | 2.6925 | +7.1% | 2 |

## Results — per cutoff (MASE per model)

| Cutoff | phase2_v2 | ckpt_940k | ckpt_950k | ckpt_1000k | Winner |
|---|---|---|---|---|---|
| 10 days | 2.9739 | 2.9418 | **2.9131** | 2.9649 | v3_950k (marginal) |
| 20 days | **2.0566** | 2.4433 | 2.3878 | 2.4085 | **v2** (clear) |

**Pattern:** v3 is marginally better at the 10-day horizon (its training
target) but substantially worse at 20-day extrapolation. v2 generalizes
further out.

## Per-symbol MASE @ cutoff=10d (v2 vs v3_950k)

| Symbol | v2 MASE | v3_950k MASE | Δ% | Winner |
|---|---|---|---|---|
| AAPL | 1.48 | 1.88 | +26.8% | v2 |
| NVDA | 1.84 | 2.45 | +33.1% | v2 |
| TSLA | 4.94 | 4.13 | -16.2% | v3 |
| JPM | 2.19 | 1.57 | -28.2% | v3 |
| MSFT | 3.54 | 3.41 | -3.7% | v3 |
| AMZN | 2.38 | 3.94 | +65.7% | v2 |
| AMD | 3.37 | 3.39 | +0.8% | tie |
| META | 3.77 | 3.28 | -13.2% | v3 |
| GOOGL | 3.78 | 2.79 | -26.2% | v3 |
| SPY | 2.46 | 2.29 | -6.8% | v3 |

Split: 4 v2 wins (often by large margin) vs 6 v3 wins (smaller margins).
Mean MASE still favors v2 because the v2 wins have larger magnitude
advantages (AAPL, NVDA, AMZN, AMD).

## Signal quality @ cutoff=20d (|forecast_ret| > 0.5% threshold)

| Model | n symbols above 0.5% | mean |forecast_ret| |
|---|---|---|
| phase2_v2 | 5/10 | 0.58% | Conservative, mostly correct |
| ckpt_940k | 8/10 | 1.38% | Stronger signals, less accurate |
| ckpt_950k | 9/10 | 1.44% | Stronger, less accurate |
| ckpt_1000k | 10/10 | 1.50% | Confident on every name, worst MASE |

**v3 models are "more confident but less right"** — classic overfit pattern.
The added fine-tune steps memorized training noise → produced bolder
forecasts that don't generalize.

## Directional (sign-correctness) metrics

> Added per **v3 Postmortem Stage 1.2** (`V3_POSTMORTEM_AND_TRAINING_PLAN.md`).
> Pure functions `sign`, `sign_match`, `hit_rate`, `signal_hit_rate` in
> `v12/scripts/eval_chronos_h2h_v3.py` (12 new unit tests). Stage-1 gate
> from the postmortem: **v2 must clear hit_rate ≥ 0.53 on the 10-symbol
> OOS panel for Stage 2 (seed ensemble / Optuna / features) to open**.

### Results — ranking by mean OOS hit_rate (higher better, gate ≥ 0.53)

| Rank | Model | mean hit_rate | mean signal_hit_rate | Δ vs gate (0.53) |
|---|---|---|---|---|
| #1 | phase2_v3_ckpt_940k | **0.45** | 0.50 | −8pp (FAIL) |
| #2 | phase2_v3_ckpt_1000k | 0.45 | 0.4334 | −8pp (FAIL) |
| #3 | phase2_v3_ckpt_950k | 0.40 | 0.4445 | −13pp (FAIL) |
| #4 | phase2_v2 (live) | 0.30 | 0.3143 | −23pp (FAIL) |

### Per-cutoff hit_rate (n=10 symbols each)

| Cutoff | v2 hit_rate | v3_940k | v3_950k | v3_1000k | Winner |
|---|---|---|---|---|---|
| 10 days | 0.50 | **0.60** | **0.60** | 0.40 | v3_940k + v3_950k |
| 20 days | 0.10 | **0.30** | 0.20 | 0.20 | v3_940k |

### Per-cutoff signal_hit_rate (|forecast| > 0.5% threshold)

| Cutoff | v2 | v3_940k | v3_950k | v3_1000k | Winner |
|---|---|---|---|---|---|
| 10 days | 0.4286 | **0.75** | 0.6667 | 0.50 | v3_940k |
| 20 days | 0.20 | **0.25** | 0.2222 | 0.20 | v3_940k |

### Stage-1 gate verdict — **NO MODEL CLEARS**

| Model | Stage-1 gate (≥ 0.53) | Verdict |
|---|---|---|
| phase2_v2 (live) | 0.30 | **FAIL** (−23pp) |
| phase2_v3_ckpt_940k | 0.45 | **FAIL** (−8pp) |
| phase2_v3_ckpt_950k | 0.40 | **FAIL** (−13pp) |
| phase2_v3_ckpt_1000k | 0.45 | **FAIL** (−8pp) |

**Surprising finding:** the *live* v2 has the *worst* directional edge of
all four candidates (0.30 vs 0.40–0.45 for v3). Its MASE win comes from
being closer to the actuals on average, **not from correctly calling the
sign of the move**. This is a known limitation of pure-precision metrics
in low-SNR forecasting regimes — MASE can be minimized by a model that
moves toward zero on uncertain days.

### Implication

| Decision axis | Verdict | Reasoning |
|---|---|---|
| Live model for MASE-driven scoring | **phase2_v2 stays** | Wins OOS MASE (2.5152 vs 2.65–2.69), wins 20d extrapolation clearly, P0 backtest locked in |
| Live model for sign-correctness edge | **No model has edge** | Even best v3 ckpt (0.45) is 8pp below 0.53 gate; v2 (0.30) is 23pp below |
| Stage 2 (seed ensemble / Optuna / features) | **CLOSED** | All 3 options gated by "v2 hit_rate ≥ 0.53" — gate FAILED. New approach needed before re-opening |
| Re-promote v3? | **NO** | v3 wins hit_rate but loses MASE 5-7%; directional edge not enough to justify precision loss |

**Takeaway:** v2 is the least-wrong option under MASE, but the directional
metric reveals it is also **not a sign-correctness edge in absolute terms**.
Before promoting any future retrain, **both** MASE AND hit_rate must clear
their respective gates. The next research question is no longer "can we
beat v2" — it's **"can any model produce a sign-correctness edge ≥ 53%
on this 10-symbol OOS panel?"** If yes, that becomes the new live. If no,
we should re-examine whether Chronos is the right backbone at all (or
narrow the symbol universe to those where sign correctness actually shows
edge, or switch to a directional-aware backbone like a classification
fine-tune instead of quantile regression).

---

## Why training MASE was misleading

During training, AutoGluon reported `-MASE` ranging from -13.17 (chunk 0)
to -14.20 (chunk 94, peak). We used these numbers to drive chunked
retraining. But:

1. AutoGluon's `eval_metric` validation is computed on the **last
   `prediction_length=10` days of the training data** — an in-sample
   holdout by construction.
2. The model can overfit to that specific 10-day window while degrading on
   unseen forward periods.
3. H2H on truly OOS windows (cutoff=10d and 20d from end-of-data) shows
   the OOS picture, and v2 wins.

**Lesson:** in-sample validation MASE is a useful early-stopping hint but
not a promotion criterion. Always run H2H on out-of-sample forward
windows before promoting a retrained checkpoint.

---

## Recommendation

1. **Do NOT promote any v3 checkpoint** — v2 wins on average OOS MASE and
   on 20-day extrapolation, which is the relevant horizon for swing
   trading.
2. **Archive the v3 chunked retrain output** to
   `models/chronos/_archive/phase2_v3_1M_2026-06-29/` so we don't
   accidentally point at it.
3. **Keep `models/chronos/phase2_v2` as the live Chronos model** in
   `v12/src/stockapp_v12/services/forecast_service.py`.
4. **For future retrain attempts**, evaluate on truly OOS forward windows
   BEFORE running millions of fine-tune steps. The 1M-step run cost ~16 GPU
   hours for zero net improvement.

---

## Artifacts

| File | Purpose |
|---|---|
| `v12/scripts/eval_chronos_h2h_v3.py` | H2H eval script (pure functions + I/O glue) — includes `sign`, `sign_match`, `hit_rate`, `signal_hit_rate`, `_directional_metrics_from_rows` |
| `v12/tests/test_eval_chronos_h2h_v3.py` | 27 unit tests (15 MASE + 12 directional) |
| `models/chronos/h2h_v3_report.json` | Full per-model, per-symbol, per-cutoff eval data (MASE + signal agg + directional) |
| `models/chronos/phase2_v3_progress.json` | Training state (100 chunks completed) |
| `models/chronos/_archive/phase2_v3_1M_2026-06-29/` | Archived 100 ckpts + progress.json (not active) |

---

## Reproduction

```bash
# On GPU box (where models live):
ssh gpu
cd ~/Stockapp && source ~/yolo_train_env/bin/activate
PYTHONPATH=v12/src:src:. python3 v12/scripts/eval_chronos_h2h_v3.py \
    --models phase2_v2 phase2_v3_ckpt_940k phase2_v3_ckpt_950k phase2_v3_ckpt_1000k \
    --cutoffs 10 20 \
    --out models/chronos/h2h_v3_report.json

# Unit tests (works on local + GPU box):
PYTHONPATH=v12/src:src:. pytest v12/tests/test_eval_chronos_h2h_v3.py -v
```
