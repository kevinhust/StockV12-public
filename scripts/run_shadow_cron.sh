#!/bin/bash
# run_shadow_cron.sh — daily shadow-trade harness entry point.
#
# Called by WSL cron at 16:05 NYC (20:05 UTC) Mon-Fri. Predicts using
# yesterday's data (cutoff=1) for the configured candidate + live baseline,
# appends per-date JSONs to models/chronos/_shadow/{model}/YYYY-MM-DD.json,
# refreshes cross-model SUMMARY.json. No-op on weekends (markets closed).
#
# Host-agnostic: STOCKAPP_ROOT and PYTHON env vars override the auto-detected
# defaults below. Default detection uses the runtime user's home (works on
# both GPU box `trainer` and NUC `app`).
#
# Manual invocation (for ad-hoc backfills):
#   TZ=America/New_York /home/$(whoami)/Stockapp/scripts/run_shadow_cron.sh
# Or test against a specific date:
#   TODAY_OVERRIDE=2026-06-30 /home/$(whoami)/Stockapp/scripts/run_shadow_cron.sh
#
# Cron install:
#   /home/$(whoami)/Stockapp/scripts/install_shadow_cron.sh
#
# Why 16:05 NYC, not 16:00 exactly:
#   - Market closes 16:00 NYC. Need 1 min buffer for the daily-bar source
#     (cache_d1_bars.py) to publish today's close to data/features/daily/.
#   - Shadow's forecast uses today's close as the "current_close" anchor
#     and tomorrow's actual closes as ground truth — if run before 16:05
#     the actuals are still yesterday's, contaminating the day-1 trade.
#
# Models monitored (both currently pass ADR-008 walk-forward gate):
#   - phase2_v2                (live baseline, since Round 1)
#   - E05_p3_coldstart_seed42  (ADR-008 candidate, awaiting ≥30 days evidence)
#
# Output:
#   - stdout/stderr → /tmp/shadow_cron.log (rotated manually if it gets big)
#   - per-date JSONs → $STOCKAPP_ROOT/models/chronos/_shadow/{model}/YYYY-MM-DD.json
#   - cross-model SUMMARY → $STOCKAPP_ROOT/models/chronos/_shadow/SUMMARY.json

set -euo pipefail

# Paths
# Auto-detect STOCKAPP_ROOT from runtime user (works on GPU box `trainer` and NUC `app`).
# Override with STOCKAPP_ROOT env var if the repo lives elsewhere.
STOCKAPP_ROOT="${STOCKAPP_ROOT:-/home/$(whoami)/Stockapp}"
# In-repo venv (created via `python3 -m venv $STOCKAPP_ROOT/.venv`).
# Override with PYTHON env var to use a different interpreter.
PYTHON="${PYTHON:-$STOCKAPP_ROOT/.venv/bin/python}"
LOG_FILE="${LOG_FILE:-/tmp/shadow_cron.log}"
# NOTE: E05 seeds {17,23,71} dropped — Chronos fine-tuning is deterministic,
# different seeds produce identical weights. Keeping seed42 (old script) +
# seed7 (new script) as 2 distinct checkpoints.
#
# Active observation set (2026-07-26 ROI review):
#   - Dropped zeroshot_bolt_small from daily cron (gate FAIL / MASE; weekly-ref only)
#   - E07 retained as observation only (not for promotion)
#   - Ensemble remains phase2_v2 + E05_p3_coldstart_seed42 (unchanged)
# Champion (baseline) + Challenger (ADR candidate). halt_swap.json can override.
STATE_DIR="${STATE_DIR:-$STOCKAPP_ROOT/models/chronos/_shadow/_state}"
SWAP_FILE="$STATE_DIR/halt_swap.json"
CHAMPION_CSV_DEFAULT="phase2_v2"
CHALLENGER_CSV_DEFAULT="E05_p3_coldstart_seed42"
CHAMPION_CSV="${CHAMPION_CSV:-$CHAMPION_CSV_DEFAULT}"
CHALLENGER_CSV="${CHALLENGER_CSV:-$CHALLENGER_CSV_DEFAULT}"

# Full daily observation set (not just champion/challenger pair).
# phase2_wf_20260724: ADR-010 D3 WFO retrain (cutoff 2026-07-24) — observation only, not decision path
OBSERVATION_CSV_DEFAULT="phase2_v2 phase2_wf_20260724 E05_p3_coldstart_seed42 E05_p3_coldstart_seed7 E06_short_ctx E07_long_ctx E08_fewer_steps E09_higher_lr E10_more_steps"

# halt_swap.json override — latest swap wins (roles only; observation set stays full).
if [ -f "$SWAP_FILE" ]; then
    SWAP_CHAMPION=$(python3 -c "import json; print(json.load(open('$SWAP_FILE'))['active_champion'])" 2>/dev/null || true)
    SWAP_HALTED=$(python3 -c "import json; print(json.load(open('$SWAP_FILE'))['halted_champion'])" 2>/dev/null || true)
    if [ -n "$SWAP_CHAMPION" ]; then
        echo "[cron] halt_swap detected: champion=$SWAP_CHAMPION halted=$SWAP_HALTED"
        CHAMPION_CSV="$SWAP_CHAMPION"
        CHALLENGER_CSV="$SWAP_HALTED"
    fi
fi

MODELS_CSV="${MODELS_CSV:-$OBSERVATION_CSV_DEFAULT}"
export CHAMPION_CSV CHALLENGER_CSV
SYMBOLS_CSV="${SYMBOLS_CSV:-AAPL NVDA TSLA JPM MSFT AMZN AMD META GOOGL SPY}"
THRESHOLD_PCT="${THRESHOLD_PCT:-0.5}"

