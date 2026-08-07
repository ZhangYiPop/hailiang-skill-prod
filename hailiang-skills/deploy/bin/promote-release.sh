#!/usr/bin/env bash
# Usage: promote-release.sh test|prod <already-installed-version>
set -euo pipefail
environment="${1:?environment required}"
version="${2:?version required}"
[ "$environment" = test ] || [ "$environment" = prod ] || { echo "environment must be test or prod" >&2; exit 2; }
release="/opt/hailiang-skills/releases/$version"
[ -d "$release" ] || { echo "release does not exist: $release" >&2; exit 2; }
[ -f "$release/frontend/package.json" ] || { echo "frontend source is missing from release" >&2; exit 2; }

wait_for_url() {
  local url="$1"
  local service_name="$2"
  local attempt
  for attempt in $(seq 1 20); do
    if curl --fail --silent "$url" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "$service_name did not become ready within 20 seconds: $url" >&2
  curl --fail --silent --show-error "$url" >/dev/null
}

if [ "$environment" = prod ]; then
  mkdir -p /var/lib/hailiang-skills/prod/backups
  backup="/var/lib/hailiang-skills/prod/backups/hailiang_skills-$(date +%Y%m%d%H%M%S).dump"
  pg_dump_url="${HAILIANG_PGDUMP_URL:-${HAILIANG_DATABASE_URL:?source /etc/hailiang-skills/prod.env first}}"
  # SQLAlchemy's explicit psycopg dialect is not understood by libpq tools.
  pg_dump_url="${pg_dump_url/postgresql+psycopg:\/\//postgresql:\/\/}"
  pg_dump --dbname "$pg_dump_url" --format=custom --file "$backup"
fi

(cd "$release" && PYTHONPATH=src .venv/bin/alembic upgrade head)
"$release/deploy/bin/build-frontend.sh" "$release"

if [ -L "/opt/hailiang-skills/current-$environment" ]; then
  sudo ln -sfn "$(readlink -f "/opt/hailiang-skills/current-$environment")" "/opt/hailiang-skills/previous-$environment"
fi

sudo ln -sfn "$release" "/opt/hailiang-skills/current-$environment"
if [ "$environment" = prod ]; then
  sudo ln -sfn "$release" /opt/hailiang-skills/current
  if [ -L /opt/hailiang-skills/previous-prod ]; then
    sudo ln -sfn "$(readlink -f /opt/hailiang-skills/previous-prod)" /opt/hailiang-skills/previous
  fi
fi
sudo systemctl restart "hailiang-skills-api@$environment.service"
sudo systemctl restart "hailiang-skills-web@$environment.service"
wait_for_url "http://${HAILIANG_BIND_HOST:?}:${BACKEND_PORT:?}/health/ready" "API"
wait_for_url "http://${HAILIANG_FRONTEND_BIND_HOST:?}:${FRONTEND_PORT:?}/" "frontend"
