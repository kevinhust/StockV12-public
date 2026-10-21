#!/bin/bash
# nuc_post_migration_refresh.sh
#
# Post v12→root promotion (2026-07-09) cron refresh for NUC (and GPU box).
#
# Usage (from Mac):
#   scp scripts/nuc_post_migration_refresh.sh app@your-nuc-ip:~/Stockapp/scripts/
#   ssh app@your-nuc-ip 'bash ~/Stockapp/scripts/nuc_post_migration_refresh.sh'
#
# Or run directly on the NUC after cd ~/Stockapp.
#
# What it does:
#   1. Removes any crontab lines that still reference the old /v12/ layout.
#   2. Re-runs the idempotent install_*_cron.sh scripts (now using root layout).
#   3. Prints current crontab and a final "any remaining v12?" check.
#
# Safe to run multiple times.

set -euo pipefail

STOCKAPP_ROOT="${STOCKAPP_ROOT:-/home/$(whoami)/Stockapp}"
cd "$STOCKAPP_ROOT"

echo "[NUC refresh] STOCKAPP_ROOT=$STOCKAPP_ROOT"
echo "[NUC refresh] Cleaning old v12/ references from crontab..."

CURRENT=$(crontab -l 2>/dev/null || echo "")

# Remove any line containing the old v12 layout (both absolute and $STOCKAPP_ROOT forms)
CLEANED=$(echo "$CURRENT" | grep -v '/v12/' | grep -v '\$STOCKAPP_ROOT/v12/' || true)

if [[ "$CURRENT" != "$CLEANED" ]]; then
    echo "$CLEANED" | crontab -
    echo "[NUC refresh] Removed stale v12/ cron lines."
else
    echo "[NUC refresh] No stale v12/ lines found."
fi

echo
echo "[NUC refresh] Re-installing cron jobs from updated scripts..."

for installer in \
    scripts/install_shadow_cron.sh \
    scripts/install_shadow_ensemble_cron.sh \
    scripts/install_pipeline_cron.sh
do
    if [[ -x "$STOCKAPP_ROOT/$installer" ]]; then
        echo "  -> $installer"
        STOCKAPP_ROOT="$STOCKAPP_ROOT" bash "$STOCKAPP_ROOT/$installer" || true
    else
        echo "  (skip) $installer not executable or missing"
    fi
done

echo
echo "[NUC refresh] Current crontab:"
crontab -l 2>/dev/null || echo "(empty)"

echo
echo "[NUC refresh] Final check for any remaining /v12/ references in crontab:"
if crontab -l 2>/dev/null | grep -i '/v12/' ; then
    echo "WARNING: still see v12/ paths above — manual edit needed."
else
    echo "OK — no v12/ paths left in crontab."
fi

echo
echo "[NUC refresh] Done. You may also want to:"
echo "  - Check paper soak / daily review entries if they are not installed via the above scripts."
echo "  - Run: grep -r '/v12/' ~/Stockapp/scripts/ ~/Stockapp/config/ 2>/dev/null || true"
echo "  - Reboot or 'crontab -l' to confirm timing."