# Force NYC time for all date math in this script
export TZ="${TZ:-America/New_York}"

# Redirect all output to log (cron captures stdout to MAILTO by default,
# we want it on disk so we can inspect when needed)
exec >> "$LOG_FILE" 2>&1

echo ""
echo "================================================================"
echo "[shadow_cron] $(date '+%Y-%m-%d %H:%M:%S %Z')  $$"
echo "================================================================"

# Weekend skip (markets closed Sat=6, Sun=7)
DOW=$(date +%u)
if [[ "$DOW" -ge 6 ]]; then
    echo "[shadow_cron] weekend ($(date '+%A')) — skip"
    exit 0
fi

# Pre-market guard: only run at or after 16:05 NYC
HHMM=$(date +%H%M)
if [[ "$HHMM" < "1605" ]]; then
    echo "[shadow_cron] too early ($(date '+%H:%M %Z'), before 16:05 NYC) — skip"
    exit 0
fi

cd "$STOCKAPP_ROOT"
# shellcheck source=source_archive_free_env.sh
SCRIPT_DIR_FOR_ENV="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_FOR_ENV}/source_archive_free_env.sh"
# Re-apply explicit PYTHON if cron set a custom interpreter above
PYTHON="${PYTHON:-$STOCKAPP_ROOT/.venv/bin/python}"

# ADR-010 E-DATA: feature freshness preflight (log + soft-fail by default).
# FEATURE_FRESHNESS_STRICT=1 aborts the cron when features are stale/missing.
FEATURES_DIR="${FEATURES_DIR:-$STOCKAPP_ROOT/data/features/daily}"
if [[ -x "$PYTHON" && -f "$STOCKAPP_ROOT/scripts/check_feature_freshness.py" ]]; then
    echo "[shadow_cron] E-DATA feature freshness check → $FEATURES_DIR"
    if ! "$PYTHON" "$STOCKAPP_ROOT/scripts/check_feature_freshness.py" \
        --features-dir "$FEATURES_DIR" \
        --max-staleness-days "${FEATURE_MAX_STALENESS_DAYS:-1}"; then
        echo "[shadow_cron] WARN: feature freshness check failed (stale/missing bars)"
        if [[ "${FEATURE_FRESHNESS_STRICT:-0}" == "1" ]]; then
            echo "[shadow_cron] FEATURE_FRESHNESS_STRICT=1 — aborting"
            exit 1
        fi
    fi
fi

# Build CLI args
CLI_ARGS=(
    --models $MODELS_CSV
    --predict-today
    --threshold "$THRESHOLD_PCT"
    --out "$STOCKAPP_ROOT/models/chronos/_shadow/SUMMARY.json"
)
# Allow caller to override today (for manual backfills / tests)
if [[ -n "${TODAY_OVERRIDE:-}" ]]; then
    CLI_ARGS+=(--today "$TODAY_OVERRIDE")
    echo "[shadow_cron] TODAY_OVERRIDE=$TODAY_OVERRIDE (manual backfill mode)"
fi

# Convert space-separated CSV to repeated args (need to handle this carefully
# because of shell quoting — nargs='+' expects separate tokens)
MODELS_ARGS=()
for m in $MODELS_CSV; do
    MODELS_ARGS+=("$m")
done
SYMBOLS_ARGS=()
for s in $SYMBOLS_CSV; do
    SYMBOLS_ARGS+=("$s")
done

# Final arg list — overwrite the --models from CLI_ARGS with the split version
FINAL_ARGS=(
    --models "${MODELS_ARGS[@]}"
    --symbols "${SYMBOLS_ARGS[@]}"
    --predict-today
    --threshold "$THRESHOLD_PCT"
    --out "$STOCKAPP_ROOT/models/chronos/_shadow/SUMMARY.json"
)
if [[ -n "${TODAY_OVERRIDE:-}" ]]; then
    FINAL_ARGS+=(--today "$TODAY_OVERRIDE")
fi

echo "[shadow_cron] cmd: $PYTHON scripts/shadow_trade.py ${FINAL_ARGS[*]}"

"$PYTHON" scripts/shadow_trade.py "${FINAL_ARGS[@]}"

EXIT=$?
echo "[shadow_cron] exit=$EXIT  ($(date '+%H:%M:%S %Z'))"
echo "[shadow_cron] per-date JSONs: $STOCKAPP_ROOT/models/chronos/_shadow/{phase2_v2,E05_p3_coldstart_seed42}/$(date +%Y-%m-%d).json"

# Best-effort longitudinal health (ADR-008) — not L2 path-change / promote
if [[ -f "$STOCKAPP_ROOT/models/chronos/_shadow/SUMMARY.json" ]]; then
    echo "[shadow_cron] longitudinal health (SUMMARY.json):"
    "$PYTHON" -c "
import json
d = json.load(open('$STOCKAPP_ROOT/models/chronos/_shadow/SUMMARY.json'))
gates = d.get('longitudinal_health_by_model') or d.get('promotion_gates') or {}
for name, gate in gates.items():
    status = 'HEALTH_OK' if gate.get('cleared') else 'HEALTH_FAIL'
    reasons = '' if gate.get('cleared') else ' — ' + '; '.join(gate.get('reasons') or [])
    print(f'  [{status}] {name}{reasons}')
" || true
fi

exit $EXIT