#!/usr/bin/env bash
set -euo pipefail
exec sudo systemctl start hailiang-skills-api@prod.service
