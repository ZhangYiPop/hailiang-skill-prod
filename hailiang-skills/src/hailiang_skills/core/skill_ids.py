"""Canonical user-facing Skill identifiers.

``main_planner`` was the historical identifier for the career-planning
consultant.  Keep the legacy spelling only at storage/compatibility
boundaries; new requests, SSE payloads and UI choices use
``career_plan_entity``.
"""

from __future__ import annotations

CAREER_PLAN_SKILL_ID = "career_plan_entity"
LEGACY_MAIN_PLANNER_SKILL_ID = "main_planner"
GENERAL_CHAT_SKILL_ID = "general_chat"
# This is an execution-source marker, not a registered runtime Skill. It
# distinguishes an Agent's role-bounded answer from the legacy general-chat
# fallback in state, traces, and user-facing session metadata.
EXPERT_DIRECT_EXECUTION_ID = "expert_direct"


def canonical_skill_id(value: object, *, default: str = "") -> str:
    normalized = str(value or "").strip()
    if normalized == LEGACY_MAIN_PLANNER_SKILL_ID:
        return CAREER_PLAN_SKILL_ID
    return normalized or default


def is_career_plan_skill(value: object) -> bool:
    return canonical_skill_id(value) == CAREER_PLAN_SKILL_ID
