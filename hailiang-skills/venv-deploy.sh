#!/usr/bin/env bash
set -euo pipefail

# ========== 和原脚本保持一致的配置变量 ==========
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$PROJECT_DIR/env.sh" ]; then
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/env.sh"
else
  echo "⚠️  未找到 env.sh，继续使用当前 shell 环境和脚本默认配置。"
fi

VENV_DIR="${VENV_DIR:-$PROJECT_DIR/.venv}"
PYTHON="${PYTHON:-python3.11}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
RECREATE_VENV="${RECREATE_VENV:-1}"
INSTALL_MS_AGENT_RUNTIME="${INSTALL_MS_AGENT_RUNTIME:-auto}"
MS_AGENT_LEGACY_WHEELS="${MS_AGENT_LEGACY_WHEELS:-1}"
LEGACY_MS_AGENT_CONSTRAINTS_FILE="$PROJECT_DIR/constraints/linux-legacy-ms-agent.txt"
MS_AGENT_RUNTIME_READY=0

AGENT_SKILL_RUNTIME_CORE_PATH="${AGENT_SKILL_RUNTIME_CORE_PATH:-}"

# ---------- 工具函数 ----------
is_truthy() {
  case "${1:-}" in
    1|true|TRUE|True|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
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
  echo "   先安装兼容 wheel 版本，减少 faiss‑cpu / pandas / matplotlib 等依赖源码编译。"
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
      echo "⚠️  已跳过 MS‑Agent runtime 安装，仅安装后端基础依赖。"
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

  echo "🚚 安装后端项目依赖（包含 ms‑agent runtime）..."
  if python -m pip install "${constraint_args[@]}" -e . -i "$PIP_INDEX_URL"; then
    MS_AGENT_RUNTIME_READY=1
    echo "✅ MS‑Agent runtime 安装成功。"
    return 0
  fi

  if [ "$mode" = "1" ] || [ "$mode" = "true" ] || [ "$mode" = "TRUE" ] || [ "$mode" = "yes" ] || [ "$mode" = "YES" ]; then
    echo "❌ MS‑Agent runtime 安装失败，且当前为强制安装模式。"
    echo "常见原因：服务器 GCC / glibc 版本过旧，无法构建 faiss‑cpu、pandas、matplotlib、contourpy 或 numpy 2.x。"
    echo "可先确认约束文件是否存在：$LEGACY_MS_AGENT_CONSTRAINTS_FILE"
    echo "如只需先启动排查，可改用 INSTALL_MS_AGENT_RUNTIME=auto 或 INSTALL_MS_AGENT_RUNTIME=0。"
    exit 1
  fi

  echo "⚠️  MS‑Agent runtime 安装失败，将以降级方式安装基础依赖。"
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

# ========== 核心：venv 创建 + 依赖安装主流程 ==========
cd "$PROJECT_DIR"

if [ "$RECREATE_VENV" = "1" ]; then
  rm -rf "$VENV_DIR"
fi

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON" -m venv "$VENV_DIR"
fi

# 激活虚拟环境
source "$VENV_DIR/bin/activate"

# 更新pip；greenlet预编译wheel规避源码编译
python -m pip install --upgrade pip -i "$PIP_INDEX_URL"
python -m pip install --only-binary :all: greenlet -i "$PIP_INDEX_URL"

# 执行项目依赖安装（带CentOS7降级容错逻辑）
install_backend_project

# 做导入校验
echo "🔎 验证后端模块导入……"
verify_backend_imports

echo ""
echo "===== Python虚拟环境构建完成 ====="
echo "Venv路径: $VENV_DIR"
echo "MS_AGENT_RUNTIME_READY=${MS_AGENT_RUNTIME_READY}"
if [ "$MS_AGENT_RUNTIME_READY" = "1" ]; then
  echo "👉 ms‑agent/faiss‑cpu 完整可用"
else
  echo "👉 ms‑agent 重型扩展未安装，服务可启动，向量runtime不可用"
fi
echo "=================================="
