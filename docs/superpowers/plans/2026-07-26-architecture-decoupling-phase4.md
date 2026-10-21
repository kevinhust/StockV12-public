# Architecture Decoupling — Phase 4 (code complete; ops soak pending)

**Goal:** Confine all V11 archive imports to `adapters/legacy_v11/`; allow archive-free PYTHONPATH; support kill-switch env.

## Delivered (code)

| Item | Detail |
|------|--------|
| `STOCKAPP_DISABLE_V11_ARCHIVE` | `ensure_v11_archive_on_path()` no-ops |
| `legacy_v11/longbridge.py` | Sole Longbridge archive gateway |
| `legacy_v11/risk.py` | HardRiskGatekeeper load for tests |
| `legacy_v11/chronos.py` | Already Phase 1 |
| `SwingScanner` | No ensure_v11 / `from src.` |
| scripts | `cache_d1_bars`, `validate_longbridge_screener` use cage |
| Tests | `tests/test_phase4_legacy_cage.py` |
| Runbook | `docs/NUC_ARCHIVE_FREE_RUNBOOK.md` |

## Ops remaining

- ≥5 trading days NUC green with archive **off** PYTHONPATH
- Then drop archive from cron env permanently
