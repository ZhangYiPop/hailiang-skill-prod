#!/usr/bin/env bash
# 启动逻辑调试台。会读取 hailiang-skills 的 env.sh，所以模型密钥和本地配置与正式联调一致。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILLS="$ROOT/hailiang-skills"
PYTHON="$SKILLS/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
  echo "缺少 $PYTHON。请先在 hailiang-skills 里准备虚拟环境。"
  exit 1
fi

# shellcheck disable=SC1091
source "$SKILLS/env.sh"
export PYTHONPATH="$SKILLS/src${PYTHONPATH:+:$PYTHONPATH}"
# 调试台自己的 HTTP 服务不能在启动时预热全部沙箱，否则子进程会拖死端口。
export HAILIANG_SANDBOX_PREWARM_ENABLED=0
cd "$SKILLS"
exec "$PYTHON" "$ROOT/agent-debug-console/server.py"
