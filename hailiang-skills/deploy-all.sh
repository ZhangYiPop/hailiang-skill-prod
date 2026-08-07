#!/usr/bin/env bash
echo "deploy-all.sh 已废弃：服务器请使用 deploy/bin/promote-release.sh 与 systemd。" >&2
exit 2


set -euo pipefail

echo -e "\n============================================="
echo "      hailiang-skills 全量部署（skill-runtime 版）"
echo -e "=============================================\n"

# --------------------- 配置 ---------------------
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$PROJECT_DIR/env.sh" ]; then
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/env.sh"
else
  echo "⚠️  未找到 env.sh，继续使用当前 shell 环境和脚本默认配置。"
fi

FRONTEND_DIR="$PROJECT_DIR/frontend"
FRONTEND_PORT="${FRONTEND_PORT:-4174}"
BACKEND_PORT="${BACKEND_PORT:-8011}"
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/.venv}"
PYTHON="${PYTHON:-python3.11}"
SERVER_IP="${SERVER_IP:-}"
PUBLIC_API_BASE_URL="${PUBLIC_API_BASE_URL:-}"
DEFAULT_USER_ID="${DEFAULT_USER_ID:-debug-user}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
RECREATE_VENV="${RECREATE_VENV:-1}"
INSTALL_MS_AGENT_RUNTIME="${INSTALL_MS_AGENT_RUNTIME:-auto}"
MS_AGENT_LEGACY_WHEELS="${MS_AGENT_LEGACY_WHEELS:-1}"
START_INFRA="${START_INFRA:-1}"
AGENT_SKILL_RUNTIME_CORE_PATH="${AGENT_SKILL_RUNTIME_CORE_PATH:-}"
HAILIANG_RUNTIME_DIR="${HAILIANG_RUNTIME_DIR:-runtime}"
HAILIANG_SOUL_PATH="${HAILIANG_SOUL_PATH:-config/soul.md}"
HAILIANG_MEMORY_ENABLED="${HAILIANG_MEMORY_ENABLED:-true}"
HAILIANG_SANDBOX_PREWARM_ENABLED="${HAILIANG_SANDBOX_PREWARM_ENABLED:-true}"
REQUIRE_DOCKER_SANDBOX="${REQUIRE_DOCKER_SANDBOX:-0}"
HAILIANG_STORAGE_BACKEND="${HAILIANG_STORAGE_BACKEND:-postgres}"
HAILIANG_DATABASE_URL="${HAILIANG_DATABASE_URL:-}"
HAILIANG_REDIS_URL="${HAILIANG_REDIS_URL:-}"
HAILIANG_AUDIT_ENCRYPTION_KEY="${HAILIANG_AUDIT_ENCRYPTION_KEY:-}"
HAILIANG_AUDIT_KEY_ID="${HAILIANG_AUDIT_KEY_ID:-primary}"
HAILIANG_AUDIT_RETENTION_DAYS="${HAILIANG_AUDIT_RETENTION_DAYS:-90}"
HAILIANG_MAX_SSE_CONNECTIONS="${HAILIANG_MAX_SSE_CONNECTIONS:-100}"
HAILIANG_STREAM_WORKERS="${HAILIANG_STREAM_WORKERS:-50}"
HAILIANG_SSE_QUEUE_TIMEOUT_SECONDS="${HAILIANG_SSE_QUEUE_TIMEOUT_SECONDS:-5}"
OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-}"
UVICORN_WORKERS="${UVICORN_WORKERS:-2}"
RUN_DB_MIGRATIONS="${RUN_DB_MIGRATIONS:-1}"
POSTGRES_HOST_PORT="${POSTGRES_HOST_PORT:-5432}"
REDIS_HOST_PORT="${REDIS_HOST_PORT:-6379}"
OTEL_HOST_PORT="${OTEL_HOST_PORT:-4318}"
OTEL_METRICS_HOST_PORT="${OTEL_METRICS_HOST_PORT:-9464}"
LEGACY_MS_AGENT_CONSTRAINTS_FILE="$PROJECT_DIR/constraints/linux-legacy-ms-agent.txt"
MS_AGENT_RUNTIME_READY=0

