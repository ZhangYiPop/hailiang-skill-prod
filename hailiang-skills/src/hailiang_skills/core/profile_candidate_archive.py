"""Profile-scoped, durable candidate archive facade.

Candidates deliberately no longer live in ``profile_facts``: that projection
contains only confirmed business facts.  The small in-memory fallback keeps
local/unit-test usage functional while production injects the PostgreSQL repo.
"""

from __future__ import annotations

from typing import Any

from hailiang_skills.core.logging import make_event
from hailiang_skills.storage.repositories.profile_memory_repo import InMemoryProfileMemoryRepository


_fallback_repository = InMemoryProfileMemoryRepository()


def candidate_archive(
    context,
    *,
    repository=None,
    query_text: str = "",
    skill_id: str = "",
    domain: str = "",
    limit: int = 12,
    token_budget: int = 24_000,
) -> list[dict[str, Any]]:
    """Retrieve only current-child evidence; legacy JSON candidates stay inert."""
    if getattr(context, "context_scope", "unbound") != "profile" or not getattr(context, "profile_id", None):
        return []
    repo = repository or _fallback_repository
    return repo.retrieve(
        str(context.profile_id), query_text,
        skill_id=skill_id, domain=domain, limit=limit, token_budget=token_budget,
    )


def archive_candidate(
    context,
    *,
    key: str,
    value: Any,
    source_skill: str,
    source_turn_id: str | None,
    evidence_summary: str,
    confidence: float,
    repository=None,
    domain: str = "",
) -> bool:
    """Persist inferred evidence without promoting it to an effective fact."""
    if getattr(context, "context_scope", "unbound") != "profile" or not getattr(context, "profile_id", None):
        return False
    repo = repository or _fallback_repository
    recorded = repo.record_candidate(
        profile_id=str(context.profile_id),
        fact_key=str(key),
        value=value,
        skill_id=source_skill,
        domain=domain or source_skill,
        source_session_id=str(getattr(context, "session_id", "") or "") or None,
        source_turn_id=source_turn_id,
        evidence_summary=str(evidence_summary or "")[:500],
        confidence=max(0.0, min(1.0, float(confidence))),
    )
    trace = getattr(context, "event_trace", None)
    if isinstance(trace, list):
        trace.append(make_event("profile_memory_recorded", {
            "profile_id": context.profile_id,
            "memory_id": recorded.get("memory_id"),
            "fact_key": key,
            "confidence": confidence,
            "source_turn_id": source_turn_id,
        }))
    return True
