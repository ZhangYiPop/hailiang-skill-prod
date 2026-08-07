#!/usr/bin/env bash
set -euo pipefail
previous="/opt/hailiang-skills/previous-prod"
[ -L "$previous" ] || { echo "no previous verified release symlink" >&2; exit 2; }
sudo ln -sfn "$(readlink -f "$previous")" /opt/hailiang-skills/current
sudo ln -sfn "$(readlink -f "$previous")" /opt/hailiang-skills/current-prod
sudo systemctl restart hailiang-skills-api@prod.service
sudo systemctl restart hailiang-skills-web@prod.service
