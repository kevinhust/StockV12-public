#!/usr/bin/env bash
# SwingScanner — 16:30 ET on NYSE trading days.
# Writes watchlist:tier1 (ZSET) + watchlist:tier1:details (HASH) to Redis.
# Idempotent: re-running same day overwrites the same keys per SCHEDULER.md.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=source_archive_free_env.sh
source "${SCRIPT_DIR}/source_archive_free_env.sh"

# NUC host redis is on 6380 (port 6379 has a stuck WSL2 reservation).
export REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
export REDIS_PORT="${REDIS_PORT:-6380}"
export TZ="America/New_York"

LOG_DIR="${STOCKAPP_ROOT}/logs/pipeline"
mkdir -p "$LOG_DIR"

SCRIPT_NAME="$0"
# P1-10 (council 2026-07-09): skip NYSE holidays and weekends.
source "${SCRIPT_DIR}/nyse_guard.sh" || exit 0
# shellcheck source=_preflight.sh
PRE_RC=0
source "${SCRIPT_DIR}/_preflight.sh" || PRE_RC=$?
if [ "$PRE_RC" -ne 0 ]; then
  # preflight already wrote a skip.log line with the reason; exit cleanly
  exit 0
fi

LOG_FILE="$LOG_DIR/swing_scanner_$(date +%Y%m%d).log"
echo "=== SwingScanner start $(date -Iseconds) ===" >> "$LOG_FILE"
exec >> "$LOG_FILE" 2>&1

cd "${STOCKAPP_ROOT}"
"${PYTHON}" -m stockapp_v12.services.swing_scanner
RC=$?
echo "=== SwingScanner exit=$RC $(date -Iseconds) ===" >> "$LOG_FILE"
exit $RC
