#!/usr/bin/env bash
# 本地 Mac 联调启动脚本。
#
# 推荐首次执行：
#   cp env.example.sh env.local.sh && 编辑 env.local.sh
#   ./run.sh --bootstrap
# 日常执行：
#   ./run.sh
# 若要把既有 logs/ 导入空数据库：
#   ./run.sh --migrate-file-logs
# Ctrl+C 会同时停止本次脚本启动的前后端；PostgreSQL/Redis 容器保留，便于下次启动。

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$PROJECT_DIR/frontend"
BOOTSTRAP=0
MIGRATE_FILE_LOGS=0
START_INFRA=1
BACKEND_PID=""
POSTGRES_HOST_PORT=""
REDIS_HOST_PORT=""
OTEL_HOST_PORT=""
OTEL_METRICS_HOST_PORT=""

usage() {
  cat <<'EOF'
用法：./run.sh [选项]
  --bootstrap           安装/更新 Python 与前端依赖
  --migrate-file-logs   将旧 logs/ 文件导入 PostgreSQL（仅首次迁移需要）
  --no-infra            不启动 Docker 的 PostgreSQL/Redis/Collector
  -h, --help            显示帮助
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bootstrap) BOOTSTRAP=1 ;;
    --migrate-file-logs) MIGRATE_FILE_LOGS=1 ;;
    --no-infra) START_INFRA=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "❌ 不支持的参数：$1"; usage; exit 2 ;;
  esac
  shift
done

cd "$PROJECT_DIR"
# shellcheck disable=SC1091
source "$PROJECT_DIR/env.sh"

require_command() { command -v "$1" >/dev/null 2>&1 || { echo "❌ 缺少命令：$1"; exit 1; }; }
require_value() { [ -n "${!1:-}" ] || { echo "❌ 缺少环境变量：$1（请填写 env.local.sh）"; exit 1; }; }
port_is_available() {
  local port="$1"
  ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}
choose_host_port() {
  local preferred="$1"
  local fallback="$2"
  if port_is_available "$preferred"; then
    echo "$preferred"
  else
    # Bash 会把中文句号视为变量名的一部分，故变量后必须使用花括号。
    echo "⚠️  本机端口 ${preferred} 已被其他服务占用，改用 ${fallback}。" >&2
    if ! port_is_available "$fallback"; then
      echo "❌ 备用端口 $fallback 也被占用；请停止占用进程或在 env.local.sh 指定其他端口。" >&2
      exit 1
    fi
    echo "$fallback"
  fi
}
existing_compose_port() {
  local service="$1"
  local container_port="$2"
  local mapping=""
  # docker compose port returns e.g. 0.0.0.0:16379 for an already-running
  # project container. Reusing it is essential: it is not an external conflict.
  mapping="$(docker compose port "$service" "$container_port" 2>/dev/null | tail -n 1 || true)"
  [ -n "$mapping" ] || return 1
  echo "${mapping##*:}"
}
resolve_infra_port() {
  local service="$1"
  local container_port="$2"
  local preferred="$3"
  local fallback="$4"
  local existing=""
  if existing="$(existing_compose_port "$service" "$container_port")"; then
    echo "ℹ️  复用本项目已运行的 ${service} 容器端口：${existing}" >&2
    echo "$existing"
    return
  fi
  choose_host_port "$preferred" "$fallback"
}
choose_application_port() {
  local preferred="$1"
  shift
  if port_is_available "$preferred"; then
    echo "$preferred"
    return
  fi
  echo "⚠️  应用端口 ${preferred} 已被占用，正在选择本地备用端口。" >&2
  local candidate
  for candidate in "$@"; do
    if [ "$candidate" != "$preferred" ] && port_is_available "$candidate"; then
      echo "$candidate"
      return
    fi
  done
  echo "❌ 找不到可用应用端口，请在 env.local.sh 修改 BACKEND_PORT/FRONTEND_PORT。" >&2
  exit 1
}
replace_local_port() {
  local url="$1"
  local old_port="$2"
  local new_port="$3"
  # 仅改写本机 URL；远程/托管数据库地址永远尊重用户在 env.local.sh 的设置。
  if [[ "$url" == *"127.0.0.1:${old_port}"* ]] || [[ "$url" == *"localhost:${old_port}"* ]]; then
    echo "${url/:${old_port}/:${new_port}}"
  else
    echo "$url"
  fi
}
kill_process() {
  [ -z "${1:-}" ] || kill "$1" 2>/dev/null || true
}
cleanup() {
  if [ -n "$BACKEND_PID" ]; then
    echo "\n🛑 正在停止本次启动的后端进程..."
    kill_process "$BACKEND_PID"
    BACKEND_PID=""
  fi
}
# INT/TERM 先退出，再由 EXIT 统一清理，避免连续 Ctrl+C 重复执行 trap。
trap cleanup EXIT
trap 'exit 130' INT TERM

require_command npm
require_command curl
require_command lsof
require_value AGENT_SKILL_RUNTIME_CORE_PATH
require_value DASHSCOPE_API_KEY
if [ "$HAILIANG_STORAGE_BACKEND" = "postgres" ]; then
  require_value HAILIANG_AUDIT_ENCRYPTION_KEY
fi

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ] || [ "$BOOTSTRAP" = "1" ]; then
  echo "📦 安装后端依赖..."
  [ -x "$PROJECT_DIR/.venv/bin/python" ] || python3 -m venv "$PROJECT_DIR/.venv"
  "$PROJECT_DIR/.venv/bin/pip" install -e .
fi

