from __future__ import annotations

from typing import Any

from hailiang_skills.skill_runtime.models import SessionState, SkillBundle
from hailiang_skills.skill_runtime.skill_contract import get_stage_contract

SATISFACTION_PENDING = "pending"
SATISFACTION_CONFIRMED = "confirmed"
SATISFACTION_REJECTED = "rejected"


def ensure_runtime_state(state: SessionState, bundle: SkillBundle) -> None:
    active_skill_id = state.active_skill_id or bundle.contract.skill_id or bundle.root_name
    state.active_skill_id = active_skill_id
    state.skill_facts.setdefault(active_skill_id, {})
    state.stage_facts.setdefault(active_skill_id, {})
    state.stage_facts[active_skill_id].setdefault(state.stage, {})
    defaults = default_status_flags()
    for key, value in defaults.items():
        state.status_flags.setdefault(key, value)


def default_status_flags() -> dict[str, Any]:
    return {
        "collection_complete": False,
        "conclusion_presented": False,
        "conclusion_confirmed": False,
        "user_satisfied": SATISFACTION_PENDING,
        "needs_revise_conclusion": False,
        "pending_route_scene": "",
        "awaiting_school_stage_for_multi_path": False,
        "pending_multi_path_scene": "",
        "interrupted_skill_id": "",
        "resume_to_skill_id": "",
    }


def merge_status_track_updates(bundle: SkillBundle, state: SessionState, response: dict[str, Any]) -> None:
    ensure_runtime_state(state, bundle)
    active_skill_id = state.active_skill_id
    if response.get("stage"):
        state.stage = str(response.get("stage") or state.stage)
    if isinstance(response.get("collected_info"), dict):
        state.collected_info.update(response["collected_info"])
    if isinstance(response.get("global_facts_patch"), dict):
        state.global_facts.update(response["global_facts_patch"])
    if isinstance(response.get("skill_facts_patch"), dict):
        state.skill_facts.setdefault(active_skill_id, {}).update(response["skill_facts_patch"])
    if isinstance(response.get("stage_facts_patch"), dict):
        state.stage_facts.setdefault(active_skill_id, {}).setdefault(state.stage, {}).update(response["stage_facts_patch"])
    if isinstance(response.get("status_flags_patch"), dict):
        state.status_flags.update(response["status_flags_patch"])
    route_signal = response.get("route_signal", {})
    if isinstance(route_signal, dict):
        scene = str(route_signal.get("scene") or "").strip()
        if scene:
            state.status_flags["pending_route_scene"] = scene
    _refresh_status_flags(bundle, state)


def snapshot_for_skill(bundle: SkillBundle, state: SessionState) -> dict[str, Any]:
    ensure_runtime_state(state, bundle)
    active_skill_id = state.active_skill_id
    return {
        "active_skill_id": active_skill_id,
        "global_facts": dict(state.global_facts),
        "skill_facts_for_current_skill": dict(state.skill_facts.get(active_skill_id, {})),
        "stage_facts_for_current_skill": dict(state.stage_facts.get(active_skill_id, {}).get(state.stage, {})),
        "status_flags": dict(state.status_flags),
    }


def mark_route_interruption(state: SessionState, *, target_skill_id: str) -> None:
    interrupted_skill_id = state.active_skill_id
    if not interrupted_skill_id or interrupted_skill_id == target_skill_id:
        return
    state.status_flags["interrupted_skill_id"] = interrupted_skill_id
    state.status_flags["resume_to_skill_id"] = interrupted_skill_id
    if state.status_flags.get("user_satisfied") == SATISFACTION_PENDING:
        state.status_flags["conclusion_confirmed"] = False


def _refresh_status_flags(bundle: SkillBundle, state: SessionState) -> None:
    active_skill_id = state.active_skill_id or bundle.contract.skill_id or bundle.root_name
    current_skill_facts = state.skill_facts.get(active_skill_id, {})
    current_stage_facts = state.stage_facts.get(active_skill_id, {}).get(state.stage, {})
    stage_contract = get_stage_contract(bundle.contract, state.stage)
    if bundle.contract.facts_schema.global_keys:
        required = set(bundle.contract.facts_schema.global_keys)
        state.status_flags["collection_complete"] = required.issubset(set(state.global_facts))
    elif state.collected_info:
        state.status_flags["collection_complete"] = True
    if current_skill_facts.get("conclusion_summary") or current_stage_facts.get("conclusion_summary"):
        state.status_flags["conclusion_presented"] = True
    if stage_contract and stage_contract.kind in {"confirmation", "summary", "analysis"}:
        if state.status_flags.get("user_satisfied") == SATISFACTION_CONFIRMED:
            state.status_flags["conclusion_confirmed"] = True
