"""Branch-safe durable candidate evidence for child profile conversations."""

from __future__ import annotations

from typing import Any

from hailiang_skills.core.logging import make_event
from hailiang_skills.schemas.provenance import Provenance


def candidate_archive(context) -> list[dict[str, Any]]:
    """Compact profile-only candidate evidence, safe to inject into prompts."""
    if getattr(context, "context_scope", "unbound") != "profile":
        return []
    records = getattr(getattr(context, "profile_facts", None), "facts", {}) or {}
    items: list[dict[str, Any]] = []
    for key, record in records.items():
        if getattr(record, "status", "confirmed") != "candidate":
            continue
        # Hydration normally replaces profile_facts on every switch. This
        # guard also prevents a stale in-memory profile object from leaking a
        # candidate into another child's prompt between switch and hydration.
        source_id = str(getattr(record, "source_id", "") or "")
        if source_id and not source_id.startswith(f"{context.profile_id}:"):
            continue
        items.append({
            "key": str(key),
            "value": record.value,
            "confidence": record.confidence,
            "observed_at": record.observed_at,
            "source_turn_id": record.source_turn_id,
            "evidence_summary": record.evidence_summary,
            "history": list(record.observation_history or [])[-3:],
        })
    return items


def archive_candidate(
    context,
    *,
    key: str,
    value: Any,
    source_skill: str,
    source_turn_id: str | None,
    evidence_summary: str,
    confidence: float,
) -> bool:
    """Persist an inferred fact only on the current child's profile archive."""
    if getattr(context, "context_scope", "unbound") != "profile" or not getattr(context, "profile_id", None):
        return False
    context.update_fact(
        key,
        value,
        source_skill=source_skill,
        source_type="conversation_inference",
        source_id=f"{context.profile_id}:{getattr(context, 'session_id', '')}",
        source_label="对话上下文推断",
        source_turn_id=source_turn_id,
        scope="profile",
        confidence=max(0.0, min(1.0, float(confidence))),
        status="candidate",
        evidence_summary=evidence_summary,
        provenance=Provenance(
            source_type="conversation_inference",
            source_id=f"{context.profile_id}:{getattr(context, 'session_id', '')}",
            turn_id=source_turn_id,
        ),
    )
    trace = getattr(context, "event_trace", None)
    if isinstance(trace, list):
        trace.append(make_event("profile_candidate_archived", {
            "profile_id": context.profile_id,
            "fact_key": key,
            "confidence": confidence,
            "source_turn_id": source_turn_id,
        }))
    return True
