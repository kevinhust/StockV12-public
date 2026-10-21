#!/usr/bin/env bash
# Refresh shadow-universe daily features on NUC via **Longbridge** (longport SDK).
#
# One-time install:
#   .venv/bin/pip install 'longport>=3.0.0' 'pyarrow>=7,<21'
# Credentials in .env / env:
#   LONGBRIDGE_APP_KEY LONGBRIDGE_APP_SECRET LONGBRIDGE_ACCESS_TOKEN
#
# Cron (before shadow 16:05 ET):
#   0 16 * * 1-5 /home/app/Stockapp/scripts/run_refresh_shadow_features.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=source_archive_free_env.sh
source "${SCRIPT_DIR}/source_archive_free_env.sh"

export TZ="${TZ:-America/New_York}"
# Credentials for Longbridge OpenAPI
if [[ -f "${STOCKAPP_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${STOCKAPP_ROOT}/.env"
  set +a
fi

LOG_DIR="${STOCKAPP_ROOT}/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/feature_refresh_$(date +%Y%m%d).log"

exec >>"$LOG_FILE" 2>&1
echo "=== feature_refresh (longbridge) start $(date -Iseconds) ==="
echo "PYTHON=$PYTHON ROOT=$STOCKAPP_ROOT"
cd "${STOCKAPP_ROOT}"

"${PYTHON}" -c "from longport.openapi import Config; print('longport_ok')"

set +e
"${PYTHON}" scripts/refresh_shadow_features.py
RC=$?
set -e

echo "=== feature_refresh exit=$RC $(date -Iseconds) ==="
exit "$RC"
