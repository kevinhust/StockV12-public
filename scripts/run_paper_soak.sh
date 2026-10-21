#!/usr/bin/env bash
#
# scripts/run_paper_soak.sh (post-migration 2026-07-09)
#
# P6 / Phase 4 paper soak launcher (Task 11 / 4.0).
#
# Sets up env vars from config/paper_soak.yaml, verifies model files
# + redis reachability, then starts the lifecycle_scheduler in the
# foreground with paper_ibkr mode.
#
# Usage:
#     bash scripts/run_paper_soak.sh
#     bash scripts/run_paper_soak.sh --checklist-only
#     bash scripts/run_paper_soak.sh --duration 5
#
# This wrapper intentionally lives in the repo (not just in the container)
# so the prep report and human-run checklist can both reference it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
CONFIG="$REPO_ROOT/config/paper_soak.yaml"

CHECKLIST_ONLY=false
DURATION_DAYS=5
LOG_FILE="/tmp/stockapp_paper_soak.log"

usage() {
    cat <<EOF
Usage: $0 [options]

Options:
    --checklist-only    Run the SOAK checklist and exit (no scheduler start)
    --duration N        Soak duration in days (informational; default 5)
    --log PATH          Log file path (default: /tmp/stockapp_paper_soak.log)
    -h, --help          Show this help

Environment (set by this wrapper from $CONFIG):
    STOCKAPP_V12_RISK
    STOCKAPP_V12_ENTRY_ENABLED
    REDIS_HOST (defaults: redis)
    REDIS_PORT (defaults: 6379)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --checklist-only) CHECKLIST_ONLY=true; shift ;;
        --duration)       DURATION_DAYS="$2"; shift 2 ;;
        --log)            LOG_FILE="$2"; shift 2 ;;
        -h|--help)        usage; exit 0 ;;
        *)                echo "Unknown arg: $1" >&2; usage; exit 2 ;;
    esac
done

echo "=== P6 Paper Soak launcher ==="
echo "Repo root:        $REPO_ROOT"
echo "Config:           $CONFIG"
echo "Log file:         $LOG_FILE"
echo "Duration (info):  $DURATION_DAYS days"
echo

# 1. Verify config exists and is loadable
if [[ ! -f "$CONFIG" ]]; then
    echo "ERROR: soak config not found at $CONFIG" >&2
    exit 2
fi
python3 -c "import yaml; yaml.safe_load(open('$CONFIG'))" || {
    echo "ERROR: $CONFIG is not valid YAML" >&2; exit 2;
}
echo "[1/4] config/paper_soak.yaml loads OK"

# 2. Set env vars from config (risk section → live env)
export STOCKAPP_V12_RISK="${STOCKAPP_V12_RISK:-1}"
export STOCKAPP_V12_ENTRY_ENABLED="${STOCKAPP_V12_ENTRY_ENABLED:-1}"
export REDIS_HOST="${REDIS_HOST:-redis}"
export REDIS_PORT="${REDIS_PORT:-6379}"

echo "[2/4] env vars set:"
echo "    STOCKAPP_V12_RISK=$STOCKAPP_V12_RISK"
echo "    STOCKAPP_V12_ENTRY_ENABLED=$STOCKAPP_V12_ENTRY_ENABLED"
echo "    REDIS_HOST=$REDIS_HOST REDIS_PORT=$REDIS_PORT"

# 3. Run the checklist (model files + env vars + redis connectivity)
echo
echo "[3/4] running SOAK checklist..."
if ! PYTHONPATH="$REPO_ROOT/src:$REPO_ROOT/src:." \
     python3 "$REPO_ROOT/scripts/paper_soak_checklist.py" \
     --config "$CONFIG" \
     --days "$DURATION_DAYS" \
     | tee "$LOG_FILE.checklist"; then
    rc=$?
    if [[ $rc -eq 1 ]]; then
        echo
        echo "WARNING: SOAK checklist returned 1 (one or more checks failed)."
        if [[ "$CHECKLIST_ONLY" == "true" ]]; then
            exit 1
        fi
        echo "Continue anyway? (y/N)"
        read -r ans
        [[ "$ans" == "y" || "$ans" == "Y" ]] || exit 1
    else
        echo "ERROR: checklist aborted (exit $rc)" >&2
        exit "$rc"
    fi
fi

# 4. Start scheduler (unless --checklist-only)
if [[ "$CHECKLIST_ONLY" == "true" ]]; then
    echo
    echo "Checklist-only mode; scheduler not started."
    exit 0
fi

echo
echo "[4/4] starting lifecycle_scheduler (foreground, log → $LOG_FILE)"
cd "$REPO_ROOT"
# Phase 4 archive-free: lifecycle_scheduler resolves via repo src/ (or
# stockapp_v12 adapters for V11-only modules). Do NOT put archive/ on PYTHONPATH.
# Optional: source config/archive_free_soak.env for soak flags.
if [[ -f "$REPO_ROOT/config/archive_free_soak.env" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/config/archive_free_soak.env"
fi
# Repo root required so `from scripts.*` resolves under python scripts/*.py
export PYTHONPATH="$REPO_ROOT/src:${REPO_ROOT}/scripts:${REPO_ROOT}"
if [[ "${PYTHONPATH}" == *archive* ]] || [[ "${PYTHONPATH}" == *"/v12/"* ]]; then
  echo "ERROR: PYTHONPATH must not include archive/ or v12/ — refuse to start soak" >&2
  exit 1
fi
python3 -m src.services.lifecycle_scheduler 2>&1 | tee "$LOG_FILE"
