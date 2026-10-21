#!/usr/bin/env bash
# DailyReview — 16:15 ET on NYSE trading days.
# Reads positions from PositionManager, applies POSITION_LIFECYCLE.md matrix,
# writes pending_action + journal entries.
# P2-spec: apply_immediately defaults to False (spec-compliant: execution
# happens next session via pending_action consumer). This script does not
# opt in to immediate execution.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=source_archive_free_env.sh
source "${SCRIPT_DIR}/source_archive_free_env.sh"

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
  exit 0
fi

LOG_FILE="$LOG_DIR/daily_review_$(date +%Y%m%d).log"
echo "=== DailyReview start $(date -Iseconds) ===" >> "$LOG_FILE"
exec >> "$LOG_FILE" 2>&1

cd "${STOCKAPP_ROOT}"
"${PYTHON}" -c "
import os, redis
from stockapp_v12.services.position_manager import PositionManager
from stockapp_v12.services.daily_review import DailyReview
from stockapp_v12.pipeline.journal import TradeJournal

r = redis.Redis(host=os.environ['REDIS_HOST'], port=int(os.environ['REDIS_PORT']), decode_responses=True)
journal = TradeJournal(redis_client=r)
pm = PositionManager(redis_client=r, journal=journal)

def last_close(symbol):
    return None

review = DailyReview(position_manager=pm, journal=journal, price_provider=last_close)
modes = [pm._mode] if pm._mode and pm._mode != 'all' else ['paper', 'live']
decisions = 0
for mode in modes:
    for pos in pm.list_open_positions(mode=mode):
        d = review.review(pos.symbol)
        if d:
            decisions += 1
print(f'DailyReview decisions={decisions}')
"
RC=$?
echo "=== DailyReview exit=$RC $(date -Iseconds) ===" >> "$LOG_FILE"
exit $RC
