from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


ACTIVE = "active"
SUBMITTED = "submitted"
SELECTED = "selected"
EXPIRED = "expired"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def interaction_id_for_block(block: dict[str, Any]) -> str | None:
    block_type = str(block.get("type") or "").strip()
    payload = block.get("payload") if isinstance(block.get("payload"), dict) else {}
    if block_type == "fact_form":
        form_id = str(payload.get("form_id") or "").strip()
        return f"fact_form:{form_id or 'default'}"
    if block_type == "path_actions":
        return "path_actions"
    return None


def _candidate_states(message: dict[str, Any]) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        blocks = metadata.get("blocks") if isinstance(metadata.get("blocks"), list) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        interaction_id = interaction_id_for_block(block)
        if not interaction_id:
            continue
        kind = interaction_id.split(":", 1)[0]
        candidates[interaction_id] = {"kind": kind}
    suggestions = message.get("route_suggestions")
    if not isinstance(suggestions, list):
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        suggestions = metadata.get("route_suggestions")
    if isinstance(suggestions, list) and suggestions:
        candidates["route_suggestions"] = {"kind": "route_suggestions"}
    handoff = message.get("team_handoff")
    if not isinstance(handoff, dict):
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        handoff = metadata.get("team_handoff")
    if isinstance(handoff, dict) and isinstance(handoff.get("candidates"), list) and handoff["candidates"]:
        candidates["team_handoff"] = {"kind": "team_handoff"}
    return candidates


def interaction_states(message: dict[str, Any]) -> dict[str, dict[str, Any]]:
    metadata = message.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        message["metadata"] = metadata
    raw = message.get("interaction_states")
    if not isinstance(raw, dict):
        raw = metadata.get("interaction_states")
    states = {str(key): dict(value) for key, value in (raw or {}).items() if isinstance(value, dict)}
    message["interaction_states"] = states
    metadata["interaction_states"] = states
    return states


def ensure_message_interactions(message: dict[str, Any], *, default_status: str = ACTIVE) -> dict[str, dict[str, Any]]:
    states = interaction_states(message)
    for interaction_id, candidate in _candidate_states(message).items():
        current = states.get(interaction_id)
        if not isinstance(current, dict):
            states[interaction_id] = {
                "kind": candidate["kind"],
                "status": default_status,
                "updated_at": utc_now_iso(),
            }
        else:
            current.setdefault("kind", candidate["kind"])
            current.setdefault("status", default_status)
            current.setdefault("updated_at", utc_now_iso())
    return states


def backfill_interactions(messages: list[dict[str, Any]]) -> None:
    latest_assistant_index = max(
        (index for index, message in enumerate(messages) if isinstance(message, dict) and message.get("role") == "assistant" and _candidate_states(message)),
        default=-1,
    )
    has_later_user = any(
        isinstance(message, dict) and message.get("role") == "user"
        for message in messages[latest_assistant_index + 1 :]
    )
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        default_status = ACTIVE if index == latest_assistant_index and not has_later_user else EXPIRED
        ensure_message_interactions(message, default_status=default_status)


def expire_active_interactions(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for interaction_id, state in ensure_message_interactions(message).items():
            if state.get("status") != ACTIVE:
                continue
            state["status"] = EXPIRED
            state["updated_at"] = utc_now_iso()
            changes.append({
                "message_id": str(message.get("message_id") or ""),
                "interaction_id": interaction_id,
            })
    return changes


def update_interaction(
    message: dict[str, Any],
    interaction_id: str,
    *,
    status: str,
    submitted_fact_keys: list[str] | None = None,
    selected_target_skill_id: str | None = None,
) -> dict[str, Any]:
    states = ensure_message_interactions(message)
    state = states.get(interaction_id)
    if not isinstance(state, dict):
        raise KeyError(interaction_id)
    state["status"] = status
    state["updated_at"] = utc_now_iso()
    if submitted_fact_keys is not None:
        state["submitted_fact_keys"] = submitted_fact_keys
    if selected_target_skill_id is not None:
        state["selected_target_skill_id"] = selected_target_skill_id
    return state
