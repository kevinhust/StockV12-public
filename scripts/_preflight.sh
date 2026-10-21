#!/usr/bin/env bash
# _preflight.sh — full pre-trade preflight for V12 cron jobs.
#
# Checks performed (each is fast and side-effect-free):
#  1. Redis reachability       (redis-cli PING == "PONG")
#  2. KillSwitch not engaged   (system:halt absent)
#  3. Python venv imports OK   (python -c "import stockapp_v12")
#  4. TradeJournal schema OK   (sample journal:<date>:<sym> == schema_version=1)
#
# On any failure the script:
#   - emits a one-line skip marker to logs/pipeline/skip.log
#   - returns non-zero so the *caller* can decide (typically map to exit 0
#     to suppress cron spam for intentional skips)
#
# On success: return 0, caller continues.
#
# IMPORTANT: This file is *sourced* by runners. Use `return`, never `exit`,
# so the parent shell is not aborted after a successful check.
#
# Spec: Phase A2 (council verdict 2026-07-07). Code review 2026-07-27 fix.

# Preserve caller shell options (sourced file must not leave set +e).
_PREFLIGHT_SET_E=0
case "$-" in *e*) _PREFLIGHT_SET_E=1 ;; esac
set +e  # check each rule independently; do NOT bail on first failure

REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6380}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Prefer STOCKAPP_ROOT from source_archive_free_env.sh
_PREFLIGHT_ROOT="${STOCKAPP_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"

FAIL_LINE=""
RC=0
log_skip() {
  local reason="$1"
  mkdir -p "${_PREFLIGHT_ROOT}/logs/pipeline" 2>/dev/null || true
  echo "[$(basename "${SCRIPT_NAME:-preflight}"):$(basename "$0")] SKIP — ${reason}" >> \
    "${_PREFLIGHT_ROOT}/logs/pipeline/skip.log" 2>/dev/null || true
}

# 1. Redis reachability
PONG=$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" PING 2>/dev/null || true)
if [ "$PONG" != "PONG" ]; then
  FAIL_LINE="${FAIL_LINE}redis_unreachable;"
  RC=1
fi

# 2. KillSwitch
HALT_VAL=$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" GET system:halt 2>/dev/null || true)
if [ -n "$HALT_VAL" ]; then
  REASON=$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" GET system:halt:reason 2>/dev/null || echo "unknown")
  FAIL_LINE="${FAIL_LINE}halt_active(${REASON});"
  RC=1
fi

# 3. Python venv imports — use PYTHON / STOCKAPP_ROOT (not host hardcode)
VENV_PY="${PYTHON:-${STOCKAPP_VENV:-${_PREFLIGHT_ROOT}/.venv/bin/python}}"
if [ ! -x "$VENV_PY" ] && ! command -v "$VENV_PY" >/dev/null 2>&1; then
  FAIL_LINE="${FAIL_LINE}venv_missing;"
  RC=1
else
  IMPORT_TEST=$(
    PYTHONPATH="${PYTHONPATH:-${_PREFLIGHT_ROOT}/src:${_PREFLIGHT_ROOT}/scripts:${_PREFLIGHT_ROOT}}" \
      "$VENV_PY" -c "import stockapp_v12" 2>&1
  ) || true
  if [ -n "$IMPORT_TEST" ]; then
    FAIL_LINE="${FAIL_LINE}import_fail(${IMPORT_TEST});"
    RC=1
  fi
fi

# 4. TradeJournal schema sanity (best-effort — empty journal is OK)
TODAY=$(date +%Y-%m-%d)
SCHEMA_FIELD=$(redis-cli -h "${REDIS_HOST}" -p "${REDIS_PORT}" \
  EVAL "local k=redis.call('KEYS','journal:${TODAY}:*'); if #k==0 then return 'EMPTY' else local v=redis.call('GET',k[1]); if not v then return 'MISSING' end; local ok,rec=pcall(cjson.decode,v); if not ok or type(rec)~='table' then return 'CORRUPT' end; if rec[1] and type(rec[1])=='table' and rec[1].schema_version==1 then return 'OK' else return 'BAD_VERSION' end end" 0 2>/dev/null || echo "REDIS_ERR")
# redis-cli may quote bulk strings as "EMPTY" — normalize
SCHEMA_FIELD="${SCHEMA_FIELD//\"/}"
case "$SCHEMA_FIELD" in
  OK|EMPTY|"") ;;  # empty journal / no keys is fine on a fresh day
  *)
    FAIL_LINE="${FAIL_LINE}journal_schema(${SCHEMA_FIELD});"
    RC=1
    ;;
esac

if [ "$RC" -ne 0 ]; then
  log_skip "${FAIL_LINE}"
fi

# Restore set -e if caller had it
if [ "$_PREFLIGHT_SET_E" -eq 1 ]; then
  set -e
else
  set +e
fi

# return when sourced; exit when executed as a standalone script
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  return "$RC"
fi
exit "$RC"
