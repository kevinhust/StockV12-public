#!/usr/bin/env bash
# Tier2 passthrough publisher (filename kept for crontab compatibility).
#
# CAPABILITY: PRODUCTION_PASSTHROUGH — NOT a catalyst scorer.
# Real catalyst scoring is unimplemented. This job publishes:
#   watchlist:tier2 := members of watchlist:tier1 (score=1.0)
# and logs L2_DEGRADED so operators can grep for honesty.
#
# Required so entry_window has candidates. See docs/PRODUCTION_CAPABILITIES.md.

set -euo pipefail

export REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
export REDIS_PORT="${REDIS_PORT:-6380}"
export TZ="America/New_York"

LOG_DIR="${LOG_DIR:-/home/app/Stockapp/logs/pipeline}"
mkdir -p "$LOG_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="$0"
# shellcheck source=source_archive_free_env.sh
source "${SCRIPT_DIR}/source_archive_free_env.sh" 2>/dev/null || true
source "${SCRIPT_DIR}/nyse_guard.sh" || exit 0
# shellcheck source=_preflight.sh
PRE_RC=0
source "${SCRIPT_DIR}/_preflight.sh" || PRE_RC=$?
if [ "$PRE_RC" -ne 0 ]; then
  exit 0
fi

LOG_FILE="$LOG_DIR/catalyst_scorer_$(date +%Y%m%d).log"
echo "=== Tier2Passthrough start $(date -Iseconds) ===" >> "$LOG_FILE"
echo "CAPABILITY=PRODUCTION_PASSTHROUGH NOT_A_CATALYST_SCORER" >> "$LOG_FILE"
TIER1=$(redis-cli -h ${REDIS_HOST} -p ${REDIS_PORT} ZCARD watchlist:tier1 2>/dev/null || echo unknown)
echo "Tier1 size: $TIER1" >> "$LOG_FILE"
exec >> "$LOG_FILE" 2>&1

TIER1_MEMBERS="$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" ZRANGE watchlist:tier1 0 -1 2>/dev/null || true)"

if [ -z "${TIER1_MEMBERS}" ]; then
  echo "L2_DEGRADED reason=no_tier1_members action=tier2=empty ts=$(date -Iseconds)"
  echo "=== Tier2Passthrough exit=0 (no tier1) $(date -Iseconds) ==="
  exit 0
fi

RC=0
redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" DEL watchlist:tier2 >> "$LOG_FILE" 2>&1 || RC=$?
if [ "$RC" -ne 0 ]; then
  echo "ERROR redis DEL watchlist:tier2 failed rc=$RC"
  echo "=== Tier2Passthrough exit=$RC $(date -Iseconds) ==="
  exit "$RC"
fi

PROMOTED=0
while IFS= read -r sym; do
  [ -z "$sym" ] && continue
  redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" ZADD watchlist:tier2 1.0 "$sym" >> "$LOG_FILE" 2>&1 || RC=$?
  PROMOTED=$((PROMOTED + 1))
done <<EOF_TIER1
${TIER1_MEMBERS}
EOF_TIER1

TIER2_CARD="$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" ZCARD watchlist:tier2 2>/dev/null || echo unknown)"
echo "Promoted ${PROMOTED} symbols tier1→tier2 (passthrough). tier2 ZCARD=${TIER2_CARD}"
echo "L2_DEGRADED reason=catalyst_filter_unimplemented action=tier2=tier1_passthrough promoted=${PROMOTED} ts=$(date -Iseconds)"
echo "=== Tier2Passthrough exit=0 $(date -Iseconds) ==="
exit 0
