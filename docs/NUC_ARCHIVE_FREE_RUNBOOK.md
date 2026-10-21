# NUC Archive-Free Runtime (Phase 4)

> **Goal:** Production PYTHONPATH does **not** include `archive/`.  
> Archive access only via `stockapp_v12.adapters.legacy_v11.*` when still required,  
> or fully off via `STOCKAPP_DISABLE_V11_ARCHIVE=1` once native paths cover all jobs.

## Env flags

| Variable | Effect |
|----------|--------|
| `STOCKAPP_DISABLE_V11_ARCHIVE=1` | `ensure_v11_archive_on_path()` returns False; no `sys.path` mutation. Chronos/Longbridge/HardRisk legacy factories soft-fail to None. |

## Recommended NUC rollout

1. **Dry-run (1 session)**  
   Export `STOCKAPP_DISABLE_V11_ARCHIVE=1` on a paper-only host/copy.  
   Confirm shadow/cron logs: Longbridge/Chronos degrade soft; decision path still uses ensemble if models are local.

2. **Trim PYTHONPATH**  
   Cron wrappers must use root paths only (never `v12/` or `archive/`):
   ```bash
   # Prefer: source scripts/source_archive_free_env.sh
   export PYTHONPATH="/home/app/Stockapp/src:/home/app/Stockapp/scripts"
   export STOCKAPP_DISABLE_V11_ARCHIVE=1
   ```
   On NUC: `scripts/source_archive_free_env.sh` + `config/archive_free_soak.env`.

3. **Gate (ops)**  
   Collect ≥5 trading days of green NUC shadow + paper HaltGate with archive off path  
   before deleting archive from the machine (repo `archive/` may remain as git history).

4. **Still caged (not deleted)**  
   - `adapters/legacy_v11/chronos.py`  
   - `adapters/legacy_v11/longbridge.py`  
   - `adapters/legacy_v11/risk.py`  

   Production injects MarketData/Chronos when possible so these are unused.

## Live soak log (NUC <nuc-host>)

| Field | Value |
|-------|--------|
| **Started** | 2026-07-26 (day-0) |
| **Status file** | `/home/app/Stockapp/logs/archive_free_soak/STATUS.md` |
| **Day-0 smoke** | **PASS** — imports, PYTHONPATH guard, wiring, disable flag |
| **ensure_v11** | Returns `False` (no useful archive tree on NUC disk) |
| **Crons patched** | shadow / ensemble / halt / swing_scanner / daily_review / seat |
| **Days remaining** | Fill checklist in STATUS.md after each Mon–Fri 16:05–16:20 window |

> Full “5 trading days green” is **ops calendar time**. Day-0 setup is complete; mark days 1–5 after each close.
## Env contract smoke (NUC / before shadow)

```bash
# Full production check (PYTHONPATH + scripts package + archive-off + Redis)
bash scripts/check_cron_env_contract.sh --with-redis

# Cron (recommended on NUC):
#   @reboot sleep 15 && .../check_cron_env_contract.sh --with-redis >> logs/cron_env_contract.log 2>&1
#   50 7 * * 1-5 .../check_cron_env_contract.sh --with-redis >> logs/cron_env_contract.log 2>&1
```

Catches Day-1 class bugs: `No module named 'scripts'`, dirty `v12/` path, `DISABLE≠1`, Redis 6380 down.

## Verification commands (dev)

```bash
# services must not call ensure_v11 / from src.
rg -n 'ensure_v11_archive_on_path|from src\.' src/stockapp_v12/services

# disable flag (include repo root for scripts package)
STOCKAPP_DISABLE_V11_ARCHIVE=1 PYTHONPATH=src:scripts:. \
  python -c "from stockapp_v12.core._v11_compat import ensure_v11_archive_on_path as e; assert e() is False"

pytest tests/test_phase4_legacy_cage.py tests/test_cron_pythonpath_contract.py -q
```
