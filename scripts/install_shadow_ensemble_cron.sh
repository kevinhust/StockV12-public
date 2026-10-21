#!/bin/bash
# install_shadow_ensemble_cron.sh — idempotent install of ensemble-shadow daily cron.
#
# What it does:
#   - Adds TZ=America/New_York to crontab (NYC time is the truth, see
#     StockApp-V12/rules). Without this WSL cron's "16:10" is in UTC.
#   - Adds a Mon-Fri 16:10 NYC cron entry that calls run_shadow_ensemble.sh.
#   - Re-running is a no-op (idempotent — checks for existing line first).
#   - Uncomment DRY_RUN=1 to print what would be added without touching crontab.
#
# Host-agnostic: defaults to /home/$(whoami)/Stockapp (works on GPU box
# `trainer` and NUC `app`). Override with STOCKAPP_ROOT env var to install
# elsewhere.
#
# Why a separate cron from install_shadow_cron.sh:
#   - run_shadow_ensemble.sh runs 5 min AFTER run_shadow_cron.sh so the
#     single-model per-date JSONs exist as a cross-check for the ensemble.
#   - Keeping them as separate crons means the single-model shadow keeps
#     running even if the ensemble cron is uninstalled.
#
# Uninstall:
#   crontab -l | grep -v "run_shadow_ensemble.sh" | crontab -
#   (TZ=America/New_York line is shared with the single-model shadow cron;
#    leave it alone if the other cron is still installed.)
#
# Per-project path: lives in Stockapp/scripts/ but installed into the
# target host's crontab (GPU box WSL or NUC WSL — not the Mac mini's).

set -euo pipefail

# Force NYC for the script's own date math
export TZ=America/New_York

# Auto-detect repo root from runtime user (works on GPU box `trainer` and NUC `app`).
# Override with STOCKAPP_ROOT env var if the repo lives elsewhere.
STOCKAPP_ROOT="${STOCKAPP_ROOT:-/home/$(whoami)/Stockapp}"

# The actual cron entry — fires 16:10 NYC Mon-Fri (cron uses local TZ per
# the TZ= line above), calls run_shadow_ensemble.sh with no env override
# so it uses defaults (NYC, today's date, A+B ensemble pair).
CRON_LINE="10 16 * * 1-5 $STOCKAPP_ROOT/scripts/run_shadow_ensemble.sh"
TZ_LINE="TZ=America/New_York"

DRY_RUN="${DRY_RUN:-0}"

# Verify the wrapper exists before installing
WRAPPER="$STOCKAPP_ROOT/scripts/run_shadow_ensemble.sh"
if [[ ! -x "$WRAPPER" ]]; then
    echo "ERROR: $WRAPPER not executable. Run: chmod +x $WRAPPER" >&2
    exit 1
fi

# Read current crontab (empty if none)
CURRENT=$(crontab -l 2>/dev/null || echo "")

# Idempotency check — already installed?
if echo "$CURRENT" | grep -qF "run_shadow_ensemble.sh"; then
    echo "[install_shadow_ensemble_cron] already installed — nothing to do"
    echo "Current shadow-ensemble entry:"
    echo "$CURRENT" | grep -E "(TZ=America/New_York|run_shadow_ensemble)" || true
    exit 0
fi

# Build the new crontab (preserve any TZ= line that's already there)
NEW=""
if ! echo "$CURRENT" | grep -qF "$TZ_LINE"; then
    NEW+="${TZ_LINE}
"
fi
NEW+="$CURRENT
"
# Only append if not already there (defensive — the grep above should catch it)
if ! echo "$NEW" | grep -qF "run_shadow_ensemble.sh"; then
    NEW+="${CRON_LINE}
"
fi

if [[ "$DRY_RUN" == "1" ]]; then
    echo "[install_shadow_ensemble_cron] DRY RUN — would install:"
    echo "--- new crontab ---"
    echo "$NEW"
    echo "--- end ---"
    exit 0
fi

echo "$NEW" | crontab -

echo "[install_shadow_ensemble_cron] installed"
echo "Current crontab:"
crontab -l