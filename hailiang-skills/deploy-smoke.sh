#!/usr/bin/env bash
# Deploy one isolated, non-systemd API instance for BFF smoke testing.
# This intentionally does not create Docker infrastructure or build the frontend.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$PROJECT_DIR/env.8015.sh"
REPLACE_PORT=0
SKIP_INSTALL=0
SKIP_MIGRATIONS=0

usage() {
  cat <<'EOF'
Usage: ./deploy-smoke.sh [options]

Deploy an isolated API instance using a private environment file.  It installs
old-Linux-compatible Python wheels from the Tsinghua mirror, migrates only the
configured isolated database, and verifies /health/ready.

Options:
  --env PATH          Private environment file (default: ./env.8015.sh)
  --replace-port      Gracefully stop an existing Hailiang Uvicorn instance on
                      BACKEND_PORT before starting this one.
  --skip-install      Reuse the existing smoke virtual environment.
  --skip-migrations   Do not run Alembic (only for an already migrated DB).
  -h, --help          Show this help.

This script never starts, recreates, or removes PostgreSQL/Redis containers.
It refuses to replace a listener unless it is a Hailiang Uvicorn process.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --env) ENV_FILE="${2:?--env requires a path}"; shift ;;
    --replace-port) REPLACE_PORT=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    --skip-migrations) SKIP_MIGRATIONS=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

require_value() {
  local name="$1"
  [ -n "${!name:-}" ] || { echo "Missing required setting: $name" >&2; exit 2; }
}

listener_pids() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -t -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
    return
  fi
  if command -v fuser >/dev/null 2>&1; then
    fuser -n tcp "$port" 2>/dev/null | tr ' ' '\n' | sed '/^$/d' || true
    return
  fi
  echo "lsof or fuser is required to identify the listener on port $port" >&2
  exit 2
}

wait_for_port_release() {
  local port="$1"
  local attempt
  for attempt in $(seq 1 20); do
    [ -z "$(listener_pids "$port")" ] && return 0
    sleep 1
  done
  return 1
}

wait_for_health() {
  local url="$1"
  local attempt
  for attempt in $(seq 1 45); do
    curl --fail --silent --show-error "$url" >/dev/null && return 0
    sleep 1
  done
  return 1
}

[ -f "$ENV_FILE" ] || { echo "Environment file not found: $ENV_FILE" >&2; exit 2; }
# The environment file is private and must consist of shell exports only.
# shellcheck disable=SC1090
source "$ENV_FILE"

require_value HAILIANG_DEPLOY_ENV
require_value HAILIANG_BIND_HOST
require_value BACKEND_PORT
require_value HAILIANG_DATABASE_URL
require_value HAILIANG_REDIS_URL
require_value HAILIANG_REDIS_KEY_PREFIX
require_value HAILIANG_AUDIT_ENCRYPTION_KEY
require_value HAILIANG_SECURITY_QUARANTINE_KEY
require_value DASHSCOPE_API_KEY
require_value AGENT_SKILL_RUNTIME_CORE_PATH

[ "$HAILIANG_DEPLOY_ENV" = "test" ] || {
  echo "Smoke deployment requires HAILIANG_DEPLOY_ENV=test" >&2
  exit 2
}
[ -d "$AGENT_SKILL_RUNTIME_CORE_PATH/agent_skill_runtime_core" ] || {
  echo "Invalid AGENT_SKILL_RUNTIME_CORE_PATH: $AGENT_SKILL_RUNTIME_CORE_PATH" >&2
  exit 2
}
# The normal test database and Redis namespace belong to the legacy test
# service.  Requiring a distinct name prevents a smoke migration from changing it.
case "$HAILIANG_DATABASE_URL" in
  *hailiang_skills_test_*) ;;
  *) echo "Smoke database must use a distinct name such as hailiang_skills_test_411" >&2; exit 2 ;;
esac
case "$HAILIANG_REDIS_KEY_PREFIX" in
  hailiang:smoke*) ;;
  *) echo "Smoke Redis key prefix must start with hailiang:smoke" >&2; exit 2 ;;
esac

VENV_DIR="${HAILIANG_SMOKE_VENV_DIR:-$PROJECT_DIR/.venv-smoke-$BACKEND_PORT}"
export HAILIANG_RUNTIME_DIR="${HAILIANG_RUNTIME_DIR:-$PROJECT_DIR/runtime-smoke-$BACKEND_PORT}"
export HAILIANG_LOG_DIR="${HAILIANG_LOG_DIR:-$PROJECT_DIR/logs-smoke-$BACKEND_PORT}"
export PYTHONPATH="$PROJECT_DIR/src:$AGENT_SKILL_RUNTIME_CORE_PATH"
LOG_FILE="${HAILIANG_SMOKE_LOG_FILE:-$PROJECT_DIR/smoke-$BACKEND_PORT.log}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PIP_DEFAULT_TIMEOUT="${PIP_DEFAULT_TIMEOUT:-120}"
PIP_RETRIES="${PIP_RETRIES:-5}"
CONSTRAINTS_FILE="$PROJECT_DIR/constraints/linux-legacy-ms-agent.txt"

