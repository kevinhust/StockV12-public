#!/usr/bin/env bash
# scripts/run_quarterly_review_reminder.sh
# Writes a log entry when day-55 of a champion's tenure approaches.
# Idempotent; safe to run daily.

set -e
STOCKAPP_ROOT="${STOCKAPP_ROOT:-/home/$(whoami)/Stockapp}"
STATE_DIR="$STOCKAPP_ROOT/models/chronos/_shadow/_state"
CONFIG="$STATE_DIR/champion_config.json"
REMINDER_LOG="$STOCKAPP_ROOT/logs/quarterly_review_reminder.txt"

[ -f "$CONFIG" ] || { echo "[reminder] no champion_config.json"; exit 0; }

NEXT_REVIEW=$(python3 -c "import json; print(json.load(open('$CONFIG'))['next_review_date'])")
TODAY=$(TZ=America/New_York date +%Y-%m-%d)

# Compute days until review
DAYS_UNTIL=$(python3 -c "
from datetime import date
review = date.fromisoformat('$NEXT_REVIEW')
today = date.fromisoformat('$TODAY')
delta = (review - today).days
print(delta)
")

if [ "$DAYS_UNTIL" -le 5 ] && [ "$DAYS_UNTIL" -ge 0 ]; then
    mkdir -p "$(dirname "$REMINDER_LOG")"
    echo "[$TODAY] Quarterly review in $DAYS_UNTIL days (review date: $NEXT_REVIEW)" >> "$REMINDER_LOG"
fi