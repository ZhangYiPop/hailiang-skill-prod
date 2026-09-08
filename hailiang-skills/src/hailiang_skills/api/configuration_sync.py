from __future__ import annotations

from typing import Any

from hailiang_skills.core.logging import make_event
from hailiang_skills.core.message_interactions import expire_active_interactions


def configuration_identity(snapshot: dict[str, Any] | None) -> tuple[str, str]:
    if not isinstance(snapshot, dict):
        return "", ""
    return str(snapshot.get("deployment_id") or ""), str(snapshot.get("package_hash") or "")


def synchronize_configuration(context, snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Switch a persisted session at the next operation, preserving durable history and Facts."""
    previous = context.session_meta.get("configuration_snapshot")
    stale_unversioned_team = (
        previous is None
        and snapshot is None
        and bool(context.session_meta.get("expert_team_id") or context.session_meta.get("active_expert_id"))
    )
    if configuration_identity(previous) == configuration_identity(snapshot) and not stale_unversioned_team:
        return None
    old_identity = configuration_identity(previous)
    new_identity = configuration_identity(snapshot)
    if snapshot is None:
        context.session_meta.pop("configuration_snapshot", None)
    else:
        context.session_meta["configuration_snapshot"] = snapshot

    expire_active_interactions(context.messages)
    context.candidate_paths = []
    context.skill_states = {}
    context.interaction_state = {}
    for key in ("pending_team_handoff", "expert_requested_skill_id", "route_suggestion", "pending_form"):
        context.session_meta.pop(key, None)

    team_id, coordinator_id, member_ids = _snapshot_team(snapshot)
    prior_expert = str(
        context.session_meta.get("active_expert_id")
        or context.session_meta.get("expert_id")
        or context.session_agent_selection().get("expert_id")
        or ""
    )
    selected_expert = prior_expert if prior_expert in member_ids else coordinator_id
    context.set_session_agent_selection(
        expert_team_id=team_id or None,
        expert_id=selected_expert or None,
        selection_source="configuration_sync" if team_id else "deployment_deactivated",
    )
    context.apply_session_agent_selection()
    context.interaction_state["active_skill"] = "career_plan_entity" if team_id else "general_chat"
    notice = {
        "code": "CONFIGURATION_UPDATED",
        "previous_deployment_id": old_identity[0] or None,
        "previous_package_hash": old_identity[1] or None,
        "deployment_id": new_identity[0] or None,
        "package_hash": new_identity[1] or None,
        "expert_team_id": team_id or None,
        "active_expert_id": selected_expert or None,
    }
    context.session_meta["configuration_change"] = notice
    context.event_trace.append(make_event("conversation_configuration_updated", notice))
    return notice


def _snapshot_team(snapshot: dict[str, Any] | None) -> tuple[str, str, set[str]]:
    entries = snapshot.get("entries") if isinstance(snapshot, dict) and isinstance(snapshot.get("entries"), list) else []
    team = next((item for item in entries if isinstance(item, dict) and item.get("object_type") == "expert_team"), None)
    if not isinstance(team, dict):
        return "", "", set()
    locks = team.get("dependency_locks") if isinstance(team.get("dependency_locks"), list) else []
    member_ids = {str(item.get("object_key") or "") for item in locks if isinstance(item, dict) and item.get("object_key")}
    payload = team.get("payload") if isinstance(team.get("payload"), dict) else {}
    coordinator_object_id = str(payload.get("coordinator_expert_id") or "")
    coordinator_id = next(
        (str(item.get("object_key") or "") for item in locks if str(item.get("object_id") or "") == coordinator_object_id),
        coordinator_object_id if coordinator_object_id in member_ids else "",
    )
    return str(team.get("object_key") or ""), coordinator_id, member_ids
