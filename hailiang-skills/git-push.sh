#!/usr/bin/env bash
# ============================================================
# hailiang-skills Git 推送 SOP 脚本（skill-runtime 版）
#
# 会确保这次 runtime 融合新增的目录被纳入版本：
#   - src/hailiang_skills/skill_runtime/
#   - src/hailiang_skills/runtime_bridge/
#   - runtime_skills/
#   - assets/generated/
#   - tests/test_runtime_bridge.py
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$SCRIPT_DIR"
ORIGIN_URL="${ORIGIN_URL:-https://github.com/ZhangYiPop/merge_hailiang_skill_runtime.git}"
BRANCH="${BRANCH:-main}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()   { echo -e "${RED}[ERR]${NC} $1"; }

cd "$REPO_DIR"

echo ""
echo "=============================================="
echo "  hailiang-skills Git 推送 SOP（runtime 版）"
echo "=============================================="
echo ""

log_info "Step 1: 检查 Git 仓库..."
if [ -d ".git" ]; then
  log_ok "Git 仓库已存在"
else
  log_warn "未找到 .git 目录，正在初始化..."
  git init
  git checkout -B "$BRANCH"
  log_ok "Git 仓库初始化完成"
fi

CURRENT_BRANCH="$(git branch --show-current 2>/dev/null || true)"
if [ -z "$CURRENT_BRANCH" ]; then
  git checkout -B "$BRANCH"
elif [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
  log_warn "当前分支为 $CURRENT_BRANCH，将切换/创建到 $BRANCH"
  git checkout -B "$BRANCH"
fi

log_info "Step 2: 设置远程仓库..."
CURRENT_REMOTE="$(git remote get-url origin 2>/dev/null || true)"
if [ -z "$CURRENT_REMOTE" ]; then
  git remote add origin "$ORIGIN_URL"
  log_ok "已添加 origin: $ORIGIN_URL"
elif [ "$CURRENT_REMOTE" != "$ORIGIN_URL" ]; then
  log_warn "origin URL 不同，正在更新..."
  git remote set-url origin "$ORIGIN_URL"
  log_ok "origin 已更新: $ORIGIN_URL"
else
  log_ok "origin 已配置: $CURRENT_REMOTE"
fi

log_info "Step 3: 写入/更新 .gitignore..."
cat > .gitignore <<'EOF'
.DS_Store
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/

.venv/
venv/
.env
.env.*

logs/
backend.log
frontend/frontend.log
.skill_runtime_cache/
.trae/

frontend/node_modules/
frontend/dist/

# runtime bridge 源码和生成资产需要提交，不要忽略：
# src/hailiang_skills/skill_runtime/
# src/hailiang_skills/runtime_bridge/
# runtime_skills/
# assets/generated/
EOF
log_ok ".gitignore 已更新"

log_info "Step 4: 检查 runtime 必需文件..."
required_paths=(
  "src/hailiang_skills/skill_runtime"
  "src/hailiang_skills/runtime_bridge"
  "runtime_skills/e生涯升学顾问v017/SKILL.md"
  "runtime_skills/e生涯前景探路v001/SKILL.md"
  "runtime_skills/e生涯提分规划v001/SKILL.md"
  "runtime_skills/e生涯兴趣探索v001/SKILL.md"
  "runtime_skills/e生涯选科参谋v001/SKILL.md"
  "runtime_skills/mock_admission/runtime_contract.json"
  "runtime_skills/multi_path_planning/runtime_contract.json"
  "assets/generated/asset_registry.json"
  "assets/generated/tool_registry.json"
  "tests/test_runtime_bridge.py"
)
for path in "${required_paths[@]}"; do
  if [ ! -e "$path" ]; then
    log_err "缺少必需文件/目录：$path"
    exit 1
  fi
done
log_ok "runtime 必需文件齐全"

log_info "Step 5: 获取远程最新状态..."
GIT_SSL_NO_VERIFY=true git fetch origin "$BRANCH" 2>/dev/null || log_warn "fetch 失败，继续本地提交流程"

STATUS_OUTPUT="$(git status --short 2>&1)"
if [ -z "$STATUS_OUTPUT" ]; then
  log_ok "工作区干净，没有需要提交的修改"
  exit 0
fi

echo ""
echo "当前修改："
echo "$STATUS_OUTPUT"
echo ""

log_info "Step 6: 提交代码..."
echo "请选择提交模式："
echo "  1) 使用默认 commit 信息（推荐）"
echo "  2) 输入自定义 commit 信息"
echo "  3) 查看详细 diff（不提交）"
echo "  4) 退出"
read -r -p "请输入选项 [1]: " CHOICE
CHOICE="${CHOICE:-1}"

case "$CHOICE" in
  1)
    COMMIT_MSG="feat: integrate skill-runtime main planner"
    ;;
  2)
    read -r -p "请输入 commit 信息: " COMMIT_MSG
    if [ -z "$COMMIT_MSG" ]; then
      log_err "commit 信息不能为空"
      exit 1
    fi
    ;;
  3)
    git diff
    exit 0
    ;;
  4)
    log_info "已退出"
    exit 0
    ;;
  *)
    log_err "无效选项"
    exit 1
    ;;
esac

log_info "暂存文件..."
git add \
  .gitignore \
  pyproject.toml \
  deploy-all.sh \
  git-push.sh \
  src \
  config \
  assets \
  data \
  runtime_skills \
  tests \
  frontend \
  README.md \
  CHANGELOG.md \
  guides \
  scripts \
  docs \
  docker-compose.yml \
  run.sh \
  stop-all.sh \
  2>/dev/null || true

if git diff --cached --quiet; then
  log_warn "暂存区为空，没有可提交内容"
  exit 0
fi

git commit -m "$COMMIT_MSG"
log_ok "提交完成: $(git log --oneline -1)"

log_info "Step 7: 推送到远程..."
push_success=false

if GIT_SSL_NO_VERIFY=true git push -u origin "$BRANCH"; then
  push_success=true
fi

if [ "$push_success" = "false" ]; then
  log_warn "标准推送失败，尝试 HTTP/1.1..."
  if GIT_HTTP_VERSION=HTTP/1.1 GIT_SSL_NO_VERIFY=true git push -u origin "$BRANCH"; then
    push_success=true
  fi
fi

if [ "$push_success" = "false" ]; then
  log_warn "仍然失败，尝试 SSH..."
  SSH_URL="git@github.com:ZhangYiPop/merge_hailiang_skill_runtime.git"
  git remote set-url origin "$SSH_URL"
  if git push -u origin "$BRANCH"; then
    push_success=true
  fi
  git remote set-url origin "$ORIGIN_URL"
fi

if [ "$push_success" = "true" ]; then
  echo ""
  echo "=============================================="
  log_ok "推送成功！"
  log_info "仓库地址: $ORIGIN_URL"
  log_info "当前提交: $(git log --oneline -1)"
  echo "=============================================="
else
  echo ""
  echo "=============================================="
  log_err "推送失败，请检查网络或权限后重试"
  echo "手动命令：git push -u origin $BRANCH"
  echo "=============================================="
  exit 1
fi
