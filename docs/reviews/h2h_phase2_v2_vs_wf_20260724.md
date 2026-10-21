# H2H: phase2_v2 vs phase2_wf_20260724 (ADR-010 D3)

**Date:** 2026-07-26  
**Host:** GPU (`ssh gpu`)  
**Symbols:** AAPL NVDA TSLA JPM MSFT AMZN AMD META GOOGL SPY  
**Cutoffs:** 10d, 20d  
**Raw JSON:** [h2h_phase2_v2_vs_wf_20260724.json](h2h_phase2_v2_vs_wf_20260724.json)

## Summary

| Metric (mean across cutoffs) | phase2_v2 | phase2_wf_20260724 | Edge |
|---|---:|---:|---|
| MASE (↓ better) | 2.370 | **2.354** | wf slight |
| hit_rate (↑) | **0.45** | 0.45 | tie |
| signal_hit_rate (↑) | 0.50 | **0.548** | wf slight |

Neither model reaches the historical Stage-1 **diagnostic red-flag line** hit_rate ≥ 0.53
(Gate Decision Architecture **layer D** — not L0/L1/L2; does not alone block trading or path change).

## By cutoff

| Cutoff | Model | MASE | hit_rate | signal_hit | n_signals |
|---:|---|---:|---:|---:|---:|
| 10 | **phase2_v2** | **1.91** | **0.50** | **0.75** | 4 |
| 10 | phase2_wf_20260724 | 2.03 | 0.30 | 0.43 | 7 |
| 20 | phase2_v2 | 2.83 | 0.40 | 0.25 | 4 |
| 20 | **phase2_wf_20260724** | **2.68** | **0.60** | **0.67** | 6 |

## Verdict (C) — diagnostic only (layer D)

- Offline H2H is **mixed**: baseline wins recent-tail (10d), wf wins longer holdout (20d).
- **D red-flag**: weak absolute hit; **not** an L2 reject by itself.
- Decision path unchanged (A+B). `phase2_wf_20260724` remains **observation only**.
- Path change later: `scripts/evaluate_path_change.py` (L2) → `READY_FOR_HUMAN` + human only.
- Accumulate daily shadow ≥15–20d, then L2 + E-EVAL — not Stage-1 absolute hit alone.

## Retrain context

- Recipe: `config/chronos_phase2_v2_recipe.yaml` (steps=1000, lr=1e-5, batch=32, ctx=512)
- Cutoff: 2026-07-24 inclusive, 203 symbols, ~82s GPU
- Live baseline `phase2_v2` untouched
