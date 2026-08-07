#!/usr/bin/env bash
set -euo pipefail

expected_env="${1:?systemd instance env is required}"
[ "$expected_env" = "test" ] || [ "$expected_env" = "prod" ] || { echo "invalid environment" >&2; exit 2; }
[ "${HAILIANG_DEPLOY_ENV:-}" = "$expected_env" ] || { echo "HAILIANG_DEPLOY_ENV does not match service instance" >&2; exit 2; }
[ -f "VERSION" ] || { echo "release VERSION file is required" >&2; exit 2; }
[ -n "${DASHSCOPE_API_KEY:-}" ] || { echo "DASHSCOPE_API_KEY is required" >&2; exit 2; }
[ -n "${HAILIANG_AUDIT_ENCRYPTION_KEY:-}" ] || { echo "HAILIANG_AUDIT_ENCRYPTION_KEY is required" >&2; exit 2; }
[ -n "${HAILIANG_SECURITY_QUARANTINE_KEY:-}" ] || { echo "HAILIANG_SECURITY_QUARANTINE_KEY is required" >&2; exit 2; }
[ -n "${HAILIANG_DATABASE_URL:-}" ] || { echo "HAILIANG_DATABASE_URL is required" >&2; exit 2; }
[ "${HAILIANG_STORAGE_BACKEND:-}" = "postgres" ] || { echo "HAILIANG_STORAGE_BACKEND must be postgres" >&2; exit 2; }
[ -n "${HAILIANG_REDIS_URL:-}" ] || { echo "HAILIANG_REDIS_URL is required" >&2; exit 2; }
[ -n "${HAILIANG_REDIS_KEY_PREFIX:-}" ] || { echo "HAILIANG_REDIS_KEY_PREFIX is required" >&2; exit 2; }
[ -n "${HAILIANG_BIND_HOST:-}" ] && [ "${HAILIANG_BIND_HOST}" != "0.0.0.0" ] || { echo "a private HAILIANG_BIND_HOST is required" >&2; exit 2; }
[ -n "${HAILIANG_FRONTEND_BIND_HOST:-}" ] || { echo "HAILIANG_FRONTEND_BIND_HOST is required" >&2; exit 2; }
[ -n "${FRONTEND_PORT:-}" ] || { echo "FRONTEND_PORT is required" >&2; exit 2; }
[ -n "${HAILIANG_PUBLIC_API_BASE_URL:-}" ] || { echo "HAILIANG_PUBLIC_API_BASE_URL is required" >&2; exit 2; }
[ -n "${HAILIANG_CORS_ORIGINS:-}" ] || { echo "HAILIANG_CORS_ORIGINS is required for the internal frontend" >&2; exit 2; }

case "$expected_env" in
  test) [[ "$HAILIANG_DATABASE_URL" == *"hailiang_skills_test"* ]] && [[ "$HAILIANG_REDIS_URL" == */1 ]] && [[ "$HAILIANG_REDIS_KEY_PREFIX" == "hailiang:test:"* ]] ;;
  prod) [[ "$HAILIANG_DATABASE_URL" == *"hailiang_skills"* && "$HAILIANG_DATABASE_URL" != *"hailiang_skills_test"* ]] && [[ "$HAILIANG_REDIS_URL" == */2 ]] && [[ "$HAILIANG_REDIS_KEY_PREFIX" == "hailiang:prod:"* ]] ;;
esac || { echo "database, Redis DB, or key prefix does not match $expected_env" >&2; exit 2; }

for directory in "${HAILIANG_LOG_DIR:?}" "${HAILIANG_STATE_DIR:?}"; do
  mkdir -p "$directory"
  [ -w "$directory" ] || { echo "not writable: $directory" >&2; exit 2; }
done

"$(dirname "$0")/../../.venv/bin/python" - <<'PY'
import base64
import os
from hailiang_skills.core.rate_limit import get_llm_rate_limiter
from hailiang_skills.storage.database import build_engine

for key_name in ("HAILIANG_AUDIT_ENCRYPTION_KEY", "HAILIANG_SECURITY_QUARANTINE_KEY"):
    encoded = os.environ[key_name]
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except Exception as exc:
        raise SystemExit(f"{key_name} must be URL-safe base64") from exc
    if len(decoded) != 32:
        raise SystemExit(f"{key_name} must decode to exactly 32 bytes")

with build_engine().connect() as connection:
    connection.exec_driver_sql("SELECT 1")
if not get_llm_rate_limiter().ready():
    raise SystemExit("Redis/rate limiter is not ready")
PY
PYTHONPATH=src "$(dirname "$0")/../../.venv/bin/alembic" current >/dev/null

echo "environment validation passed: $expected_env"
