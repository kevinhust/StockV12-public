#!/usr/bin/env bash
# Point NUC crontab at repo-root scripts/ (not legacy v12/scripts/).
# Idempotent. Run on NUC after git pull of main.
#
#   bash scripts/nuc_converge_to_root_scripts.sh --yes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
YES=0
[[ "${1:-}" == "--yes" ]] && YES=1

if [[ "$YES" -ne 1 ]]; then
  echo "Usage: $0 --yes"
  echo "Will rewrite crontab lines that still point at ${REPO_ROOT}/v12/scripts/"
  echo "to ${REPO_ROOT}/scripts/ and reinstall pipeline + shadow crons from root."
  exit 2
fi

CURRENT="$(crontab -l 2>/dev/null || true)"
if [[ -z "$CURRENT" ]]; then
  echo "No crontab; installing fresh from root scripts."
else
  UPDATED="$(printf '%s\n' "$CURRENT" | sed "s|${REPO_ROOT}/v12/scripts/|${REPO_ROOT}/scripts/|g")"
  printf '%s\n' "$UPDATED" | crontab -
  echo "Rewrote v12/scripts → scripts in crontab."
fi

# Reinstall managed blocks from canonical root installers
bash "${REPO_ROOT}/scripts/install_pipeline_cron.sh" --yes
if [[ -x "${REPO_ROOT}/scripts/install_shadow_cron.sh" ]]; then
  bash "${REPO_ROOT}/scripts/install_shadow_cron.sh" --yes 2>/dev/null || \
    bash "${REPO_ROOT}/scripts/install_shadow_cron.sh" 2>/dev/null || true
fi
if [[ -x "${REPO_ROOT}/scripts/install_shadow_ensemble_cron.sh" ]]; then
  bash "${REPO_ROOT}/scripts/install_shadow_ensemble_cron.sh" --yes 2>/dev/null || \
    bash "${REPO_ROOT}/scripts/install_shadow_ensemble_cron.sh" 2>/dev/null || true
fi

echo "=== crontab now ==="
crontab -l
echo "Done. Prefer only ${REPO_ROOT}/scripts/ for new jobs; v12/scripts is legacy."
