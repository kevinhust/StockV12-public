#!/usr/bin/env bash
# Ensure Redis is listening on 6380 (NUC / WSL2 production port).
#
# Why 6380: WSL2 on this host family often has a stuck 6379 reservation;
# pipeline scripts default REDIS_PORT=6380.
#
# Usage:
#   bash scripts/ensure_redis_6380.sh
#   bash scripts/ensure_redis_6380.sh --restart
#
# Safe to re-run (idempotent if already PONG on 6380).

set -euo pipefail

REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
REDIS_PORT="${REDIS_PORT:-6380}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${REDIS_DATA_DIR:-${REPO_ROOT}/data/redis}"
LOG_FILE="${REDIS_LOG_FILE:-${REPO_ROOT}/logs/redis-${REDIS_PORT}.log}"
PID_FILE="${REDIS_PID_FILE:-${REPO_ROOT}/data/redis/redis-${REDIS_PORT}.pid}"
RESTART=0

for arg in "$@"; do
  case "$arg" in
    --restart) RESTART=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
  esac
done

ping_ok() {
  timeout 2 redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" ping 2>/dev/null | grep -q PONG
}

if [[ "$RESTART" -eq 0 ]] && ping_ok; then
  echo "OK: Redis already PONG on ${REDIS_HOST}:${REDIS_PORT}"
  exit 0
fi

mkdir -p "$DATA_DIR" "$(dirname "$LOG_FILE")"

if [[ "$RESTART" -eq 1 ]] && [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${old_pid:-}" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Stopping redis pid=$old_pid (port ${REDIS_PORT})"
    kill "$old_pid" 2>/dev/null || true
    sleep 1
    kill -9 "$old_pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
fi

# Drop zombie redis-server processes that claim a port but do not answer PING
# (common after WSL2 network glitches). Only kill if OUR port does not PONG.
if ! ping_ok; then
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    case "$cmd" in
      *"--port ${REDIS_PORT}"*|*"port ${REDIS_PORT}"*)
        echo "Killing unresponsive redis-server pid=$pid ($cmd)"
        kill "$pid" 2>/dev/null || true
        sleep 0.5
        kill -9 "$pid" 2>/dev/null || true
        ;;
    esac
  done < <(pgrep -x redis-server || true)
fi

if ping_ok; then
  echo "OK: Redis PONG on ${REDIS_HOST}:${REDIS_PORT} after cleanup"
  exit 0
fi

if ! command -v redis-server >/dev/null 2>&1; then
  echo "ERROR: redis-server not installed" >&2
  exit 1
fi

echo "Starting redis-server on ${REDIS_HOST}:${REDIS_PORT}"
redis-server \
  --daemonize yes \
  --bind "$REDIS_HOST" \
  --port "$REDIS_PORT" \
  --dir "$DATA_DIR" \
  --dbfilename "dump-${REDIS_PORT}.rdb" \
  --pidfile "$PID_FILE" \
  --logfile "$LOG_FILE" \
  --protected-mode yes \
  --save "" \
  --appendonly no

sleep 0.5
if ping_ok; then
  echo "OK: Redis started — PONG on ${REDIS_HOST}:${REDIS_PORT}"
  echo "  data=$DATA_DIR pidfile=$PID_FILE log=$LOG_FILE"
  exit 0
fi

echo "ERROR: Redis failed to become ready on ${REDIS_HOST}:${REDIS_PORT}" >&2
echo "  log tail:" >&2
tail -20 "$LOG_FILE" 2>/dev/null >&2 || true
exit 1
