from __future__ import annotations

import json
from typing import Any, Iterator

from hailiang_skills.llm.client import LLMClient, LLMClientError
from hailiang_skills.llm.prompt_registry import get_prompt_spec


def build_recent_messages(context, limit: int = 6) -> list[dict[str, str]]:
    return context.messages[-limit:]


def build_context_snapshot(context) -> dict[str, Any]:
    return {
        "session_id": context.session_id,
        "known_facts": {
            key: record.model_dump() for key, record in context.known_facts.facts.items()
        },
        "candidate_paths": context.candidate_paths[:5],
        "recent_messages": build_recent_messages(context),
        "skill_states": context.skill_states,
        "interaction_state": context.interaction_state,
    }


def safe_complete_json(
    client: LLMClient | None,
    prompt_key: str,
    user_payload: dict[str, Any],
) -> dict[str, Any] | None:
    if client is None or not client.enabled:
        return None

    system_prompt = get_prompt_spec(prompt_key).content
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
    ]
    try:
        return client.complete_json(messages)
    except LLMClientError:
        return None


def safe_complete_text(
    client: LLMClient | None,
    prompt_key: str,
    user_payload: dict[str, Any],
) -> str | None:
    if client is None or not client.enabled:
        return None

    system_prompt = get_prompt_spec(prompt_key).content
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
    ]
    try:
        return client.complete_text(messages)
    except LLMClientError:
        return None


def safe_stream_text(
    client: LLMClient | None,
    prompt_key: str,
    user_payload: dict[str, Any],
) -> Iterator[str]:
    if client is None or not client.enabled:
        return iter(())

    system_prompt = get_prompt_spec(prompt_key).content
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
    ]
    try:
        return client.stream_text(messages)
    except LLMClientError:
        return iter(())
