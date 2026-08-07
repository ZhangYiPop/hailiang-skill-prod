#!/usr/bin/env bash
#
# Hailiang Skills 环境变量入口（请使用 source env.sh，不要直接执行）。
#
# 密钥、服务器地址和本机路径必须放在 env.local.sh：该文件已被 .gitignore 忽略，
# 不会提交到 Git。首次配置可执行：
#   cp env.example.sh env.local.sh
#   chmod 600 env.local.sh
# 然后编辑 env.local.sh 填入真实值。

_HAILIANG_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_HAILIANG_LOCAL_ENV="${HAILIANG_ENV_FILE:-$_HAILIANG_ENV_DIR/env.local.sh}"

if [ -f "$_HAILIANG_LOCAL_ENV" ]; then
  # shellcheck disable=SC1090
  source "$_HAILIANG_LOCAL_ENV"
else
  echo "⚠️  未找到本机私有配置：$_HAILIANG_LOCAL_ENV"
  echo "   请先执行：cp env.example.sh env.local.sh，然后填写密钥和路径。"
fi

# 以下为可安全提交的默认值。env.local.sh 可以覆盖它们。
export HAILIANG_STORAGE_BACKEND="${HAILIANG_STORAGE_BACKEND:-postgres}"
export HAILIANG_DATABASE_URL="${HAILIANG_DATABASE_URL:-postgresql+psycopg://hailiang:hailiang@127.0.0.1:5432/hailiang_skills}"
export HAILIANG_REDIS_URL="${HAILIANG_REDIS_URL:-redis://127.0.0.1:6379/0}"
export HAILIANG_MAX_SSE_CONNECTIONS="${HAILIANG_MAX_SSE_CONNECTIONS:-100}"
export HAILIANG_STREAM_WORKERS="${HAILIANG_STREAM_WORKERS:-50}"
export HAILIANG_SSE_QUEUE_TIMEOUT_SECONDS="${HAILIANG_SSE_QUEUE_TIMEOUT_SECONDS:-5}"
export HAILIANG_AUDIT_RETENTION_DAYS="${HAILIANG_AUDIT_RETENTION_DAYS:-90}"
export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://127.0.0.1:4318}"
export BACKEND_PORT="${BACKEND_PORT:-8010}"
export FRONTEND_PORT="${FRONTEND_PORT:-4175}"
export HAILIANG_CORS_ORIGINS="${HAILIANG_CORS_ORIGINS:-http://127.0.0.1:${FRONTEND_PORT},http://localhost:${FRONTEND_PORT}}"

unset _HAILIANG_ENV_DIR _HAILIANG_LOCAL_ENV
