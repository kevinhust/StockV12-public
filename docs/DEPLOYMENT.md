# StockApp V12 — Deployment Topology

> **Date:** 2026-07-10 (added after Round-2 council caught the gap)
> **Status:** Canonical deployment reference. Cross-link from SYSTEM_OVERVIEW, README, ENVIRONMENT.

## TL;DR

The V12 **production** trading pipeline runs on the **NUC (Linux WSL on Windows)** at `/home/app/Stockapp/`. The Mac mini (`/Users/dev/Stockapp/`) is the dev environment where unit tests are run. The GPU box is a separate machine for CUDA training only.

| Role | Host | OS | Repo path | Redis | Python |
|------|------|-----|-----------|-------|--------|
| **Production (live cron + paper-soak)** | **NUC** | Linux 6.x under WSL2 on Windows | `/home/app/Stockapp/` | port **6380** (WSL2 quirk; 6379 has a stuck reservation) | `/home/app/Stockapp/.venv/bin/python` |
| Dev / test (where `pytest` runs) | Mac mini (Apple Silicon) | macOS 14+ | `/Users/dev/Stockapp/` | `127.0.0.1:6379` (local) | `python3` via pyenv (3.12.9) |
| Training (CUDA only) | GPU box | Linux 6.x under WSL2 on Windows | `/home/trainer/Stockapp/` (minimal subset) | n/a | `/home/trainer/yolo_train_env/bin/python` |

**Why this matters:** the runner scripts (`scripts/run_*.sh`) hardcode `/home/app/Stockapp/...` because that's the production target. They are **not expected to run on the Mac mini** — the Mac mini is the dev/test surface, not the deploy target.

## What runs on the NUC

The NUC runs the live cron table installed by **repo-root** `scripts/install_pipeline_cron.sh`  
(canonical path: `/home/app/Stockapp/scripts/` — **not** `v12/scripts/`).

| Cron (ET) | Script | What it does |
|-----------|--------|--------------|
| 16:30 Mon-Fri | `run_swing_scanner.sh` | Daily scan → `watchlist:tier1` |
| 08:00 Mon-Fri | `run_catalyst_scorer.sh` | **Tier2 passthrough only** (not a scorer); see [PRODUCTION_CAPABILITIES.md](PRODUCTION_CAPABILITIES.md) |
| 09:35 Mon-Fri | `run_entry_window.sh` | First entry eval of the day |
| 10:00–15:30 every 5 min | `run_entry_window.sh` | Intraday entry pulse |
| 16:15 Mon-Fri | `run_daily_review.sh` | HOLD/TRIM/EXIT decisions |
| — | `run_trailing_stop_check.sh` | **DISABLED** — not installed; broker SL only |

**Path converge (after pull on NUC):**

```bash
cd ~/Stockapp && git pull
bash scripts/nuc_converge_to_root_scripts.sh --yes
```

Honest capability matrix: [PRODUCTION_CAPABILITIES.md](PRODUCTION_CAPABILITIES.md).

Each script:
1. Sources `nyse_guard.sh` to skip holidays + half-days (P1-10)
2. Sources `_preflight.sh` to verify Redis reachable, kill switch not engaged, venv imports OK, journal schema valid
3. Exits cleanly (`exit 0`) on preflight failure, writing a `SKIP — <reason>` line to `logs/pipeline/skip.log`

## NUC WSL specifics (gotchas)

These are NUC-specific and do **not** apply to the Mac mini dev environment:

1. **Redis on port 6380**, not 6379. WSL2 has a stuck port-6379 reservation on the Windows host; the NUC runs redis-server on 6380. All scripts default `REDIS_PORT=6380`. Override via env var.
2. **Linux user is `app`** (not `trainer` — that's the GPU box). Repo lives at `/home/app/Stockapp/`. venv at `/home/app/Stockapp/.venv/`.
3. **Cron runs under the `app` user.** `crontab -l` from the NUC shell to inspect.
4. **NYSE-calendar guard** (`scripts/nyse_guard.sh`) is sourced at the top of every runner. It uses `stockapp_v12.core.nyse_calendar.is_trading_day` which falls back to a built-in 2024–2028 holiday table if `exchange_calendars` isn't installed.
5. **WSL2 clock drift** — WSL2 has had historical issues with the system clock falling behind after Windows sleep. NUC's `chronyc tracking` should show <100ms offset. If entries stop firing, this is the first thing to check.

## Deployment procedure (from Mac mini to NUC)

The repo lives on both the Mac mini and the NUC. Code changes flow Mac → NUC; the NUC runs the live crons.

### Routine: code change

```bash
# On Mac mini: commit + push
git add -A && git commit -m "..." && git push

# On NUC (via SSH)
ssh app@nuc
cd ~/Stockapp
git pull
# Re-run install_pipeline_cron.sh --yes if a new script was added
bash scripts/install_pipeline_cron.sh --yes
```

### One-time: initial deploy

```bash
# On Mac mini
scp scripts/nuc_post_migration_refresh.sh app@nuc:~/Stockapp/scripts/
ssh app@nuc 'bash ~/Stockapp/scripts/nuc_post_migration_refresh.sh'
```

The `nuc_post_migration_refresh.sh` script:
1. Removes any crontab entries that still reference the old `v12/` directory layout (pre-2026-07-09 migration).
2. Re-runs `install_pipeline_cron.sh` idempotently.
3. Prints the resulting crontab and verifies no `v12/` references remain.

### One-time: venv setup on NUC

```bash
ssh app@nuc
cd ~/Stockapp
python3.12 -m venv .venv
source .venv/bin/activate
pip install -U pip wheel
pip install -e .
# Or for the V11 bridge:
# pip install exchange_calendars  # optional; built-in holiday table covers 2024-2028
```

## Preflight check (what runs before every cron)

`_preflight.sh` is sourced at the top of every runner. It does four checks, all side-effect-free:

1. **Redis reachable** — `redis-cli -h 127.0.0.1 -p 6380 PING == "PONG"`
2. **KillSwitch not engaged** — `system:halt` key is absent
3. **Python venv imports OK** — `/home/app/Stockapp/.venv/bin/python -c "import stockapp_v12"` exits 0
4. **TradeJournal schema valid** — sample `journal:{date}:*` record has `schema_version=1`

On any failure, it writes a `SKIP — <reason>` line to `logs/pipeline/skip.log` and exits non-zero. The runner then does `exit 0` (not the preflight's non-zero) so cron doesn't spam alert emails. The skip log is the operator's signal.

### Daily feature refresh (shadow universe, on NUC)

**Primary source: Longbridge OpenAPI** (`longport` SDK) — not yfinance.

```bash
# one-time
.venv/bin/pip install 'longport>=3.0.0' 'pyarrow>=7,<21'
# .env must have LONGBRIDGE_APP_KEY / APP_SECRET / ACCESS_TOKEN

# manual
source scripts/source_archive_free_env.sh
set -a; source .env; set +a
"$PYTHON" scripts/refresh_shadow_features.py
# or
bash scripts/run_refresh_shadow_features.sh
```

Native client: `stockapp_v12.adapters.marketdata.longbridge_daily` (no V11 archive).  
API caps daily klines (~500/request); script spaces symbol calls to avoid 301607.

Cron (before shadow 16:05 ET):

```cron
0 16 * * 1-5 /home/app/Stockapp/scripts/run_refresh_shadow_features.sh
```

Log: `logs/feature_refresh_YYYYMMDD.log`.

### Env contract smoke (Day-1 regression guard)

Separately from per-job preflight, run **`scripts/check_cron_env_contract.sh --with-redis`** on boot and before the session:
```bash
# Manual
bash scripts/check_cron_env_contract.sh --with-redis

# NUC crontab (example)
@reboot sleep 15 && /home/app/Stockapp/scripts/ensure_redis_6380.sh >> .../logs/redis-ensure.log 2>&1
@reboot sleep 30 && /home/app/Stockapp/scripts/check_cron_env_contract.sh --with-redis >> .../logs/cron_env_contract.log 2>&1
50 7 * * 1-5 /home/app/Stockapp/scripts/check_cron_env_contract.sh --with-redis >> .../logs/cron_env_contract.log 2>&1
```

Checks: `PYTHONPATH` includes repo root (not only `src:scripts`), no `archive/`/`v12/`, `STOCKAPP_DISABLE_V11_ARCHIVE=1`, cron-style `from scripts.*` import, `stockapp_v12` + `ensure_v11() is False`, Redis 6380.

## Observability

The NUC writes structured logs to `/home/app/Stockapp/logs/pipeline/`:

- `entry_window_YYYYMMDD.log` — per-day entry eval trace (every 5 min, 09:35–15:30)
- `swing_scanner_YYYYMMDD.log` — daily scan output
- `catalyst_scorer_YYYYMMDD.log` — daily tier1→tier2 (passthrough)
- `daily_review_YYYYMMDD.log` — daily review decisions
- `skip.log` — preflight failures (one line per skip)

These are rsync'd back to the Mac mini weekly for review.

## What the previous docs got wrong

- **`docs/ENVIRONMENT.md`** (2026-06-29) is a training-env-only inventory. The "Mac mini" entries are about the dev machine for the Round-1 Chronos retrain; they are not about live deployment. The doc was last updated before the council review and the WSL→NUC production split.
- **`docs/SYSTEM_OVERVIEW.md:46`** said "single-host scheduler on Mac Mini" — **wrong**, should be "single-host scheduler on NUC (Linux WSL on Windows)". Fixed 2026-07-10.
- **`docs/architecture/00-ASSET_INVENTORY.md:54,149`** use `/Users/dev/Stockapp/` — these are **dev** paths, not production. The doc mostly describes the dev environment; the NUC prod paths are documented here.

## What runs on the GPU box (for completeness)

The GPU box is a separate machine for CUDA training only:
- It does **not** run the trading pipeline.
- It does **not** run paper-soak.
- It is used by `scripts/run_experiment.py` for the Round-1 Chronos fine-tunes.
- It has a minimal subset of the repo (no `src/`, no `config/`, no `.claude/`, no `Plans.md`).
- See `docs/ENVIRONMENT.md` for the training-env details.

## Related docs

- `docs/SYSTEM_OVERVIEW.md` — architecture + module ownership (now references NUC, not Mac mini)
- `docs/ENVIRONMENT.md` — training-env inventory (dev machine + GPU box)
- `docs/SCHEDULER.md` — job schedule + failure matrix
- `docs/SAFETY_LIVE_GUARD.md` — R-rule enforcement + LiveOrderGuard
- `docs/architecture/00-ASSET_INVENTORY.md` — file map
- `docs/architecture/01-COUNCIL-LOG.md` — NUC shadow crons reference
- `docs/architecture/06-POST-LAUNCH-ROADMAP.md` — NUC monitoring cadence
- `scripts/nuc_post_migration_refresh.sh` — one-time deploy / refresh
- `scripts/install_pipeline_cron.sh` — idempotent cron installer