# 建议在服务器环境变量里配置 DASHSCOPE_API_KEY。
# 为兼容旧脚本，如果环境变量未设置，可在这里临时填入；不要提交真实密钥到远程仓库。
DASHSCOPE_API_KEY="${DASHSCOPE_API_KEY:-}"

log() { echo -e "$1"; }

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "❌ 缺少命令：$1"
    exit 1
  fi
}

port_is_available() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    return
  fi
  if command -v ss >/dev/null 2>&1; then
    ! ss -lnt "( sport = :$port )" 2>/dev/null | grep -q LISTEN
    return
  fi
  python - "$port" <<'PY' >/dev/null 2>&1
import socket
import sys

port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        raise SystemExit(1)
raise SystemExit(0)
PY
}

choose_host_port() {
  local preferred="$1"
  local fallback="$2"
  if port_is_available "$preferred"; then
    echo "$preferred"
  else
    echo "⚠️  本机端口 ${preferred} 已被其他服务占用，改用 ${fallback}。" >&2
    if ! port_is_available "$fallback"; then
      echo "❌ 备用端口 $fallback 也被占用；请释放端口或在环境变量中指定其他端口。" >&2
      exit 1
    fi
    echo "$fallback"
  fi
}

existing_compose_port() {
  local service="$1"
  local container_port="$2"
  local mapping=""
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

replace_local_port() {
  local url="$1"
  local old_port="$2"
  local new_port="$3"
  if [[ "$url" == *"127.0.0.1:${old_port}"* ]] || [[ "$url" == *"localhost:${old_port}"* ]]; then
    echo "${url/:${old_port}/:${new_port}}"
  else
    echo "$url"
  fi
}

detect_runtime_core_path() {
  if [ -n "$AGENT_SKILL_RUNTIME_CORE_PATH" ]; then
    echo "$AGENT_SKILL_RUNTIME_CORE_PATH"
    return
  fi

  for candidate in \
    "$PROJECT_DIR/agent_skill_runtime_core" \
    "$PROJECT_DIR/../agent_skill_runtime_core" \
    "$PROJECT_DIR/../hailiang-skills/agent_skill_runtime_core"; do
    if [ -d "$candidate/agent_skill_runtime_core" ]; then
      echo "$candidate"
      return
    fi
  done

  echo ""
}

assert_runtime_core() {
  local runtime_core_path="$1"
  if [ -z "$runtime_core_path" ]; then
    echo "❌ 未找到 agent_skill_runtime_core。"
    echo "请把共享 runtime core 放到服务器，并设置："
    echo "  export AGENT_SKILL_RUNTIME_CORE_PATH=/path/to/agent_skill_runtime_core"
    exit 1
  fi

  if [ ! -d "$runtime_core_path/agent_skill_runtime_core" ]; then
    echo "❌ AGENT_SKILL_RUNTIME_CORE_PATH 不正确：$runtime_core_path"
    echo "期望存在：$runtime_core_path/agent_skill_runtime_core"
    exit 1
  fi
}

resolve_project_path() {
  local path_value="$1"
  if [[ "$path_value" = /* ]]; then
    echo "$path_value"
  else
    echo "$PROJECT_DIR/$path_value"
  fi
}

is_truthy() {
  case "${1:-}" in
    1|true|TRUE|True|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

prepare_runtime_config_and_dirs() {
  local runtime_path soul_path
  runtime_path="$(resolve_project_path "$HAILIANG_RUNTIME_DIR")"
  soul_path="$(resolve_project_path "$HAILIANG_SOUL_PATH")"

  echo "🔎 验证 runtime bridge 配置..."
  if [ ! -f "$PROJECT_DIR/config/runtime.yml" ]; then
    echo "❌ 缺少 runtime 配置：$PROJECT_DIR/config/runtime.yml"
    exit 1
  fi
  if [ ! -f "$soul_path" ]; then
    echo "⚠️  未找到 Soul 文件：$soul_path"
    echo "   将创建空文件；如需注入 Soul，请部署前写入内容。"
    mkdir -p "$(dirname "$soul_path")"
    : > "$soul_path"
  fi

  mkdir -p \
    "$runtime_path" \
    "$runtime_path/conversation_memories" \
    "$runtime_path/logs" \
    "$runtime_path/sandbox_deps" \
    "$runtime_path/sandbox_warmups" \
    "$runtime_path/sessions"
  if ! touch "$runtime_path/.write-test" 2>/dev/null; then
    echo "❌ runtime 目录不可写：$runtime_path"
    exit 1
  fi
  rm -f "$runtime_path/.write-test"

  echo "✅ runtime_dir：$runtime_path"
  echo "✅ soul_path：$soul_path"
  echo "✅ memory_enabled：$HAILIANG_MEMORY_ENABLED"
  echo "✅ sandbox_prewarm_enabled：$HAILIANG_SANDBOX_PREWARM_ENABLED"
}

check_docker_sandbox() {
  if ! is_truthy "$HAILIANG_SANDBOX_PREWARM_ENABLED"; then
    echo "ℹ️  sandbox prewarm 已关闭，跳过 Docker 检查。"
    return 0
  fi

  if ! command -v docker >/dev/null 2>&1; then
    echo "⚠️  未检测到 docker 命令。"
    echo "   服务仍可启动，但 native Skill 脚本 sandbox 执行会被跳过并记录 warning。"
    if is_truthy "$REQUIRE_DOCKER_SANDBOX"; then
      echo "❌ REQUIRE_DOCKER_SANDBOX=1，Docker 不可用，终止部署。"
      exit 1
    fi
    return 0
  fi

  if ! docker info >/tmp/hailiang-docker-info.txt 2>&1; then
    echo "⚠️  docker 命令存在，但 daemon 不可用或当前用户无权限。"
    echo "   详情：$(tail -n 1 /tmp/hailiang-docker-info.txt)"
    echo "   服务仍可启动，但脚本 sandbox 执行会被跳过。"
    if is_truthy "$REQUIRE_DOCKER_SANDBOX"; then
      echo "❌ REQUIRE_DOCKER_SANDBOX=1，Docker daemon 不可用，终止部署。"
      exit 1
    fi
    return 0
  fi

  echo "✅ Docker sandbox 可用。"
}

detect_server_ip() {
  if [ -n "$SERVER_IP" ]; then
    echo "$SERVER_IP"
    return
  fi

  local host_ip=""
  host_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  if [ -n "$host_ip" ]; then
    echo "$host_ip"
    return
  fi

  if command -v ip >/dev/null 2>&1; then
    local route_ip=""
    route_ip="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)"
    if [ -n "$route_ip" ]; then
      echo "$route_ip"
      return
    fi
  fi

  echo "127.0.0.1"
}

kill_port() {
  local port="$1"
  local pid=""
  if command -v lsof >/dev/null 2>&1; then
    pid="$(lsof -t -i:"$port" 2>/dev/null || true)"
    if [ -n "$pid" ]; then
      echo "🔪 关闭端口 $port 上的旧进程：$pid"
      kill $pid 2>/dev/null || true
      sleep 2
      kill -9 $pid 2>/dev/null || true
    fi
    return
  fi

  if command -v fuser >/dev/null 2>&1; then
    echo "🔪 尝试关闭端口 $port 上的旧进程"
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
  fi
}

require_value() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "❌ 缺少环境变量：$name"
    echo "   请填写 env.local.sh（本地）或服务器的受保护环境变量文件。"
    exit 1
  fi
}

assert_production_storage_config() {
  case "$HAILIANG_STORAGE_BACKEND" in
    postgres)
      require_value HAILIANG_DATABASE_URL
      require_value HAILIANG_REDIS_URL
      require_value HAILIANG_AUDIT_ENCRYPTION_KEY
      ;;
    file)
      echo "⚠️  当前以 file 模式部署：仅适用于临时调试，不能多实例部署，也不会写 PostgreSQL。"
      ;;
    *)
      echo "❌ HAILIANG_STORAGE_BACKEND 只能是 postgres 或 file，当前值：$HAILIANG_STORAGE_BACKEND"
      exit 1
      ;;
  esac
}

prepare_local_infra() {
  if [ "$HAILIANG_STORAGE_BACKEND" != "postgres" ] || ! is_truthy "$START_INFRA"; then
    return 0
  fi
  require_command docker
  if ! docker info >/tmp/hailiang-deploy-docker-info.txt 2>&1; then
    echo "❌ Docker daemon 不可用，无法自动启动 PostgreSQL / Redis。"
    echo "   详情：$(tail -n 1 /tmp/hailiang-deploy-docker-info.txt)"
    echo "   若你使用外部数据库/Redis，请设置 START_INFRA=0 并填写真实的 HAILIANG_DATABASE_URL / HAILIANG_REDIS_URL。"
    exit 1
  fi

  POSTGRES_HOST_PORT="$(resolve_infra_port postgres 5432 "$POSTGRES_HOST_PORT" 15432)"
  REDIS_HOST_PORT="$(resolve_infra_port redis 6379 "$REDIS_HOST_PORT" 16379)"
  OTEL_HOST_PORT="$(resolve_infra_port otel-collector 4318 "$OTEL_HOST_PORT" 14318)"
  OTEL_METRICS_HOST_PORT="$(resolve_infra_port otel-collector 9464 "$OTEL_METRICS_HOST_PORT" 19464)"
  export POSTGRES_HOST_PORT REDIS_HOST_PORT OTEL_HOST_PORT OTEL_METRICS_HOST_PORT

  HAILIANG_DATABASE_URL="$(replace_local_port "$HAILIANG_DATABASE_URL" 5432 "$POSTGRES_HOST_PORT")"
  HAILIANG_REDIS_URL="$(replace_local_port "$HAILIANG_REDIS_URL" 6379 "$REDIS_HOST_PORT")"
  if [ -z "$OTEL_EXPORTER_OTLP_ENDPOINT" ]; then
    OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:${OTEL_HOST_PORT}"
  else
    OTEL_EXPORTER_OTLP_ENDPOINT="$(replace_local_port "$OTEL_EXPORTER_OTLP_ENDPOINT" 4318 "$OTEL_HOST_PORT")"
  fi
  export HAILIANG_DATABASE_URL HAILIANG_REDIS_URL OTEL_EXPORTER_OTLP_ENDPOINT

  echo "🐳 启动本地 PostgreSQL、Redis 与观测 Collector..."
  echo "   PostgreSQL: $HAILIANG_DATABASE_URL"
  echo "   Redis:      $HAILIANG_REDIS_URL"
  docker compose up -d postgres redis otel-collector
}

prepare_database() {
  if [ "$HAILIANG_STORAGE_BACKEND" != "postgres" ]; then
    return 0
  fi
  echo "🗃️  检查 PostgreSQL 与 Redis 连接..."
  HAILIANG_DATABASE_URL="$HAILIANG_DATABASE_URL" HAILIANG_REDIS_URL="$HAILIANG_REDIS_URL" \
    "$VENV_DIR/bin/python" - <<'PY'
from hailiang_skills.storage.database import build_engine
from hailiang_skills.core.concurrency import TurnCoordinator

with build_engine().connect() as connection:
    connection.exec_driver_sql("SELECT 1")
if not TurnCoordinator().ready():
    raise SystemExit("Redis ping failed")
print("✅ PostgreSQL 与 Redis 可用")
PY
  if is_truthy "$RUN_DB_MIGRATIONS"; then
    echo "🗃️  执行 Alembic 数据库迁移..."
    HAILIANG_DATABASE_URL="$HAILIANG_DATABASE_URL" PYTHONPATH="$PROJECT_DIR/src" \
      "$VENV_DIR/bin/alembic" upgrade head
  else
    echo "ℹ️  RUN_DB_MIGRATIONS=0，跳过数据库结构升级。"
  fi
}

wait_for_health() {
  local url="$1"
  local retries="${2:-30}"
  for _ in $(seq 1 "$retries"); do
    if curl -fsS "$url" >/tmp/hailiang-health.json 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

install_legacy_ms_agent_constraints() {
  if [ "$MS_AGENT_LEGACY_WHEELS" != "1" ]; then
    return 0
  fi
  if [ ! -f "$LEGACY_MS_AGENT_CONSTRAINTS_FILE" ]; then
    echo "⚠️  未找到老旧 Linux 约束文件：$LEGACY_MS_AGENT_CONSTRAINTS_FILE"
    echo "   将直接安装项目依赖。"
    return 0
  fi

  echo "🧩 检测到老旧 Linux 约束文件：$LEGACY_MS_AGENT_CONSTRAINTS_FILE"
  echo "   先安装兼容 wheel 版本，减少 faiss-cpu / pandas / matplotlib 等依赖源码编译。"
  if python -m pip install -r "$LEGACY_MS_AGENT_CONSTRAINTS_FILE" -i "$PIP_INDEX_URL"; then
    echo "✅ 兼容依赖安装成功。"
  else
    echo "⚠️  兼容依赖安装失败，继续尝试安装项目依赖。"
  fi
}

install_backend_project() {
  local mode="$INSTALL_MS_AGENT_RUNTIME"
  local constraint_args=()

  case "$mode" in
    0|false|False|no|NO|skip|SKIP)
      echo "⚠️  已跳过 MS-Agent runtime 安装，仅安装后端基础依赖。"
      python -m pip install \
        "fastapi>=0.115.0" \
        "starlette>=0.40.0,<0.46.0" \
        "uvicorn>=0.30.0" \
        "pydantic>=2.8.0" \
        "sqlalchemy>=2.0.0" \
        "PyYAML>=6.0" \
        "loguru>=0.7.0" \
        -i "$PIP_INDEX_URL"
      python -m pip install --no-deps -e . -i "$PIP_INDEX_URL"
      MS_AGENT_RUNTIME_READY=0
      return 0
      ;;
  esac

  install_legacy_ms_agent_constraints
  if [ "$MS_AGENT_LEGACY_WHEELS" = "1" ] && [ -f "$LEGACY_MS_AGENT_CONSTRAINTS_FILE" ]; then
    constraint_args=(-c "$LEGACY_MS_AGENT_CONSTRAINTS_FILE")
  fi

  echo "🚚 安装后端项目依赖（包含 ms-agent runtime）..."
  if python -m pip install "${constraint_args[@]}" -e . -i "$PIP_INDEX_URL"; then
    MS_AGENT_RUNTIME_READY=1
    echo "✅ MS-Agent runtime 安装成功。"
    return 0
  fi

  if [ "$mode" = "1" ] || [ "$mode" = "true" ] || [ "$mode" = "TRUE" ] || [ "$mode" = "yes" ] || [ "$mode" = "YES" ]; then
    echo "❌ MS-Agent runtime 安装失败，且当前为强制安装模式。"
    echo "常见原因：服务器 GCC / glibc 版本过旧，无法构建 faiss-cpu、pandas、matplotlib、contourpy 或 numpy 2.x。"
    echo "可先确认约束文件是否存在：$LEGACY_MS_AGENT_CONSTRAINTS_FILE"
    echo "如只需先启动排查，可改用 INSTALL_MS_AGENT_RUNTIME=auto 或 INSTALL_MS_AGENT_RUNTIME=0。"
    exit 1
  fi

  echo "⚠️  MS-Agent runtime 安装失败，将以降级方式安装基础依赖并继续部署。"
  echo "   正式对话链路会返回 runtime unavailable，不会静默走旧 runtime。"
  python -m pip install \
    "fastapi>=0.115.0" \
    "starlette>=0.40.0,<0.46.0" \
    "uvicorn>=0.30.0" \
    "pydantic>=2.8.0" \
    "sqlalchemy>=2.0.0" \
    "PyYAML>=6.0" \
    "loguru>=0.7.0" \
    -i "$PIP_INDEX_URL"
  python -m pip install --no-deps -e . -i "$PIP_INDEX_URL"
  MS_AGENT_RUNTIME_READY=0
}

verify_backend_imports() {
  local modules=("hailiang_skills.api.main" "agent_skill_runtime_core")
  if [ "$MS_AGENT_RUNTIME_READY" = "1" ]; then
    modules+=("ms_agent" "loguru")
  fi

  PYTHONPATH="$PROJECT_DIR/src:$AGENT_SKILL_RUNTIME_CORE_PATH" "$VENV_DIR/bin/python" - "${modules[@]}" <<'PY'
import importlib
import sys

for module in sys.argv[1:]:
    importlib.import_module(module)
print("✅ backend imports passed:", ", ".join(sys.argv[1:]))
PY
}

assert_runtime_health() {
  HAILIANG_PROJECT_DIR="$PROJECT_DIR" "$VENV_DIR/bin/python" - <<'PY'
import json
import os
from pathlib import Path

import yaml

payload = json.loads(Path("/tmp/hailiang-health.json").read_text(encoding="utf-8"))
runtime = payload.get("runtime") or {}
entry = runtime.get("entry_skill")
skills = set(runtime.get("skills") or [])
expected = {
    "career_plan_entity",
    "future_explore",
    "interest_explore",
    "junior_multi_path_planning",
    "score_improve",
    "subject_advisor",
    "mock_admission",
    "multi_path_planning",
}
config_path = Path(os.environ["HAILIANG_PROJECT_DIR"]) / "config" / "runtime.yml"
config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
skill_management = config.get("skill_management") or {}
enabled_config = skill_management.get("enabled") or {}
disabled = {
    str(skill_id).strip()
    for skill_id, enabled in enabled_config.items()
    if isinstance(skill_id, str) and enabled is False
}
# Missing entries remain enabled, matching the server-side SkillRegistry.
required = expected - disabled
missing = sorted(required - skills)
if entry != "general_chat" or missing:
    raise SystemExit(
        "runtime health check failed: "
        f"entry_skill={entry!r}, missing_skills={missing}"
    )
print("✅ runtime health check passed: general_chat + career_plan_entity + 子场景已加载")
PY
}

require_command "$PYTHON"
require_command npm
require_command curl
if ! command -v lsof >/dev/null 2>&1 && ! command -v fuser >/dev/null 2>&1; then
  echo "❌ 缺少命令：lsof 或 fuser（二者至少需要一个用于释放端口）"
  exit 1
fi

SERVER_IP="$(detect_server_ip)"
if [ -z "$PUBLIC_API_BASE_URL" ]; then
  PUBLIC_API_BASE_URL="http://${SERVER_IP}:${BACKEND_PORT}"
fi

AGENT_SKILL_RUNTIME_CORE_PATH="$(detect_runtime_core_path)"
assert_runtime_core "$AGENT_SKILL_RUNTIME_CORE_PATH"
export AGENT_SKILL_RUNTIME_CORE_PATH
assert_production_storage_config

if [ -z "${SSL_CERT_FILE:-}" ]; then
  for ca_file in \
    /etc/pki/tls/cert.pem \
    /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \
    /etc/ssl/certs/ca-certificates.crt \
    /etc/ssl/cert.pem; do
    if [ -f "$ca_file" ]; then
      export SSL_CERT_FILE="$ca_file"
      break
    fi
  done
fi

echo "✅ 项目目录：$PROJECT_DIR"
echo "✅ 共享 runtime core：$AGENT_SKILL_RUNTIME_CORE_PATH"
echo "✅ 服务器地址：$SERVER_IP"
echo "✅ 前端接口地址：$PUBLIC_API_BASE_URL"
echo "✅ MS-Agent runtime 安装模式：$INSTALL_MS_AGENT_RUNTIME"
echo "✅ HAILIANG_RUNTIME_DIR：$HAILIANG_RUNTIME_DIR"
echo "✅ HAILIANG_SOUL_PATH：$HAILIANG_SOUL_PATH"
echo "✅ 存储模式：$HAILIANG_STORAGE_BACKEND"
echo "✅ START_INFRA：$START_INFRA"
echo "✅ Uvicorn workers：$UVICORN_WORKERS（每 worker 最多 $HAILIANG_STREAM_WORKERS 条流）"
if [ -n "${SSL_CERT_FILE:-}" ]; then
  echo "✅ SSL_CERT_FILE：$SSL_CERT_FILE"
fi

cd "$PROJECT_DIR"
prepare_runtime_config_and_dirs
check_docker_sandbox
prepare_local_infra

# --------------------- 1. 后端 ---------------------
echo -e "\n[1/3] 构建后端环境"

if [ "$RECREATE_VENV" = "1" ]; then
  rm -rf "$VENV_DIR"
fi

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip -i "$PIP_INDEX_URL"
python -m pip install --only-binary :all: greenlet -i "$PIP_INDEX_URL"
install_backend_project
echo "🔎 验证后端依赖导入..."
verify_backend_imports
prepare_database

echo "🔎 验证 runtime 目录..."
test -d "$PROJECT_DIR/src/hailiang_skills/skill_runtime"
test -d "$PROJECT_DIR/runtime_skills"
test -d "$PROJECT_DIR/assets/generated"
test -f "$PROJECT_DIR/assets/generated/asset_registry.json"
test -f "$PROJECT_DIR/assets/generated/tool_registry.json"


if [ -z "$DASHSCOPE_API_KEY" ]; then
  echo "⚠️  未检测到 DASHSCOPE_API_KEY，后端会启动，但 runtime 原生 Skill 无法调用模型。"
else
  export DASHSCOPE_API_KEY
fi

kill_port "$BACKEND_PORT"

echo "🚀 启动后端服务..."
nohup env \
  PYTHONPATH="$PROJECT_DIR/src:$AGENT_SKILL_RUNTIME_CORE_PATH" \
  AGENT_SKILL_RUNTIME_CORE_PATH="$AGENT_SKILL_RUNTIME_CORE_PATH" \
  DASHSCOPE_API_KEY="${DASHSCOPE_API_KEY:-}" \
  SSL_CERT_FILE="${SSL_CERT_FILE:-}" \
  SSL_CERT_DIR="${SSL_CERT_DIR:-}" \
  HAILIANG_RUNTIME_DIR="$HAILIANG_RUNTIME_DIR" \
  HAILIANG_SOUL_PATH="$HAILIANG_SOUL_PATH" \
  HAILIANG_MEMORY_ENABLED="$HAILIANG_MEMORY_ENABLED" \
  HAILIANG_SANDBOX_PREWARM_ENABLED="$HAILIANG_SANDBOX_PREWARM_ENABLED" \
  HAILIANG_STORAGE_BACKEND="$HAILIANG_STORAGE_BACKEND" \
  HAILIANG_DATABASE_URL="$HAILIANG_DATABASE_URL" \
  HAILIANG_REDIS_URL="$HAILIANG_REDIS_URL" \
  HAILIANG_AUDIT_ENCRYPTION_KEY="$HAILIANG_AUDIT_ENCRYPTION_KEY" \
  HAILIANG_AUDIT_KEY_ID="$HAILIANG_AUDIT_KEY_ID" \
  HAILIANG_AUDIT_RETENTION_DAYS="$HAILIANG_AUDIT_RETENTION_DAYS" \
  HAILIANG_MAX_SSE_CONNECTIONS="$HAILIANG_MAX_SSE_CONNECTIONS" \
  HAILIANG_STREAM_WORKERS="$HAILIANG_STREAM_WORKERS" \
  HAILIANG_SSE_QUEUE_TIMEOUT_SECONDS="$HAILIANG_SSE_QUEUE_TIMEOUT_SECONDS" \
  OTEL_EXPORTER_OTLP_ENDPOINT="$OTEL_EXPORTER_OTLP_ENDPOINT" \
  "$VENV_DIR/bin/python" -m uvicorn hailiang_skills.api.main:app \
  --host 0.0.0.0 \
  --port "$BACKEND_PORT" \
  --workers "$UVICORN_WORKERS" \
  --timeout-keep-alive 15 \
  > "$PROJECT_DIR/backend.log" 2>&1 &

if ! wait_for_health "http://127.0.0.1:${BACKEND_PORT}/health/ready" 45; then
  echo "❌ 后端健康检查失败，请查看：tail -f $PROJECT_DIR/backend.log"
  exit 1
fi

if ! curl -fsS "http://127.0.0.1:${BACKEND_PORT}/health" >/tmp/hailiang-health.json; then
  echo "❌ 无法获取后端 /health，请查看：tail -f $PROJECT_DIR/backend.log"
  exit 1
fi
assert_runtime_health

# --------------------- 2. 前端 ---------------------
echo -e "\n[2/3] 构建并启动前端"

cd "$FRONTEND_DIR"
npm config set registry "$NPM_REGISTRY"
if [ -f package-lock.json ]; then
  npm ci
else
  npm install
fi

mkdir -p public
cat > public/runtime-config.js <<EOF
window.__HAILIANG_RUNTIME_CONFIG__ = {
  apiBaseUrl: "${PUBLIC_API_BASE_URL}",
  backendPort: ${BACKEND_PORT},
  userId: "${DEFAULT_USER_ID}"
};
EOF
echo "✅ 已写入前端运行时配置：$PUBLIC_API_BASE_URL"

npm run build

kill_port "$FRONTEND_PORT"

nohup npm run preview -- --host 0.0.0.0 --port "$FRONTEND_PORT" \
  > "$FRONTEND_DIR/frontend.log" 2>&1 &

if ! wait_for_health "http://127.0.0.1:${FRONTEND_PORT}" 30; then
  echo "❌ 前端启动失败，请查看：tail -f $FRONTEND_DIR/frontend.log"
  exit 1
fi

# --------------------- 3. 输出 ---------------------
echo -e "\n[3/3] 部署完成"
echo "============================================="
echo "✅ 部署成功！"
echo "前端地址：http://${SERVER_IP}:${FRONTEND_PORT}"
echo "后端地址：http://${SERVER_IP}:${BACKEND_PORT}"
echo "存活检查：http://${SERVER_IP}:${BACKEND_PORT}/health/live"
echo "就绪检查：http://${SERVER_IP}:${BACKEND_PORT}/health/ready"
echo "指标地址：http://${SERVER_IP}:${BACKEND_PORT}/metrics"
if [ "$MS_AGENT_RUNTIME_READY" = "1" ]; then
  echo "MS-Agent runtime：已安装，可使用真实单 Skill runtime 能力"
else
  echo "MS-Agent runtime：未完整安装，正式 runtime 对话会返回 unavailable"
fi
echo "前端日志：tail -f $FRONTEND_DIR/frontend.log"
echo "后端日志：tail -f $PROJECT_DIR/backend.log"
echo ""
echo "常用覆盖参数："
echo "  BACKEND_PORT=8010 FRONTEND_PORT=4174 ./deploy-all.sh"
echo "  AGENT_SKILL_RUNTIME_CORE_PATH=/path/to/agent_skill_runtime_core ./deploy-all.sh"
echo "  INSTALL_MS_AGENT_RUNTIME=1 MS_AGENT_LEGACY_WHEELS=1 ./deploy-all.sh"
echo "  INSTALL_MS_AGENT_RUNTIME=0 ./deploy-all.sh"
echo "  HAILIANG_SOUL_PATH=/path/to/soul.md HAILIANG_RUNTIME_DIR=/data/hailiang-runtime ./deploy-all.sh"
echo "  REQUIRE_DOCKER_SANDBOX=1 ./deploy-all.sh"
echo "  RUN_DB_MIGRATIONS=0 ./deploy-all.sh  # 已由发布平台完成迁移时使用"
echo "============================================="
