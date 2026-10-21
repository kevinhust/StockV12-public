#!/bin/bash
# run_shadow_ensemble.sh — daily A+B ensemble shadow harness entry point.
#
# Called by WSL cron at 16:10 NYC (20:10 UTC) Mon-Fri, 5 minutes after the
# single-model shadow loop finishes. Same data window, same 10-symbol set,
# but runs N predictors per symbol and writes:
#   - models/chronos/_shadow/ensemble/YYYY-MM-DD.json (ensemble result)
#   - models/chronos/_shadow/ensemble/per_model/{label}/YYYY-MM-DD.json (raw per-model)
#   - models/chronos/_shadow/ensemble/SUMMARY.json (accumulated stats)
#
# No trades. The whole point is to accumulate live spread / fallback /
# hit_rate evidence before promoting the ensemble backend to Tier 2 paper.
#
# Host-agnostic: STOCKAPP_ROOT and PYTHON env vars override the auto-detected
# defaults below. Default detection uses the runtime user's home (works on
# both GPU box `trainer` and NUC `app`).
#
# Manual invocation (for ad-hoc backfills):
#   TZ=America/New_York /home/$(whoami)/Stockapp/scripts/run_shadow_ensemble.sh
# Or test against a specific date:
#   TODAY_OVERRIDE=2026-07-01 /home/$(whoami)/Stockapp/scripts/run_shadow_ensemble.sh
#
# Cron install:
#   /home/$(whoami)/Stockapp/scripts/install_shadow_ensemble_cron.sh
#
# Why 16:10 NYC (vs single-model shadow's 16:05):
#   - Runs AFTER shadow_trade.py so per-model JSONs exist as a cross-check.
#   - Adds 2 min for N×10 model loads + N×10 predicts (vs single-model's
#     2×10). Worst-case measured at ~90s on RTX 3060 with 2 models.
#
# Models monitored (both currently pass ADR-008 walk-forward gate):
#   - phase2_v2                (Variant A — the more-conservative baseline)
#   - E05_p3_coldstart_seed42  (Variant B — ADR-008 candidate)
#
# Output:
#   - stdout/stderr → /tmp/shadow_ensemble_cron.log
#   - per-date JSONs → $STOCKAPP_ROOT/models/chronos/_shadow/ensemble/...

set -euo pipefail

# Paths
# Auto-detect STOCKAPP_ROOT from runtime user (works on GPU box `trainer` and NUC `app`).
# Override with STOCKAPP_ROOT env var if the repo lives elsewhere.
STOCKAPP_ROOT="${STOCKAPP_ROOT:-/home/$(whoami)/Stockapp}"
# In-repo venv (created via `python3 -m venv $STOCKAPP_ROOT/.venv`).
# Override with PYTHON env var to use a different interpreter.
PYTHON="${PYTHON:-$STOCKAPP_ROOT/.venv/bin/python}"
LOG_FILE="${LOG_FILE:-/tmp/shadow_ensemble_cron.log}"

# Colon-separated model paths — passed via env var to shadow_ensemble.py
# (matches CHRONOS_ENSEMBLE_MODEL_PATHS in EnsembleChronosBackend env contract).
# Defaults match the Round 3 closure A+B pair.
ENSEMBLE_MODEL_PATHS_DEFAULT="$STOCKAPP_ROOT/models/chronos/phase2_v2:$STOCKAPP_ROOT/models/chronos/E05_p3_coldstart_seed42"
export CHRONOS_ENSEMBLE_MODEL_PATHS="${CHRONOS_ENSEMBLE_MODEL_PATHS:-$ENSEMBLE_MODEL_PATHS_DEFAULT}"

# 3% spread threshold matches EnsembleChronosBackend.DEFAULT_FALLBACK_SPREAD_PCT
export CHRONOS_ENSEMBLE_FALLBACK_SPREAD_PCT="${CHRONOS_ENSEMBLE_FALLBACK_SPREAD_PCT:-3.0}"

SYMBOLS_CSV="${SYMBOLS_CSV:-AAPL NVDA TSLA JPM MSFT AMZN AMD META GOOGL SPY}"
THRESHOLD_PCT="${THRESHOLD_PCT:-0.5}"
CUTOFF_DAYS_AGO="${CUTOFF_DAYS_AGO:-1}"

# Force NYC time for all date math in this script
export TZ="${TZ:-America/New_York}"

# Redirect all output to log
exec >> "$LOG_FILE" 2>&1

