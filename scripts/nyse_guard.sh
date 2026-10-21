#!/usr/bin/env bash
# nyse_guard.sh — exit 0 if today is an NYSE trading day, exit 1 otherwise.
#
# Source this at the top of any cron wrapper script to skip holidays:
#   source "$(dirname "$0")/nyse_guard.sh" || exit 0
#
# Or call directly:
#   if bash "$(dirname "$0")/nyse_guard.sh"; then ... fi

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# Resolve PYTHONPATH relative to repo root (parent of scripts/)
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHONPATH="${REPO_ROOT}/src:${PYTHONPATH:-}" \
    TZ="America/New_York" \
    python -m stockapp_v12.core.nyse_calendar
