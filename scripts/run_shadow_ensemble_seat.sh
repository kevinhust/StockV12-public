#!/bin/bash
# run_shadow_ensemble_seat.sh — ADR-010 E-SEAT side-car ensemble (NO trade path).
#
# Writes to models/chronos/_shadow/ensemble_seat_*/ only.
# Does NOT touch models/chronos/_shadow/ensemble/ (A+B decision path).
#
# Default seat: phase2_v2 + E09_higher_lr (E-EVAL 20d quality leader).
# Optional cron (after main ensemble, e.g. 16:12 ET):
#   12 16 * * 1-5 /home/app/Stockapp/scripts/run_shadow_ensemble_seat.sh
#
# Env overrides:
#   SEAT_MODEL_PATHS=colon-separated absolute or basenames under models/chronos/
#   SEAT_DIR_NAME=ensemble_seat_e09
#   TODAY_OVERRIDE=YYYY-MM-DD

set -euo pipefail

STOCKAPP_ROOT="${STOCKAPP_ROOT:-/home/$(whoami)/Stockapp}"
PYTHON="${PYTHON:-$STOCKAPP_ROOT/.venv/bin/python}"
LOG_FILE="${LOG_FILE:-/tmp/shadow_ensemble_seat_cron.log}"
export TZ="${TZ:-America/New_York}"

SEAT_DIR_NAME="${SEAT_DIR_NAME:-ensemble_seat_e09}"
SEAT_A="${SEAT_A:-phase2_v2}"
SEAT_B="${SEAT_B:-E09_higher_lr}"
SEAT_MODEL_PATHS="${SEAT_MODEL_PATHS:-$STOCKAPP_ROOT/models/chronos/$SEAT_A:$STOCKAPP_ROOT/models/chronos/$SEAT_B}"

exec >> "$LOG_FILE" 2>&1

echo ""
echo "================================================================"
echo "[ensemble_seat] $(date '+%Y-%m-%d %H:%M:%S %Z')  $$"
echo "================================================================"

DOW=$(date +%u)
if [[ "$DOW" -ge 6 ]]; then
    echo "[ensemble_seat] weekend — skip"
    exit 0
fi

cd "$STOCKAPP_ROOT"
# shellcheck source=source_archive_free_env.sh
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${_SCRIPT_DIR}/source_archive_free_env.sh"
PYTHON="${PYTHON:-$STOCKAPP_ROOT/.venv/bin/python}"

# Convert colon paths to CLI args
IFS=':' read -r -a MODEL_ARR <<< "$SEAT_MODEL_PATHS"
ARGS=(--seat --ensemble-dir-name "$SEAT_DIR_NAME" --models "${MODEL_ARR[@]}")
if [[ -n "${TODAY_OVERRIDE:-}" ]]; then
    ARGS+=(--today "$TODAY_OVERRIDE")
    echo "[ensemble_seat] TODAY_OVERRIDE=$TODAY_OVERRIDE"
fi

echo "[ensemble_seat] dir=$SEAT_DIR_NAME models=${SEAT_MODEL_PATHS}"
echo "[ensemble_seat] cmd: $PYTHON scripts/shadow_ensemble.py ${ARGS[*]}"

"$PYTHON" scripts/shadow_ensemble.py "${ARGS[@]}"
EXIT=$?
echo "[ensemble_seat] exit=$EXIT ($(date '+%H:%M:%S %Z'))"
exit $EXIT
