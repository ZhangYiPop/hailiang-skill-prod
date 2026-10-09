from __future__ import annotations

from typing import Any

from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID
from hailiang_skills.runtime_bridge.question_progress import question_ledger_projection


RUNTIME_STATE_KEY = "skill_runtime"


def fact_values(context) -> dict[str, Any]:
    return {key: record.value for key, record in context.known_facts.facts.items()}


def user_fact_evidence(value: Any, messages: list[dict[str, Any]], evidence: str = "") -> dict[str, Any] | None:
    parts = value if isinstance(value, list) else [value]
    return next((message for message in reversed(messages)
        if message.get("role") == "user" and (
            evidence.strip() in str(message.get("content") or "") if evidence.strip() else all(
                str(part).strip() and str(part).strip() in str(message.get("content") or "") for part in parts
            )
        )), None)


def sync_context_to_runtime_state(context, state) -> None:
    values = fact_values(context)
    # Runtime facts are a projection of the three persistent fact scopes.
    # Rebuild instead of updating so an exited skill cannot leak stale values.
    state.global_facts = {key: value for key, value in values.items() if value not in (None, "", [], {})}
    state.session_id = context.session_id
    if int(state.status_flags.get("context_contract_version") or 2) >= 3:
        for skill_id, progress in (state.status_flags.get("runtime_skill_progress") or {}).items():
            if isinstance(progress, dict):
                if "diagnostics" not in progress:
                    progress["diagnostics"] = {key: progress[key] for key in (
                        "stage_label", "pending_topics", "resolved_topics", "next_action"
                    ) if key in progress}
                progress["confirmed_facts"] = dict(state.global_facts)
                progress["confirmed_fact_keys"] = sorted(state.global_facts)
                projection = question_ledger_projection(state, skill_id)
                progress["pending_topics"] = [item.get("text", "") for item in projection["unresolved"]]
                progress["resolved_topics"] = [item.get("question", "") for item in projection["answered"]]
                progress["next_action"] = ""
                progress["stage_label"] = state.stage


def sync_runtime_state_to_context(context, state, *, source_skill: str = CAREER_PLAN_SKILL_ID) -> None:
    for key, value in dict(state.global_facts).items():
        if value in (None, "", [], {}):
            continue
        current = context.known_facts.get_value(key)
        if current == value:
            continue
        source_message = None
        if int(state.status_flags.get("context_contract_version") or 2) >= 3:
            # Model-proposed collection values need a user source before
            # becoming durable facts. Derived script data stays in runtime.
            evidence = str((state.status_flags.get("_user_fact_evidence") or {}).get(key) or "")
            source_message = user_fact_evidence(value, context.messages, evidence)
            if source_message is None:
                continue
            if current is not None:
                record = context.known_facts.facts.get(key)
                current_source = user_fact_evidence(current, context.messages, str(getattr(record, "evidence_summary", "") or ""))
                if current_source and context.messages.index(current_source) > context.messages.index(source_message):
                    continue
        context.update_fact(
            key,
            value,
            source_skill=source_skill,
            confidence=0.85,
            source_type="user_message" if source_message else "skill_runtime",
            source_id=source_message.get("message_id") if source_message else str(state.active_skill_id or source_skill),
            source_turn_id=(source_message.get("metadata") or {}).get("turn_id") if source_message else None,
            evidence_summary=source_message.get("content") if source_message else None,
        )
    state.status_flags.pop("_user_fact_evidence", None)


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
