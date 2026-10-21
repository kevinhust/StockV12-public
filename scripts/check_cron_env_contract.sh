#!/usr/bin/env bash
# check_cron_env_contract.sh — smoke-test production env before cron/shadow.
#
# Catches Day-1 class failures early:
#   ModuleNotFoundError: No module named 'scripts'
#   dirty PYTHONPATH (archive/ or v12/)
#   missing STOCKAPP_DISABLE_V11_ARCHIVE
#   Redis down on 6380 (optional)
#
# Usage:
#   bash scripts/check_cron_env_contract.sh
#   bash scripts/check_cron_env_contract.sh --with-redis
#   bash scripts/check_cron_env_contract.sh --quiet
#
# Cron examples (NUC):
#   @reboot sleep 15 && /home/app/Stockapp/scripts/check_cron_env_contract.sh --with-redis >> /home/app/Stockapp/logs/cron_env_contract.log 2>&1
#   50 7 * * 1-5 /home/app/Stockapp/scripts/check_cron_env_contract.sh --with-redis >> /home/app/Stockapp/logs/cron_env_contract.log 2>&1
#
# Exit: 0 = all checks pass; 1 = one or more failures.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=source_archive_free_env.sh
source "${SCRIPT_DIR}/source_archive_free_env.sh"

WITH_REDIS=0
QUIET=0
for arg in "$@"; do
  case "$arg" in
    --with-redis) WITH_REDIS=1 ;;
    --quiet|-q) QUIET=1 ;;
    -h|--help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
  esac
done

LOG_DIR="${STOCKAPP_ROOT}/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S%z')"

log() {
  if [[ "$QUIET" -eq 0 ]]; then
    echo "$@"
  fi
}

FAIL=0
pass() { log "  OK  $*"; }
fail() {
  log "  FAIL $*"
  FAIL=1
}

log "=== cron_env_contract ${STAMP} root=${STOCKAPP_ROOT} ==="

# ── 1. PYTHONPATH shape ─────────────────────────────────────────────
log "[1/6] PYTHONPATH"
log "      $PYTHONPATH"
case ":${PYTHONPATH}:" in
  *":${STOCKAPP_ROOT}/src:"*) pass "contains src" ;;
  *) fail "missing ${STOCKAPP_ROOT}/src" ;;
esac
case ":${PYTHONPATH}:" in
  *":${STOCKAPP_ROOT}/scripts:"*) pass "contains scripts dir" ;;
  *) fail "missing ${STOCKAPP_ROOT}/scripts" ;;
esac
case ":${PYTHONPATH}:" in
  *":${STOCKAPP_ROOT}:"*) pass "contains repo root (for from scripts.*)" ;;
  *) fail "missing repo root ${STOCKAPP_ROOT} — Day-1 class bug" ;;
esac
case ":${PYTHONPATH}:" in
  *archive*|*v11-legacy*|*"${STOCKAPP_ROOT}/v12/"*|*:*/v12/src:*)
    fail "dirty path (archive/ or v12/ present)"
    ;;
  *) pass "no archive/ or v12/ on path" ;;
esac

# ── 2. Archive-off flag ─────────────────────────────────────────────
log "[2/6] STOCKAPP_DISABLE_V11_ARCHIVE"
if [[ "${STOCKAPP_DISABLE_V11_ARCHIVE:-}" == "1" ]]; then
  pass "DISABLE=1 (hard archive-off)"
else
  fail "expected STOCKAPP_DISABLE_V11_ARCHIVE=1 (got '${STOCKAPP_DISABLE_V11_ARCHIVE:-}')"
fi

# ── 3. Python interpreter ───────────────────────────────────────────
log "[3/6] PYTHON=${PYTHON}"
if [[ -x "${PYTHON}" ]] || command -v "${PYTHON}" >/dev/null 2>&1; then
  pass "interpreter present"
else
  fail "PYTHON not executable: ${PYTHON}"
fi

# ── 4. Cron-style scripts package import ─────────────────────────────
# Mimic `python scripts/foo.py`: path[0] = scripts/, strip accidental cwd.
log "[4/6] cron-style from scripts.* import"
IMPORT_OUT="$(
  cd "${STOCKAPP_ROOT}"
  PYTHONPATH="${PYTHONPATH}" "${PYTHON}" -c "
import sys
sys.path = [p for p in sys.path if p not in ('', '.')]
sys.path.insert(0, '${STOCKAPP_ROOT}/scripts')
from scripts.eval_chronos_h2h_v3 import load_model
print('scripts_pkg_ok')
" 2>&1
)" || true
if echo "$IMPORT_OUT" | grep -q 'scripts_pkg_ok'; then
  pass "from scripts.eval_chronos_h2h_v3 import load_model"
else
  fail "scripts package import: ${IMPORT_OUT}"
fi

# ── 5. stockapp_v12 + archive gate ──────────────────────────────────
log "[5/6] stockapp_v12 + ensure_v11"
PKG_OUT="$(
  cd "${STOCKAPP_ROOT}"
  PYTHONPATH="${PYTHONPATH}" STOCKAPP_DISABLE_V11_ARCHIVE="${STOCKAPP_DISABLE_V11_ARCHIVE}" \
    "${PYTHON}" -c "
import stockapp_v12
from stockapp_v12.core._v11_compat import ensure_v11_archive_on_path as e
r = e()
assert r is False, f'ensure_v11 expected False, got {r!r}'
print('pkg_ok')
" 2>&1
)" || true
if echo "$PKG_OUT" | grep -q 'pkg_ok'; then
  pass "import stockapp_v12; ensure_v11() is False"
else
  fail "package/gate: ${PKG_OUT}"
fi

# ── 6. Redis (optional) ─────────────────────────────────────────────
log "[6/6] Redis ${REDIS_HOST}:${REDIS_PORT}"
if [[ "$WITH_REDIS" -eq 1 ]]; then
  if command -v redis-cli >/dev/null 2>&1; then
    PONG="$(timeout 2 redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" ping 2>/dev/null || true)"
    if [[ "$PONG" == "PONG" ]]; then
      pass "PONG"
    else
      # best-effort auto-heal then recheck
      if [[ -x "${SCRIPT_DIR}/ensure_redis_6380.sh" ]]; then
        log "      attempting ensure_redis_6380.sh ..."
        bash "${SCRIPT_DIR}/ensure_redis_6380.sh" 2>&1 | while read -r line; do log "      $line"; done || true
        PONG="$(timeout 2 redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" ping 2>/dev/null || true)"
      fi
      if [[ "$PONG" == "PONG" ]]; then
        pass "PONG after ensure"
      else
        fail "redis not reachable on ${REDIS_HOST}:${REDIS_PORT}"
      fi
    fi
  else
    fail "redis-cli not installed"
  fi
else
  pass "skipped (pass --with-redis to check)"
fi

# ── Summary ─────────────────────────────────────────────────────────
if [[ "$FAIL" -eq 0 ]]; then
  log "=== RESULT PASS ${STAMP} ==="
  exit 0
fi
log "=== RESULT FAIL ${STAMP} ==="
exit 1