mkdir -p "$HAILIANG_RUNTIME_DIR" "$HAILIANG_LOG_DIR"
if [ -z "${SSL_CERT_FILE:-}" ] && [ -f /etc/pki/tls/cert.pem ]; then
  export SSL_CERT_FILE=/etc/pki/tls/cert.pem
fi

echo "Project: $PROJECT_DIR"
echo "Environment: $ENV_FILE"
echo "API: http://$HAILIANG_BIND_HOST:$BACKEND_PORT"
echo "Smoke database: ${HAILIANG_DATABASE_URL%@*}@…"
echo "Venv: $VENV_DIR"

if [ "$SKIP_INSTALL" = "0" ]; then
  command -v python3.11 >/dev/null 2>&1 || { echo "python3.11 is required" >&2; exit 2; }
  python3.11 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel -i "$PIP_INDEX_URL"
  "$VENV_DIR/bin/python" -m pip install --only-binary :all: greenlet -i "$PIP_INDEX_URL"
  "$VENV_DIR/bin/python" -m pip install -r "$CONSTRAINTS_FILE" -i "$PIP_INDEX_URL"
  "$VENV_DIR/bin/python" -m pip install -c "$CONSTRAINTS_FILE" -e "$PROJECT_DIR" -i "$PIP_INDEX_URL"
fi

PYTHONPATH="$PYTHONPATH" "$VENV_DIR/bin/python" - <<'PY'
import importlib

for module in ("hailiang_skills.api.main", "agent_skill_runtime_core", "ms_agent"):
    importlib.import_module(module)
print("dependency imports passed")
PY

if [ "$SKIP_MIGRATIONS" = "0" ]; then
  PYTHONPATH=src "$VENV_DIR/bin/alembic" upgrade head
fi

"$VENV_DIR/bin/python" - <<'PY'
from hailiang_skills.core.rate_limit import get_llm_rate_limiter
from hailiang_skills.storage.database import build_engine

with build_engine().connect() as connection:
    connection.exec_driver_sql("SELECT 1")
if not get_llm_rate_limiter().ready():
    raise SystemExit("Redis/rate limiter is not ready")
print("PostgreSQL and Redis are ready")
PY

existing_pids="$(listener_pids "$BACKEND_PORT")"
if [ -n "$existing_pids" ]; then
  if [ "$REPLACE_PORT" != "1" ]; then
    echo "Port $BACKEND_PORT is already in use by PID(s): $existing_pids" >&2
    echo "Stop it manually, choose another port, or rerun with --replace-port." >&2
    exit 3
  fi
  stop_pids=()
  for pid in $existing_pids; do
    command_line="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    if [[ "$command_line" == *"hailiang_skills.api.main:app"* ]] && [[ "$command_line" == *"--port $BACKEND_PORT"* ]]; then
      # In a multi-worker Uvicorn process group, only the parent must receive
      # SIGTERM.  The children will exit cleanly with it.
      stop_pids+=("$pid")
      continue
    fi

    parent_pid="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ')"
    parent_command="$(ps -p "$parent_pid" -o args= 2>/dev/null || true)"
    if [[ "$parent_command" == *"hailiang_skills.api.main:app"* ]] && [[ "$parent_command" == *"--port $BACKEND_PORT"* ]]; then
      continue
    fi

    echo "Refusing to stop PID $pid because it is not a Hailiang Uvicorn listener:" >&2
    echo "$command_line" >&2
    exit 3
  done
  [ "${#stop_pids[@]}" -gt 0 ] || {
    echo "No Uvicorn parent process was found for port $BACKEND_PORT" >&2
    exit 3
  }
  echo "Gracefully stopping existing Hailiang Uvicorn parent(s): ${stop_pids[*]}"
  kill -TERM "${stop_pids[@]}"
  wait_for_port_release "$BACKEND_PORT" || {
    echo "Port $BACKEND_PORT was not released after SIGTERM; inspect the old process before retrying." >&2
    exit 3
  }
fi

echo "Starting smoke API; log: $LOG_FILE"
nohup "$VENV_DIR/bin/python" -m uvicorn hailiang_skills.api.main:app \
  --host "$HAILIANG_BIND_HOST" \
  --port "$BACKEND_PORT" \
  --workers "${UVICORN_WORKERS:-1}" \
  --timeout-keep-alive 15 \
  --no-access-log \
  > "$LOG_FILE" 2>&1 &
new_pid="$!"

health_host="$HAILIANG_BIND_HOST"
[ "$health_host" = "0.0.0.0" ] && health_host=127.0.0.1
health_url="http://$health_host:$BACKEND_PORT/health/ready"
if ! wait_for_health "$health_url"; then
  echo "Smoke API did not become ready: $health_url" >&2
  tail -n 100 "$LOG_FILE" >&2 || true
  exit 1
fi

echo "Smoke deployment succeeded. PID: $new_pid"
curl --fail --silent "$health_url"
echo