echo ""
echo "================================================================"
echo "[shadow_ensemble_cron] $(date '+%Y-%m-%d %H:%M:%S %Z')  $$"
echo "================================================================"

# Weekend skip (markets closed Sat=6, Sun=7)
DOW=$(date +%u)
if [[ "$DOW" -ge 6 ]]; then
    echo "[shadow_ensemble_cron] weekend ($(date '+%A')) — skip"
    exit 0
fi

# Pre-market guard: only run at or after 16:10 NYC
HHMM=$(date +%H%M)
if [[ "$HHMM" < "1610" ]]; then
    echo "[shadow_ensemble_cron] too early ($(date '+%H:%M %Z'), before 16:10 NYC) — skip"
    exit 0
fi

cd "$STOCKAPP_ROOT"
# shellcheck source=source_archive_free_env.sh
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${_SCRIPT_DIR}/source_archive_free_env.sh"
PYTHON="${PYTHON:-$STOCKAPP_ROOT/.venv/bin/python}"

# Convert space-separated CSV to repeated args
SYMBOLS_ARGS=()
for s in $SYMBOLS_CSV; do
    SYMBOLS_ARGS+=("$s")
done

FINAL_ARGS=(
    --symbols "${SYMBOLS_ARGS[@]}"
    --cutoff-days-ago "$CUTOFF_DAYS_AGO"
    --fallback-spread-pct "$CHRONOS_ENSEMBLE_FALLBACK_SPREAD_PCT"
    --threshold "$THRESHOLD_PCT"
)
if [[ -n "${TODAY_OVERRIDE:-}" ]]; then
    FINAL_ARGS+=(--today "$TODAY_OVERRIDE")
    echo "[shadow_ensemble_cron] TODAY_OVERRIDE=$TODAY_OVERRIDE (manual backfill mode)"
fi

# Round 4: filter to champion only. CHALLENGER_CSV is excluded from ensemble.
CHAMPION="${CHAMPION_CSV:-E07_long_ctx}"
for m in $CHAMPION; do
    MODEL_DIR="$STOCKAPP_ROOT/models/chronos/_shadow/$m"
    [ -d "$MODEL_DIR" ] || continue
    for f in "$MODEL_DIR"/2*.json; do
        [ -f "$f" ] || continue
        echo "  [$m] $(basename "$f" .json)"
    done
done

echo "[shadow_ensemble_cron] models: $CHRONOS_ENSEMBLE_MODEL_PATHS"
echo "[shadow_ensemble_cron] fallback_spread_pct: $CHRONOS_ENSEMBLE_FALLBACK_SPREAD_PCT"
echo "[shadow_ensemble_cron] cmd: $PYTHON scripts/shadow_ensemble.py ${FINAL_ARGS[*]}"

"$PYTHON" scripts/shadow_ensemble.py "${FINAL_ARGS[@]}"

EXIT=$?
echo "[shadow_ensemble_cron] exit=$EXIT  ($(date '+%H:%M:%S %Z'))"
echo "[shadow_ensemble_cron] per-date JSON: $STOCKAPP_ROOT/models/chronos/_shadow/ensemble/$(date +%Y-%m-%d).json"

# Best-effort summary of accumulated stats
SUMMARY="$STOCKAPP_ROOT/models/chronos/_shadow/ensemble/SUMMARY.json"
if [[ -f "$SUMMARY" ]]; then
    echo "[shadow_ensemble_cron] ensemble summary (one-liner):"
    "$PYTHON" -c "
import json, sys
try:
    d = json.load(open('$SUMMARY'))
    agg = (d.get('accumulated') or {}).get('aggregate') or {}
    spread_stats = agg.get('spread_stats') or {}
    today_agg = d.get('current_run_aggregate') or {}
    print(f'  n_dates={d[\"accumulated\"][\"n_dates\"]} '
          f'n_rows_today={today_agg.get(\"n_rows\")} '
          f'fallback_today={today_agg.get(\"n_used_fallback\")} '
          f'hit_rate_accum={agg.get(\"hit_rate\",{}).get(\"hit_rate\")} '
          f'spread_p50={spread_stats.get(\"p50\")} '
          f'spread_p95={spread_stats.get(\"p95\")} '
          f'fallback_rate_accum={agg.get(\"fallback_rate\")}')
except Exception as e:
    print(f'  [error reading SUMMARY] {e}', file=sys.stderr)
" || true
fi

exit $EXIT