if [ "$BOOTSTRAP" = "1" ] || [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  echo "📦 安装前端依赖..."
  (cd "$FRONTEND_DIR" && npm ci)
fi

if [ "$START_INFRA" = "1" ] && [ "$HAILIANG_STORAGE_BACKEND" = "postgres" ]; then
  require_command docker
  if ! docker info >/dev/null 2>&1; then
    echo "❌ Docker Desktop 尚未启动，无法启动本地 PostgreSQL / Redis。"
    echo "   请在 Mac 中打开 Docker Desktop，等待状态变为 Running 后重新执行："
    echo "   ./run.sh --bootstrap"
    echo "   若你已经有可访问的远程 PostgreSQL 和 Redis，可使用 --no-infra。"
    exit 1
  fi
  # 不占用/中断已有 Redis、PostgreSQL；本项目容器使用可预测备用端口。
  POSTGRES_HOST_PORT="$(resolve_infra_port postgres 5432 "${POSTGRES_HOST_PORT:-5432}" 15432)"
  REDIS_HOST_PORT="$(resolve_infra_port redis 6379 "${REDIS_HOST_PORT:-6379}" 16379)"
  OTEL_HOST_PORT="$(resolve_infra_port otel-collector 4318 "${OTEL_HOST_PORT:-4318}" 14318)"
  OTEL_METRICS_HOST_PORT="$(resolve_infra_port otel-collector 9464 "${OTEL_METRICS_HOST_PORT:-9464}" 19464)"
  export POSTGRES_HOST_PORT REDIS_HOST_PORT OTEL_HOST_PORT OTEL_METRICS_HOST_PORT
  HAILIANG_DATABASE_URL="$(replace_local_port "$HAILIANG_DATABASE_URL" 5432 "$POSTGRES_HOST_PORT")"
  HAILIANG_REDIS_URL="$(replace_local_port "$HAILIANG_REDIS_URL" 6379 "$REDIS_HOST_PORT")"
  OTEL_EXPORTER_OTLP_ENDPOINT="$(replace_local_port "$OTEL_EXPORTER_OTLP_ENDPOINT" 4318 "$OTEL_HOST_PORT")"
  export HAILIANG_DATABASE_URL HAILIANG_REDIS_URL OTEL_EXPORTER_OTLP_ENDPOINT
  echo "🐳 启动本地 PostgreSQL、Redis 与观测 Collector..."
  echo "   PostgreSQL: $HAILIANG_DATABASE_URL"
  echo "   Redis:      $HAILIANG_REDIS_URL"
  docker compose up -d postgres redis otel-collector
fi

if [ "$HAILIANG_STORAGE_BACKEND" = "postgres" ]; then
  echo "🗃️  执行数据库迁移..."
  PYTHONPATH=src "$PROJECT_DIR/.venv/bin/alembic" upgrade head
  if [ "$MIGRATE_FILE_LOGS" = "1" ]; then
    echo "📥 导入旧 logs/ 数据（已存在的记录会跳过）..."
    PYTHONPATH=src "$PROJECT_DIR/.venv/bin/python" scripts/migrate_file_logs_to_postgres.py
  fi
fi

# 避免旧调试服务（例如 IDE 代理）占用端口后，curl 一直连到错误服务。
BACKEND_PORT="$(choose_application_port "$BACKEND_PORT" 8010 8011 8012 8013)"
FRONTEND_PORT="$(choose_application_port "$FRONTEND_PORT" 4175 4176 4177)"
export BACKEND_PORT FRONTEND_PORT
# The browser may open Vite as localhost or 127.0.0.1. Keep both local
# origins explicitly allowed, while the frontend derives the API hostname
# from the page URL to avoid localhost <-> 127.0.0.1 fetch failures.
HAILIANG_CORS_ORIGINS="${HAILIANG_CORS_ORIGINS},http://localhost:${FRONTEND_PORT},http://127.0.0.1:${FRONTEND_PORT}"
export HAILIANG_CORS_ORIGINS
echo "🚀 启动后端：http://127.0.0.1:${BACKEND_PORT}"
PYTHONPATH="$PROJECT_DIR/src:$AGENT_SKILL_RUNTIME_CORE_PATH" \
  "$PROJECT_DIR/.venv/bin/python" -m uvicorn hailiang_skills.api.main:app \
  --host 0.0.0.0 --port "$BACKEND_PORT" > "$PROJECT_DIR/backend.local.log" 2>&1 &
BACKEND_PID="$!"

for _ in $(seq 1 30); do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "❌ 后端进程已退出，请查看：tail -n 100 $PROJECT_DIR/backend.local.log"
    exit 1
  fi
  if curl --connect-timeout 1 --max-time 3 -fsS "http://127.0.0.1:${BACKEND_PORT}/health/ready" >/dev/null; then break; fi
  sleep 1
done
if ! curl --connect-timeout 1 --max-time 3 -fsS "http://127.0.0.1:${BACKEND_PORT}/health/ready" >/dev/null; then
  echo "❌ 后端未就绪，请查看：tail -n 100 $PROJECT_DIR/backend.local.log"
  exit 1
fi

echo "✅ 后端已就绪。"
echo "🎨 启动前端：http://localhost:${FRONTEND_PORT}（也可用 http://127.0.0.1:${FRONTEND_PORT}）"
echo "   浏览器访问前端；接口指标：http://127.0.0.1:${BACKEND_PORT}/metrics"
echo "   前端使用“本地调试身份”面板填写或生成 user_id、profile_id、session_id。"
cd "$FRONTEND_DIR"
# Do not hard-code 127.0.0.1 here. runtime.ts uses the browser page hostname
# (localhost or 127.0.0.1) and this variable only supplies the selected port.
VITE_API_BASE_URL="" VITE_BACKEND_PORT="$BACKEND_PORT" npm run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT"
