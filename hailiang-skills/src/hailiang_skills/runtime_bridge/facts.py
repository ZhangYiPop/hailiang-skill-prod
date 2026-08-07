from __future__ import annotations

from typing import Any

from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID


RUNTIME_STATE_KEY = "skill_runtime"


def fact_values(context) -> dict[str, Any]:
    return {key: record.value for key, record in context.known_facts.facts.items()}


def sync_context_to_runtime_state(context, state) -> None:
    values = fact_values(context)
    # Runtime facts are a projection of the three persistent fact scopes.
    # Rebuild instead of updating so an exited skill cannot leak stale values.
    state.global_facts = {key: value for key, value in values.items() if value not in (None, "", [], {})}
    state.session_id = context.session_id


def sync_runtime_state_to_context(context, state, *, source_skill: str = CAREER_PLAN_SKILL_ID) -> None:
    for key, value in dict(state.global_facts).items():
        if value in (None, "", [], {}):
            continue
        current = context.known_facts.get_value(key)
        if current == value:
            continue
        context.update_fact(
            key,
            value,
            source_skill=source_skill,
            confidence=0.85,
            source_type="skill_runtime",
            source_id=str(state.active_skill_id or source_skill),
        )


def runtime_state_payload(state) -> dict[str, Any]:
    return {
        "session_id": state.session_id,
        "stage": state.stage,
        "collected_info": dict(state.collected_info),
        "active_skill_id": state.active_skill_id,
        "global_facts": dict(state.global_facts),
        "skill_facts": {
            str(key): dict(value)
            for key, value in dict(state.skill_facts).items()
            if isinstance(value, dict)
        },
        "stage_facts": {
            str(skill_id): {
                str(stage_id): dict(stage_value)
                for stage_id, stage_value in dict(skill_value).items()
                if isinstance(stage_value, dict)
            }
            for skill_id, skill_value in dict(state.stage_facts).items()
            if isinstance(skill_value, dict)
        },
        "status_flags": dict(state.status_flags),
        "route_history": list(state.route_history),
    }
