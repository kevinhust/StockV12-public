#!/usr/bin/env bash
# TrailingStopCheck — DISABLED (not a production capability).
#
# STATUS: DISABLED (2026-07-26)
# Reason: no live/EOD price feed wired; prior stub was a silent no-op that
# misled operators into thinking trail ratchet ran every 5 minutes.
#
# Protective stop remains the broker/paper bracket SL at entry.
# Re-enable only after implementing PositionManager trail tick + price provider
# and re-adding the job in install_pipeline_cron.sh + PRODUCTION_CAPABILITIES.md.
#
# If invoked (legacy crontab), exit 0 after logging so cron does not spam.

set -euo pipefail

export TZ="${TZ:-America/New_York}"
LOG_DIR="${LOG_DIR:-/home/app/Stockapp/logs/pipeline}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/trailing_stop_$(date +%Y%m%d).log"

echo "=== TrailingStopCheck DISABLED $(date -Iseconds) ===" >> "$LOG_FILE"
echo "CAPABILITY=DISABLED reason=no_price_feed; use broker SL only" >> "$LOG_FILE"
echo "=== TrailingStopCheck exit=0 (disabled) $(date -Iseconds) ===" >> "$LOG_FILE"
exit 0
