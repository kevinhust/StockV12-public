# Architecture Decoupling — Phase 5 (executed)

**Goal:** Thin ops surface — no `sys.path` folklore; installable CLI entrypoints.

## Delivered

| Item | Detail |
|------|--------|
| `stockapp_v12.app.bootstrap` | Single `ensure_src_on_path()` — never adds archive/ |
| `stockapp_v12.app.cli` | `demo_wiring`, `check_boundaries`, `main` |
| `pyproject.toml` | setuptools package + console_scripts |
| Scripts converted | champion_evaluation, compare_shadow_models, evaluate_path_change, shadow_halt_decision, cache_d1_bars, demo_wiring_runtime, run_paper_soak (archive-free) |
| Tests | `tests/test_phase5_bootstrap.py` |

## Install

```bash
pip install -e .
stockapp-demo-wiring
stockapp-check-boundaries
```

## NUC soak note

Day-0 archive-free soak started 2026-07-26 on NUC; fill 5 trading-day checklist in  
`/home/app/Stockapp/logs/archive_free_soak/STATUS.md`.
