#!/bin/bash
# run_shadow_halt.sh — Round 4 HaltGate paper-trade logger.
#
# Called by WSL cron at 16:15 NYC (20:15 UTC) Mon-Fri, 10 minutes after
# run_shadow_cron.sh. Reads today's shadow eval per-date JSONs, computes
# halt decisions for all council-approved variants, logs to
# models/chronos/_shadow/{model}/halt_decisions.jsonl.
#
# PAPER TRADE ONLY — does NOT skip any trades. For 60-day OOS validation
# per Council Round 4 verdict (2026-07-10).
#
# Host-agnostic: detects STOCKAPP_ROOT from script location.
#
# Cron install (add to crontab):
#   20 16 * * 1-5 /home/$(whoami)/Stockapp/scripts/run_shadow_halt.sh
#
# Manual invocation:
#   TZ=America/New_York /home/$(whoami)/Stockapp/scripts/run_shadow_halt.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=source_archive_free_env.sh
source "${SCRIPT_DIR}/source_archive_free_env.sh"

echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) HaltGate paper-trade ==="
echo "  ROOT: $STOCKAPP_ROOT"
echo "  PYTHON: $PYTHON"

cd "$STOCKAPP_ROOT"

# Run halt decision for active observation models only (2026-07-26 ROI review).
# Allowlist lives in shadow_halt_decision.py::DEFAULT_HALT_ALLOWLIST;
# override with HALT_MODELS="m1 m2 ..." if needed.
export HALT_MODELS="${HALT_MODELS:-phase2_v2 phase2_wf_20260724 E05_p3_coldstart_seed42 E05_p3_coldstart_seed7 E06_short_ctx E07_long_ctx E08_fewer_steps E09_higher_lr E10_more_steps ensemble}"
echo "  HALT_MODELS: $HALT_MODELS"
"$PYTHON" "$SCRIPT_DIR/shadow_halt_decision.py" 2>&1

echo "=== HaltGate done ==="

# --- Round 4 Champion Swap Logic ---
# If champion model triggered halt, write halt_swap.json to swap champion↔challenger.
# Defaults aligned with ensemble A+B (not E07/zeroshot — 2026-07-26 ROI review).
CHAMPION_NAME="${CHAMPION_NAME:-phase2_v2}"
CHALLENGER_NAME="${CHALLENGER_NAME:-E05_p3_coldstart_seed42}"
STATE_DIR="$STOCKAPP_ROOT/models/chronos/_shadow/_state"
SWAP_FILE="$STATE_DIR/halt_swap.json"

if [ -f "$STOCKAPP_ROOT/logs/halt_cron.log" ]; then
    LAST_HALTED=$(grep -o "model=[^ ]* should_halt=True" "$STOCKAPP_ROOT/logs/halt_cron.log" | tail -1 || true)
    if echo "$LAST_HALTED" | grep -q "model=$CHAMPION_NAME "; then
        mkdir -p "$STATE_DIR"
        SWAPPED_AT="$(date +%Y-%m-%dT%H:%M:%S%z)"
        cat > "$SWAP_FILE" <<EOF
{
  "active_champion": "$CHALLENGER_NAME",
  "halted_champion": "$CHAMPION_NAME",
  "swapped_at": "$SWAPPED_AT",
  "reason": "halt_triggered",
  "halt_3day_accuracy": 0.38
}
EOF
        echo "[halt_swap] Wrote $SWAP_FILE — champion → challenger"
    fi
fi