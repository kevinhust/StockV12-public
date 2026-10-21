#!/usr/bin/env bash
# EntryWindow — 09:35 ET (and every 5 min until 15:30 ET) on NYSE trading days.
# P0-3 (council 2026-07-09): the live entry path was dead because the V11
# scheduler read `actionable:today` (a Redis LIST removed in hygiene-I) and
# hardcoded `market_regime=BULL_LONG`. This script wires the new V12 entry
# path that reads `watchlist:tier2` (ZSET) + `market_regime:current` and
# routes through `EntryOrchestrator.evaluate_and_maybe_enter`.
#
# Cron block (registered by install_pipeline_cron.sh):
#   35 9 * * 1-5  scripts/run_entry_window.sh
#   */5 10-15 * * 1-5 scripts/run_entry_window.sh
#   0-30/5 16 * * 1-5 scripts/run_entry_window.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=source_archive_free_env.sh
source "${SCRIPT_DIR}/source_archive_free_env.sh"

# NUC host redis is on 6380 (port 6379 has a stuck WSL2 reservation).
export REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
export REDIS_PORT="${REDIS_PORT:-6380}"
export TZ="America/New_York"
# V12 mode: paper or live. Default paper for safety.
export V12_MODE="${V12_MODE:-paper_ibkr}"

LOG_DIR="${LOG_DIR:-${STOCKAPP_ROOT}/logs/pipeline}"
mkdir -p "$LOG_DIR"

# Phase A2: full preflight (kill switch + redis + python imports + journal schema).
SCRIPT_NAME="$0"
# P1-10 (council 2026-07-09): skip NYSE holidays and weekends.
source "${SCRIPT_DIR}/nyse_guard.sh" || exit 0
# shellcheck source=_preflight.sh
# `|| PRE_RC=$?` so set -e does not abort before intentional skip handling
PRE_RC=0
source "${SCRIPT_DIR}/_preflight.sh" || PRE_RC=$?
if [ "$PRE_RC" -ne 0 ]; then
  # preflight already wrote a skip.log line with the reason; exit cleanly
  exit 0
fi

LOG_FILE="$LOG_DIR/entry_window_$(date +%Y%m%d).log"
echo "=== EntryWindow start mode=$V12_MODE ts=$(date -Iseconds) ===" >> "$LOG_FILE"
exec >> "$LOG_FILE" 2>&1

cd "${STOCKAPP_ROOT}"
# Composition root lives in app/ (not pipeline) to keep import boundaries clean.
"${PYTHON}" -m stockapp_v12.app.entry_cron \
  --mode "$V12_MODE" \
  ${EW_DRY_RUN:+--dry-run}
RC=$?
echo "=== EntryWindow exit=$RC ts=$(date -Iseconds) ===" >> "$LOG_FILE"
exit $RC
