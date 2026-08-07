#!/usr/bin/env bash
set -euo pipefail

release="${1:?release directory required}"
cd "$release/frontend"
npm ci
npm run build

api_base_url="${HAILIANG_PUBLIC_API_BASE_URL:-http://${HAILIANG_BIND_HOST:?}:${BACKEND_PORT:?}}"
cat > dist/runtime-config.js <<EOF
window.__HAILIANG_RUNTIME_CONFIG__ = {
  apiBaseUrl: "${api_base_url}",
  backendPort: ${BACKEND_PORT},
  userId: "${DEFAULT_USER_ID:-debug-user}"
};
EOF
