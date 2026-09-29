from __future__ import annotations

from typing import Any

from hailiang_skills.core.facts_config import get_enabled_fact_keys, get_fact_meta
from hailiang_skills.schemas.facts import normalize_fact_value


def _user_message_index(context) -> dict[str, dict[str, Any]]:
    result = {}
    for message in getattr(context, "messages", []) or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if metadata.get("hidden") or metadata.get("message_type") in {
            "team_handoff_confirmation", "skill_transition_command",
        }:
            continue
        message_id = str(message.get("message_id") or metadata.get("message_id") or "").strip()
        if message_id:
            result[message_id] = message
    return result


def _valid_evidence(update: dict[str, Any], messages: dict[str, dict[str, Any]]):
    message_id = str(update.get("source_message_id") or "").strip()
    evidence = str(update.get("evidence") or "").strip()
    message = messages.get(message_id)
    if not message:
        return None, "source_message_not_in_branch"
    if not evidence or evidence not in str(message.get("content") or ""):
        return None, "evidence_not_found_in_source"
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    return (message_id, evidence, str(metadata.get("turn_id") or message_id)), ""


def apply_model_context_updates(context, updates: Any, *, source: str) -> dict[str, Any]:
    """Validate model-proposed continuity facts and persist them only in-session."""
    messages = _user_message_index(context)
    summary = {"accepted_fact_keys": [], "rejected": [], "identity": "unchanged"}
    payload = updates if isinstance(updates, dict) else {}
    if updates is not None and not isinstance(updates, dict):
        summary["rejected"].append({"kind": "result", "reason": "invalid_context_updates_format"})
    identity = payload.get("identity")
    if isinstance(identity, dict):
        provenance, error = _valid_evidence(identity, messages)
        role = str(identity.get("role") or "").strip().lower()
        try:
            confidence = float(identity.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0
        if error or role not in {"parent", "student", "unknown"} or not 0.5 <= confidence <= 1:
            summary["rejected"].append({"kind": "identity", "reason": error or "invalid_identity"})
        elif role != "unknown":
            message_id, evidence, turn_id = provenance
            previous = context.session_meta.get("conversation_identity")
            previous_id = str(previous.get("source_message_id") or "") if isinstance(previous, dict) else ""
            # Only a newer message may correct a previously established role.
            ordered_ids = list(messages)
            if not previous_id or previous_id not in ordered_ids or ordered_ids.index(message_id) >= ordered_ids.index(previous_id):
                context.session_meta["conversation_identity"] = {
                    "role": role, "source_message_id": message_id,
                    "source_turn_id": turn_id, "evidence": evidence,
                    "confidence": confidence,
                }
                summary["identity"] = "updated"
    elif identity is not None:
        summary["rejected"].append({"kind": "identity", "reason": "invalid_identity_format"})

    facts = payload.get("facts")
    if not isinstance(facts, list):
        if "facts" in payload:
            summary["rejected"].append({"kind": "facts", "reason": "invalid_facts_format"})
        facts = []
    enabled = set(get_enabled_fact_keys())
    for item in facts:
        if not isinstance(item, dict):
            summary["rejected"].append({"kind": "fact", "reason": "invalid_update"})
            continue
        key = str(item.get("key") or "").strip()
        provenance, error = _valid_evidence(item, messages)
        if error:
            summary["rejected"].append({"key": key or None, "reason": error})
            continue
        if key not in enabled:
            summary["rejected"].append({"key": key or None, "reason": "unknown_or_disabled_fact"})
            continue
        try:
            confidence = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0
        value = normalize_fact_value(key, item.get("value"))
        meta = get_fact_meta(key)
        value_type = meta.get("value_type")
        valid_type = {
            "string": lambda v: isinstance(v, str) and bool(v.strip()),
            "string_list": lambda v: isinstance(v, list) and bool(v),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
        }.get(value_type, lambda _v: False)(value)
        allowed = meta.get("allowed_values") or []
        check_values = value if isinstance(value, list) else [value]
        if not valid_type or (allowed and any(candidate not in allowed for candidate in check_values)):
            summary["rejected"].append({"key": key, "reason": "invalid_fact_value"})
            continue
        if not 0.5 <= confidence <= 1:
            summary["rejected"].append({"key": key, "reason": "invalid_confidence"})
            continue
        message_id, evidence, turn_id = provenance
        previous = context.session_facts.facts.get(key)
        previous_id = str(previous.source_id or "") if previous else ""
        ordered_ids = list(messages)
        if previous_id in ordered_ids and ordered_ids.index(message_id) < ordered_ids.index(previous_id):
            summary["rejected"].append({"key": key, "reason": "older_than_current_session_value"})
            continue
        context.update_fact(
            key, value, source_skill=source, confidence=confidence,
            source_type="user_input", source_id=message_id, source_turn_id=turn_id,
            scope="session", evidence_summary=evidence[:240],
        )
        summary["accepted_fact_keys"].append(key)

    if not summary["accepted_fact_keys"] and summary["identity"] == "unchanged":
        status = "missing_result" if updates is None else ("rejected" if summary["rejected"] else "no_updates")
    else:
        status = "applied"
    from hailiang_skills.core.logging import make_event
    event_trace = getattr(context, "event_trace", None)
    if isinstance(event_trace, list):
        event_trace.append(make_event("conversation_fact_extraction", {
            "source": source, "status": status,
            "accepted_fact_keys": summary["accepted_fact_keys"],
            "identity_status": summary["identity"], "rejected": summary["rejected"][:20],
        }))
    return summary
