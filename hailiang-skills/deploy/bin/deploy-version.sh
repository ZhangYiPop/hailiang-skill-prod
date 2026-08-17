#!/usr/bin/env bash
# Build and publish one immutable Hailiang Skills release.
# Usage: deploy-version.sh test|prod VERSION SOURCE_ROOT
set -euo pipefail

environment="${1:?environment required: test or prod}"
version="${2:?version required}"
source_root="${3:?source root required}"

case "$environment" in
  test|prod) ;;
  *) echo "environment must be test or prod" >&2; exit 2 ;;
esac

[[ "$version" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "version contains unsupported characters: $version" >&2
  exit 2
}
[ -d "$source_root/hailiang-skills" ] || {
  echo "missing source project: $source_root/hailiang-skills" >&2
  exit 2
}
[ -f "$source_root/hailiang-skills/pyproject.toml" ] || {
  echo "pyproject.toml is missing from source project" >&2
  exit 2
}

project_source="$source_root/hailiang-skills"
release="/opt/hailiang-skills/releases/$version"
env_file="/etc/hailiang-skills/$environment.env"
runtime_source="$source_root/agent_skill_runtime_core"

if [ ! -f "$env_file" ]; then
  echo "environment file is missing: $env_file" >&2
  exit 2
fi

command -v rsync >/dev/null 2>&1 || { echo "rsync is required" >&2; exit 2; }
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  for candidate in /usr/local/bin/python3.11 /usr/bin/python3.11 "$(command -v python3.11 2>/dev/null || true)"; do
    if [ -x "$candidate" ]; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
[ -x "$PYTHON_BIN" ] || { echo "python3.11 is required (set PYTHON_BIN to its absolute path)" >&2; exit 2; }
command -v sudo >/dev/null 2>&1 || { echo "sudo is required" >&2; exit 2; }

if [ -e "$release" ]; then
  echo "release already exists: $release" >&2
  echo "Use a new version, or remove this incomplete release after inspection." >&2
  exit 3
fi

echo "==> Preparing release $version for $environment"
sudo mkdir -p "$release"
sudo rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '.venv-*' \
  --exclude 'node_modules' \
  --exclude 'frontend/dist' \
  --exclude 'logs' \
  --exclude 'runtime-*' \
  "$project_source/" "$release/"
printf '%s\n' "$version" | sudo tee "$release/VERSION" >/dev/null

if [ -d "$runtime_source" ]; then
  sudo mkdir -p /opt/agent-skill-runtime-core
  sudo rsync -a --delete --exclude '.git' "$runtime_source/" /opt/agent-skill-runtime-core/
fi

sudo chown -R hailiang:hailiang "$release" /opt/agent-skill-runtime-core

echo "==> Creating Python 3.11 virtual environment"
sudo -u hailiang "$PYTHON_BIN" -m venv "$release/.venv"
sudo -u hailiang "$release/.venv/bin/python" -m pip install --upgrade pip setuptools wheel \
  -i "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
  --default-timeout "${PIP_DEFAULT_TIMEOUT:-120}" --retries "${PIP_RETRIES:-5}"
sudo -u hailiang "$release/.venv/bin/python" -m pip install --only-binary :all: greenlet Pillow \
  -i "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
  --default-timeout "${PIP_DEFAULT_TIMEOUT:-120}" --retries "${PIP_RETRIES:-5}"

if [ -x "$release/venv-deploy.sh" ]; then
  echo "==> Installing backend dependencies using venv-deploy pipeline"
  sudo -u hailiang env \
    VENV_DIR="$release/.venv" \
    PYTHON="$PYTHON_BIN" \
    RECREATE_VENV=0 \
    PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
    INSTALL_MS_AGENT_RUNTIME="${INSTALL_MS_AGENT_RUNTIME:-auto}" \
    MS_AGENT_LEGACY_WHEELS="${MS_AGENT_LEGACY_WHEELS:-1}" \
    AGENT_SKILL_RUNTIME_CORE_PATH=/opt/agent-skill-runtime-core \
    bash "$release/venv-deploy.sh"
else
  sudo -u hailiang "$release/.venv/bin/python" -m pip install -e "$release" \
    -i "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
    --default-timeout "${PIP_DEFAULT_TIMEOUT:-120}" --retries "${PIP_RETRIES:-5}"
fi

sudo sed -i -E "s/^HAILIANG_RELEASE_VERSION=.*/HAILIANG_RELEASE_VERSION=$version/" "$env_file"
sudo chmod 600 "$env_file"

echo "==> Release prepared: $release"
echo "==> Publishing $environment"
# The promotion script runs Alembic before systemd starts the service. Load
# the target environment first so migrations use the isolated local PostgreSQL
# URL (127.0.0.1), rather than a stale shell value such as host `postgres`.
set -a
# shellcheck disable=SC1090
source "$env_file"
set +a
[ "${HAILIANG_DEPLOY_ENV:-}" = "$environment" ] || {
  echo "HAILIANG_DEPLOY_ENV does not match target environment: $environment" >&2
  exit 2
}
[ "${HAILIANG_RELEASE_VERSION:-}" = "$version" ] || {
  echo "HAILIANG_RELEASE_VERSION does not match release: $version" >&2
  exit 2
}
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
  bash "$release/deploy/bin/promote-release.sh" "$environment" "$version"

echo "==> Verifying service"
health_host="${HAILIANG_BIND_HOST/0.0.0.0/127.0.0.1}"
curl --fail --silent --show-error "http://$health_host:$BACKEND_PORT/health/ready"
echo
curl --fail --silent --show-error "http://${HAILIANG_FRONTEND_BIND_HOST:-$health_host}:$FRONTEND_PORT/" >/dev/null
echo "Deployment succeeded: $environment $version"
