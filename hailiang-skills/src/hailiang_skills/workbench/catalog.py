from __future__ import annotations

from typing import Any

from sqlalchemy import select

from hailiang_skills.storage.database import (
    WorkbenchAssetRow,
    WorkbenchObjectRow,
    WorkbenchReleaseRow,
    WorkbenchRevisionRow,
)


def load_current_release_entries(session_factory) -> list[dict[str, Any]]:
    """Read the immutable entries selected by each object's publication pointer."""
    if session_factory is None:
        return []
    with session_factory() as db:
        rows = db.execute(
            select(WorkbenchObjectRow, WorkbenchReleaseRow, WorkbenchRevisionRow)
            .join(WorkbenchReleaseRow, WorkbenchReleaseRow.release_id == WorkbenchObjectRow.current_release_id)
            .join(WorkbenchRevisionRow, WorkbenchRevisionRow.revision_id == WorkbenchReleaseRow.revision_id)
            .where(
                WorkbenchObjectRow.archived.is_(False),
                WorkbenchReleaseRow.archived.is_(False),
            )
        ).all()
        entries: list[dict[str, Any]] = []
        for obj, release, revision in rows:
            assets = list(db.scalars(
                select(WorkbenchAssetRow)
                .where(WorkbenchAssetRow.revision_id == revision.revision_id)
                .order_by(WorkbenchAssetRow.relative_path)
            ))
            entries.append({
                "object_id": obj.object_id,
                "object_type": obj.object_type,
                "object_key": obj.object_key,
                "name": obj.name,
                "release_id": release.release_id,
                "release_no": release.release_no,
                "payload": revision.payload,
                "dependency_locks": release.dependency_locks,
                "content_hash": revision.content_hash,
                "files": [
                    {
                        "relative_path": asset.relative_path,
                        "media_type": asset.media_type,
                        "size_bytes": asset.size_bytes,
                        "content_hash": asset.content_hash,
                        "content_base64": __import__("base64").b64encode(bytes(asset.content)).decode("ascii"),
                    }
                    for asset in assets
                ],
            })
        return entries


def build_runtime_registries(entries: list[dict[str, Any]], *, enabled_by_id: dict[str, bool] | None = None):
    """Build Skills, experts and teams from database rows without filesystem fallbacks."""
    from hailiang_skills.runtime_bridge.expert_bundle import ExpertDefinition, ExpertRegistry, LockedSkill
    from hailiang_skills.runtime_bridge.expert_team_bundle import (
        ExpertTeamDefinition,
        ExpertTeamMember,
        ExpertTeamRegistry,
    )
    from hailiang_skills.skill_runtime.skill_registry import SkillRegistry
    from hailiang_skills.workbench.runtime_overlay import skill_bundle_from_entry

    by_type = {
        kind: [entry for entry in entries if entry.get("object_type") == kind]
        for kind in ("skill", "expert", "expert_team")
    }
    skills = SkillRegistry(enabled_by_id=dict(enabled_by_id or {}))
    for entry in by_type["skill"]:
        bundle = skill_bundle_from_entry(entry)
        skills.bundles[str(entry["object_key"])] = bundle

    experts = ExpertRegistry(definitions={})
    for entry in by_type["expert"]:
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
        locks = entry.get("dependency_locks") if isinstance(entry.get("dependency_locks"), list) else []
        definition = ExpertDefinition(
            agent_id=str(entry["object_key"]),
            name=str(entry.get("name") or entry["object_key"]),
            rules_markdown=str(payload.get("rules_markdown") or ""),
            skills=tuple(LockedSkill(str(lock["object_key"]), f"v{int(lock.get('release_no') or 0)}") for lock in locks),
            brief=str(payload.get("brief") or ""),
            max_iters=int(budget.get("max_iters") or 4),
            max_skill_calls=int(budget.get("max_skill_calls") or 3),
            capabilities=tuple(payload.get("capabilities") or (
                "execute_skill", "request_declared_form", "read_effective_facts",
            )),
        )
        experts.definitions[definition.agent_id] = definition

    teams = ExpertTeamRegistry(definitions={})
    for entry in by_type["expert_team"]:
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        locks = entry.get("dependency_locks") if isinstance(entry.get("dependency_locks"), list) else []
        member_payloads = payload.get("members") if isinstance(payload.get("members"), list) else []
        by_key = {str(item.get("expert_id") or ""): item for item in member_payloads if isinstance(item, dict)}
        coordinator_object_id = str(payload.get("coordinator_expert_id") or "")
        coordinator = next(
            (str(lock.get("object_key") or "") for lock in locks if str(lock.get("object_id") or "") == coordinator_object_id),
            coordinator_object_id,
        )
        members = tuple(
            ExpertTeamMember(
                expert_id=str(lock.get("object_key") or ""),
                mention_name=str((by_key.get(str(lock.get("object_key") or "")) or {}).get("mention_name") or lock.get("object_key") or ""),
                routing_brief=str((by_key.get(str(lock.get("object_key") or "")) or {}).get("routing_brief") or ""),
            )
            for lock in locks
            if str(lock.get("object_key") or "")
        )
        definition = ExpertTeamDefinition(
            team_id=str(entry["object_key"]),
            name=str(entry.get("name") or entry["object_key"]),
            rules_markdown=str(payload.get("rules_markdown") or ""),
            coordinator_expert_id=coordinator,
            members=members,
            brief=str(payload.get("brief") or ""),
        )
        teams.definitions[definition.team_id] = definition
    return skills, experts, teams
