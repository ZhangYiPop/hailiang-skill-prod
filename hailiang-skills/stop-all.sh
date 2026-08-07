#!/usr/bin/env bash
# Kept only to make legacy automation fail safely. It must never kill all
# Python/Node processes on a shared production host.
set -euo pipefail
echo "stop-all.sh 已废弃；请使用 deploy/bin/test-stop.sh 或 deploy/bin/prod-stop.sh。" >&2
exit 2
