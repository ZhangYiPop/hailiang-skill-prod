from __future__ import annotations

from typing import Any

from hailiang_skills.skill_runtime.models import SessionState


def export_runtime_facts(state: SessionState) -> dict[str, Any]:
    return {
        "active_skill_id": state.active_skill_id,
        "stage": state.stage,
        "global_facts": dict(state.global_facts),
        "skill_facts": {key: dict(value) for key, value in state.skill_facts.items()},
        "stage_facts": {
            skill_id: {stage_id: dict(stage_value) for stage_id, stage_value in skill_value.items()}
            for skill_id, skill_value in state.stage_facts.items()
        },
        "status_flags": dict(state.status_flags),
        "route_history": list(state.route_history),
    }


def map_runtime_facts_to_external_schema(runtime_facts: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    global_facts = runtime_facts.get("global_facts", {})
    if not isinstance(global_facts, dict):
        global_facts = {}
    for runtime_key, external_key in mapping.items():
        if runtime_key in global_facts and str(external_key).strip():
            result[str(external_key).strip()] = global_facts[runtime_key]
    return result


def import_external_facts_patch(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "global_facts_patch": dict(payload.get("global_facts_patch") or {}),
        "skill_facts_patch": dict(payload.get("skill_facts_patch") or {}),
        "status_flags_patch": dict(payload.get("status_flags_patch") or {}),
    }
