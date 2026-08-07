from __future__ import annotations

from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.session_logging import load_session_snapshot
from hailiang_skills.schemas.facts import FactRecord, KnownFacts


def _build_known_facts(raw: dict | None) -> KnownFacts:
    facts = KnownFacts()
    for key, payload in (raw or {}).items():
        facts.facts[key] = FactRecord.model_validate(payload)
    return facts


def load_session_context_from_snapshot(session_id: str) -> SessionContext:
    snapshot = load_session_snapshot(session_id)
    if snapshot is None:
        raise KeyError(session_id)

    context = SessionContext(
        session_id=snapshot.get("session_id", session_id),
        user_id=snapshot.get("user_id", "anonymous"),
        profile_id=snapshot.get("profile_id"),
        profile_name=snapshot.get("profile_name"),
        title=snapshot.get("title"),
        messages=snapshot.get("messages", []),
        skill_states=snapshot.get("skill_states", {}),
        candidate_paths=snapshot.get("candidate_paths", []),
        interaction_state=snapshot.get("interaction_state", {}),
        risk_signals=snapshot.get("risk_signals", []),
        event_trace=[],
        asset_version=snapshot.get("asset_version", "dev"),
        session_meta=snapshot.get("session_meta", {}),
        last_fact_changes=snapshot.get("last_fact_changes", []),
    )
    shared_facts = _build_known_facts(snapshot.get("shared_facts"))
    profile_facts = _build_known_facts(snapshot.get("profile_facts"))
    session_facts = _build_known_facts(snapshot.get("session_facts"))
    if not shared_facts.facts:
        shared_facts = _build_known_facts(snapshot.get("user_facts"))
    if not shared_facts.facts and not profile_facts.facts and not session_facts.facts:
        # Backward compatibility for old snapshots.
        shared_facts = _build_known_facts(snapshot.get("facts"))
    context.load_effective_facts(
        shared_facts=shared_facts,
        profile_facts=profile_facts,
        session_facts=session_facts,
    )
    return context
