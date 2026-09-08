#!/usr/bin/env python3
"""Preflight or migrate filesystem business configuration into the database."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="迁移 runtime_skills/runtime_agents/runtime_agent_teams")
    parser.add_argument("--execute", action="store_true", help="写入不可变修订和发布版本；默认只预检")
    parser.add_argument("--make-current", action="store_true", help="显式将本次导入版本设为当前发布")
    parser.add_argument(
        "--skills-only",
        action="store_true",
        help="只迁移 runtime_skills，不导入 runtime_agents/runtime_agent_teams",
    )
    parser.add_argument("--actor-id", default="migration-operator")
    parser.add_argument("--export-dir", help="执行后将所有当前发布专家团导出为递归 ZIP")
    args = parser.parse_args()
    if args.make_current and not args.execute:
        parser.error("--make-current 必须与 --execute 同时使用")

    # The migration source must be the filesystem. Automatic bootstrap would
    # make a dry run mutate state, so it is always disabled for this command.
    os.environ["HAILIANG_BUSINESS_CONFIG_SOURCE"] = "filesystem"
    os.environ["HAILIANG_WORKBENCH_BOOTSTRAP"] = "false"
    from hailiang_skills.api.main import create_app

    service = create_app().state.workbench_service
    result = (
        service.migrate_runtime_catalog(
            actor_id=args.actor_id,
            make_current=args.make_current,
            skills_only=args.skills_only,
        )
        if args.execute
        else service.preview_runtime_migration(skills_only=args.skills_only)
    )
    if args.export_dir:
        if not args.execute:
            parser.error("--export-dir 必须与 --execute 同时使用")
        output_dir = Path(args.export_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        exports = []
        for team in service.list_objects(object_type="expert_team"):
            release_id = str(team.get("current_release_id") or "")
            if not release_id:
                continue
            package, manifest = service.export_release(release_id, actor_id=args.actor_id)
            target = output_dir / f"{team['object_key']}-{manifest['manifest_hash'][:12]}.zip"
            target.write_bytes(package)
            exports.append({
                "expert_team_id": team["object_key"],
                "release_id": release_id,
                "path": str(target),
                "package_sha256": __import__("hashlib").sha256(package).hexdigest(),
            })
        result["exports"] = exports
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
