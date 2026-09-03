"""Persisted, client-safe conversation state helpers."""

from __future__ import annotations

from typing import Any

from hailiang_skills.core.logging import utc_now_iso


def get_conversation_state(context) -> dict[str, Any]:
    state = context.session_meta.setdefault("conversation_state", {})
    if not isinstance(state, dict):
        state = {}
        context.session_meta["conversation_state"] = state
    security = state.setdefault("security", {"latest": None, "input": None, "output": None})
    if not isinstance(security, dict):
        state["security"] = {"latest": None, "input": None, "output": None}
    return state


def record_security_result(context, *, stage: str, result, case_id: str | None = None) -> dict[str, Any]:
    """Store the moderation result in the session snapshot and return it.

    ``to_public_dict`` is intentionally retained verbatim because the BFF
    contract requires the complete existing public moderation payload.
    """
    payload = dict(result.to_public_dict())
    payload.update(
        {
            "stage": stage,
            "case_id": case_id,
            "checked_at": utc_now_iso(),
            "status": "blocked" if result.blocked else ("degraded" if result.mode == "local_fallback" else "passed"),
        }
    )
    security = get_conversation_state(context)["security"]
    security[stage] = payload
    security["latest"] = payload
    callback = (context.session_meta or {}).get("security_callback")
    if callable(callback):
        callback(
            {
                "status": payload["status"],
                "stage": stage,
                "blocked": payload["status"] == "blocked",
                "message": "该内容被内容安全策略拦截，请调整后重新输入。"
                if payload["status"] == "blocked"
                else "",
                "category": (payload.get("categories") or ["content_policy"])[0]
                if payload["status"] == "blocked" else "",
                "provider": payload.get("provider", "") if payload["status"] == "blocked" else "",
            }
        )
    return payload
