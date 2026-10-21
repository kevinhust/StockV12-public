#!/usr/bin/env bash
# Canonical production env for StockApp on NUC (and any single-host deploy).
#
# - PYTHONPATH is `src` + `scripts` + repo root (never archive/, never legacy v12/)
# - Optional soak flags from config/archive_free_soak.env
#
# Usage (from any runner under scripts/):
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   # shellcheck source=source_archive_free_env.sh
#   source "${SCRIPT_DIR}/source_archive_free_env.sh"
#
# Do NOT use set -e here — this file is sourced into runners.

# scripts/ → repo root
_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export STOCKAPP_ROOT="${STOCKAPP_ROOT:-$_REPO_ROOT}"

if [[ -f "${STOCKAPP_ROOT}/config/archive_free_soak.env" ]]; then
  # shellcheck disable=SC1091
  source "${STOCKAPP_ROOT}/config/archive_free_soak.env"
fi

# Force path truth: do NOT append inherited PYTHONPATH (may carry v12/src or archive/).
# Include STOCKAPP_ROOT so `from scripts.xxx import ...` works when cron runs
# `python scripts/foo.py` (script dir is on sys.path[0], but parent package
# `scripts` still needs the repo root).
export PYTHONPATH="${STOCKAPP_ROOT}/src:${STOCKAPP_ROOT}/scripts:${STOCKAPP_ROOT}"

case ":${PYTHONPATH}:" in
  *":${STOCKAPP_ROOT}/archive"*|*"archive/v11"*|*"v11-legacy"*|*"${STOCKAPP_ROOT}/v12/"*|*:*/v12/src:*)
    echo "ERROR: PYTHONPATH must not include archive/ or v12/: $PYTHONPATH" >&2
    exit 1
    ;;
esac

# Archive-free soak default: hard-off unless operator sets 0 in soak env.
export STOCKAPP_DISABLE_V11_ARCHIVE="${STOCKAPP_DISABLE_V11_ARCHIVE:-1}"

# NUC Redis defaults (port 6380 — see docs/DEPLOYMENT.md)
export REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
export REDIS_PORT="${REDIS_PORT:-6380}"

# Prefer project venv when present
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "${STOCKAPP_ROOT}/.venv/bin/python" ]]; then
    export PYTHON="${STOCKAPP_ROOT}/.venv/bin/python"
  elif [[ -x "${STOCKAPP_ROOT}/.venv/bin/python3" ]]; then
    export PYTHON="${STOCKAPP_ROOT}/.venv/bin/python3"
  else
    export PYTHON="$(command -v python3)"
  fi
fi
