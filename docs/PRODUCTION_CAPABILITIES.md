# Production Capabilities (honest matrix)

> **Updated:** 2026-07-28  
> Single source of truth for what NUC cron **actually** does vs marketing names.

| Capability | Cron / entry | Status | Notes |
|------------|--------------|--------|-------|
| Swing scan → tier1 | `run_swing_scanner.sh` | **Production** | Writes Redis tier1 |
| Tier2 for entry | `run_catalyst_scorer.sh` | **Production passthrough** | **Not a catalyst scorer.** Copies tier1→tier2 with `L2_DEGRADED`. Needed so entry has candidates. |
| Entry eval | `run_entry_window.sh` → `python -m stockapp_v12.app.entry_cron` | **Paper wired** (2026-07-28 ABC) | Composition root is **`app.entry_cron`** (not `pipeline.entry_window` main). Wires PaperBroker + LiveOrderGuard (fail-closed KillSwitch, paper mode whitelist) + bars from `data/features/daily`. Requires tier2 + D1 features; Chronos when `V12_LOAD_CHRONOS=1`. `V12_ENTRY_WIRED=0` or `--visibility-only` → log only. **Live modes blocked.** |
| Daily review | `run_daily_review.sh` | **DEGRADED** | `price_provider` currently returns `None` → skips all positions (no HOLD/TRIM/EXIT). |
| Trailing stop ratchet | ~~`run_trailing_stop_check.sh`~~ | **DISABLED** | Script kept for history; **not installed** by `install_pipeline_cron.sh`. Protective SL remains at broker/bracket. |
| Shadow observation | `run_shadow_cron.sh` / ensemble / seat / halt | **Production observation** | Does not change decision path without human promote |
| Live broker money | — | **Out of scope** until paper soak + trail policy decided |
| Cron env contract | `check_cron_env_contract.sh` | **Production** | PYTHONPATH + archive-off + optional Redis; `@reboot` + 07:50 ET |
| Shadow feature refresh | `run_refresh_shadow_features.sh` (16:00 ET) | **Production (Longbridge)** | Rebuilds shadow-10 features on **NUC** via `longport` SDK + `adapters/marketdata/longbridge_daily`. Before shadow 16:05. |

## Operator rules

1. Grep `L2_DEGRADED` in catalyst logs → you are **not** scoring catalysts.  
2. Do not re-enable trailing cron until a live/EOD price feed is wired and tested.  
3. Install crons only from **repo-root** `scripts/install_*.sh` (never `v12/scripts/`).  
4. Safety defaults for paper path: see [SAFETY_LIVE_GUARD.md](SAFETY_LIVE_GUARD.md) (fail-closed + paper whitelist).  

## Related

- [SCHEDULER.md](SCHEDULER.md)  
- [DEPLOYMENT.md](DEPLOYMENT.md)  
- [NUC_ARCHIVE_FREE_RUNBOOK.md](NUC_ARCHIVE_FREE_RUNBOOK.md)  
- [SAFETY_LIVE_GUARD.md](SAFETY_LIVE_GUARD.md)  
