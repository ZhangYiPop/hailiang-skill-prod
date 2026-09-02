#!/usr/bin/env bash
set -euo pipefail
previous="/opt/hailiang-skills/previous-prod"
[ -L "$previous" ] || { echo "no previous verified release symlink" >&2; exit 2; }
sudo ln -sfn "$(readlink -f "$previous")" /opt/hailiang-skills/current
sudo ln -sfn "$(readlink -f "$previous")" /opt/hailiang-skills/current-prod
sudo systemctl restart hailiang-skills-api@prod.service
if systemctl list-unit-files "hailiang-skills-workbench@.service" --no-legend 2>/dev/null | grep -q hailiang-skills-workbench; then
  sudo systemctl restart hailiang-skills-workbench@prod.service
fi
sudo systemctl restart hailiang-skills-web@prod.service
