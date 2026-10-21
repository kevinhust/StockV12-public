#!/usr/bin/env bash
# install_pipeline_cron.sh — register V12 production jobs (repo-root scripts/).
#
# Canonical path: /home/app/Stockapp/scripts/  (NOT v12/scripts/)
#
# Job table (US/Eastern) — see docs/PRODUCTION_CAPABILITIES.md:
#   16:30  swing_scanner
#   08:00  tier2_passthrough   (legacy name run_catalyst_scorer.sh — NOT a scorer)
#   09:35 + */5 10-15  entry_window
#   16:15  daily_review
#   trailing_stop_check  — REMOVED from install (DISABLED; SL at broker only)
#
# Run with --dry to print, --yes to install. Re-run is idempotent (overwrites).
# Use --uninstall to remove only the entries managed by this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TZ_LINE="TZ=America/New_York"
TAG="# v12-pipeline"

NL=$'\n'
CRON_BLOCK="${TZ_LINE}${NL}${NL}# --- swing_scanner (16:30 ET, Mon-Fri) ---${NL}30 16 * * 1-5 ${SCRIPT_DIR}/run_swing_scanner.sh ${TAG}${NL}# --- tier2 passthrough (08:00 ET) — NOT catalyst scoring; keeps entry_window fed ---${NL}0 8 * * 1-5 ${SCRIPT_DIR}/run_catalyst_scorer.sh ${TAG}${NL}# --- entry_window (09:35 + every 5 min 10:00–15:30 ET) ---${NL}35 9 * * 1-5 ${SCRIPT_DIR}/run_entry_window.sh ${TAG}${NL}*/5 10-15 * * 1-5 ${SCRIPT_DIR}/run_entry_window.sh ${TAG}${NL}# --- daily_review (16:15 ET, Mon-Fri) ---${NL}15 16 * * 1-5 ${SCRIPT_DIR}/run_daily_review.sh ${TAG}${NL}# trailing_stop_check intentionally NOT installed (DISABLED — see PRODUCTION_CAPABILITIES.md)"

DRY=0
INSTALL=0
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --dry) DRY=1 ;;
        --yes) INSTALL=1 ;;
        --uninstall) UNINSTALL=1 ;;
        *) echo "Unknown arg: $arg" >&2; exit 2 ;;
    esac
done

CURRENT="$(crontab -l 2>/dev/null || true)"

if [[ "$UNINSTALL" -eq 1 ]]; then
    FILTERED="$(printf "%s\n" "$CURRENT" | grep -v "v12-pipeline" || true)"
    if [[ -n "$FILTERED" ]]; then
        printf "%s\n" "$FILTERED" | sed "/^$/d" | crontab - 2>/dev/null || true
    fi
    echo "Removed v12-pipeline entries. Remaining crontab:"
    crontab -l
    exit 0
fi

if [[ "$DRY" -eq 1 ]]; then
    echo "----- Proposed cron block -----"
    printf "%s\n" "$CRON_BLOCK"
    echo "----- Current crontab -----"
    printf "%s\n" "$CURRENT"
    exit 0
fi

if [[ "$INSTALL" -ne 1 ]]; then
    echo "Usage: $0 [--dry | --yes | --uninstall]" >&2
    exit 2
fi

# Strip any prior v12-pipeline lines, keep everything else, append new block.
KEPT="$(printf "%s\n" "$CURRENT" | grep -v "v12-pipeline" | sed "/^$/d" || true)"

if [[ -n "$KEPT" ]]; then
    NEW="${KEPT}
${CRON_BLOCK}"
else
    NEW="${CRON_BLOCK}"
fi

printf "%s\n" "$NEW" | crontab -
echo "Installed. New crontab:"
crontab -l
