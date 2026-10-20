#!/usr/bin/env bash
# Isolated 8011/5178 smoke deployment for the current local Harness setup.
# It layers on the server's existing private 8010 credentials/configuration,
# then overrides every resource that must not be shared with 8010.
#
# Optional override when the base private file is stored elsewhere:
#   HAILIANG_8011_BASE_ENV=/secure/env.8010.sh ./deploy-smoke.sh --env ./env.8011.sh ...

BASE_ENV="${HAILIANG_8011_BASE_ENV:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.8010.sh}"
if [ ! -r "$BASE_ENV" ]; then
  echo "env.8011.sh requires the existing private base environment: $BASE_ENV" >&2
  return 2 2>/dev/null || exit 2
fi
# shellcheck disable=SC1090
source "$BASE_ENV"

export HAILIANG_DEPLOY_ENV=test
export HAILIANG_RELEASE_VERSION=4.11.0.1-smoke-8011
export HAILIANG_BIND_HOST=10.30.6.45
export BACKEND_PORT=8011
export WORKBENCH_PORT=8021
export HAILIANG_FRONTEND_BIND_HOST=10.30.6.45
export FRONTEND_PORT=5178
export HAILIANG_PUBLIC_API_BASE_URL=http://10.30.6.45:8011
# deploy-smoke.sh appends the 5178 origin when --with-frontend is used.
export HAILIANG_CORS_ORIGINS=

# Dedicated PostgreSQL database and Redis namespace. The database is created
# by deploy-smoke.sh when absent; never point this file at the 8010 database.
export HAILIANG_SMOKE_DATABASE_NAME=hailiang_skills_test_multi_profile_v1_smoke_8011
export HAILIANG_DATABASE_URL="${HAILIANG_DATABASE_URL%/*}/$HAILIANG_SMOKE_DATABASE_NAME"
export HAILIANG_REDIS_URL="${HAILIANG_REDIS_URL%/*}/4"
export HAILIANG_REDIS_KEY_PREFIX='hailiang:smoke:8011:'
export HAILIANG_AUDIT_KEY_ID='smoke-8011'

# Keep runtime artifacts, Python dependencies, and logs separate from 8010.
export HAILIANG_SMOKE_VENV_DIR='/home/hljy/Project/for_930/hailiang-skill_sensitive_v02121_base/hailiang-skills/.venv-smoke-8011'
export HAILIANG_RUNTIME_DIR='/home/hljy/Project/for_930/hailiang-skill_sensitive_v02121_base/hailiang-skills/runtime-smoke-8011'
export HAILIANG_LOG_DIR='/home/hljy/Project/for_930/hailiang-skill_sensitive_v02121_base/hailiang-skills/logs-smoke-8011'
export HAILIANG_SMOKE_LOG_FILE='/home/hljy/Project/for_930/hailiang-skill_sensitive_v02121_base/hailiang-skills/smoke-8011.log'
export HAILIANG_SMOKE_FRONTEND_LOG_FILE='/home/hljy/Project/for_930/hailiang-skill_sensitive_v02121_base/hailiang-skills/smoke-frontend-5178.log'

# Mirror the local run.sh Harness configuration used for SSE v3 validation.
export HAILIANG_TEST_LLM_ENABLED=true
export HAILIANG_TEST_LLM_TIMEOUT_S=120
export HAILIANG_TEST_LLM_TEMPERATURE=0
export HAILIANG_TEST_LLM_MAX_TOKENS=8000
export HAILIANG_UNIFIED_CONTEXT_ENABLED=true
export HAILIANG_SKILL_QUESTION_UNIT_GATE_ENABLED=true
export HAILIANG_CONTEXT_POLICY=v2
export HAILIANG_HARNESS_MAX_RUN_INPUT_TOKENS=0
export HAILIANG_SSE_V3=true
export VITE_HAILIANG_SSE_V3=true
export HAILIANG_HARNESS_SSE_WAIT_SECONDS=600
