"""Session opening used by the BFF's single streaming entry point."""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.conversation_state import get_conversation_state
from hailiang_skills.core.fact_service import FactService
from hailiang_skills.core.facts_config import get_enabled_fact_keys
from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID, GENERAL_CHAT_SKILL_ID, LEGACY_MAIN_PLANNER_SKILL_ID
from hailiang_skills.runtime_bridge.facts import RUNTIME_STATE_KEY


GENERAL_CHAT_ID = "general_chat"


class ContextData(BaseModel):
    """Identity required by the chat stream, plus optional BFF context.

    BFFs may add context fields without requiring a simultaneous algorithm
    service release.  Only the three identifiers below are part of the
    mandatory identity contract.
    """

    model_config = ConfigDict(extra="allow")
    student_name: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    school_year: str | None = Field(default=None, min_length=1)
    grade: str | None = Field(default=None, min_length=1)
    facts: dict[str, object] = Field(default_factory=dict)


def _seed_profile(fact_service: FactService, data: ContextData) -> str:
    """Create/synchronise the profile only while opening a new session."""
    try:
        profile = fact_service.profile_repo.update_profile(
            data.user_id, data.profile_id, name=data.student_name
        )
    except KeyError:
        try:
            profile = fact_service.profile_repo.create_profile(
                data.user_id,
                profile_id=data.profile_id,
                name=data.student_name,
                shared_facts_initialized=False,
            )
        except IntegrityError as exc:
            try:
                profile = fact_service.profile_repo.update_profile(
                    data.user_id, data.profile_id, name=data.student_name
                )
            except KeyError as conflict:
                raise HTTPException(status_code=409, detail="PROFILE_ID_CONFLICT") from conflict
            if profile.get("user_id") not in (None, data.user_id):
                raise HTTPException(status_code=409, detail="PROFILE_ID_CONFLICT") from exc

    profile_facts = fact_service.get_profile_facts(data.user_id, data.profile_id)
    if data.school_year is not None and data.grade is not None:
        school_facts = [{"school_year": data.school_year, "grade": data.grade}]
        profile_facts.set_fact(
            "profile_school_facts",
            school_facts,
            source_skill="project_backend",
            source_type="project_backend",
            source_label="context_data",
            scope="profile",
        )
    fact_service.profile_repo.save_profile_facts(data.user_id, data.profile_id, profile_facts)
    return str(profile.get("name") or data.student_name)


def _context_fact_values(data: ContextData) -> dict[str, object]:
    """Return configured Facts supplied by a trusted forwarding service.

    ``facts`` is the preferred extensible envelope. Registered Fact keys may
    also be sent directly in ``context_data`` for BFFs that cannot yet nest
    them. Unknown metadata is deliberately ignored.
    """
    enabled_keys = set(get_enabled_fact_keys())
    values = {
        str(key): value
        for key, value in data.facts.items()
        if str(key) in enabled_keys
    }
    for key, value in (data.model_extra or {}).items():
        if key in enabled_keys and key not in values:
            values[key] = value
    if data.grade is not None:
        values["grade"] = data.grade
    return values


def _seed_context_facts(fact_service: FactService, context: SessionContext, data: ContextData) -> None:
    for key, value in _context_fact_values(data).items():
        normalized = fact_service.validate_configured_update(key, value)
        if normalized is None:
            continue
        context.update_fact(
            key,
            normalized,
            source_skill="project_backend",
            source_type="project_backend",
            source_label="context_data",
        )
    fact_service.persist_context(context)


def open_or_resume_session(repository, fact_service: FactService, *, session_id: str, data: ContextData) -> tuple[SessionContext, bool]:
    """Return ``(context, created)`` without generating an opening message."""
    try:
        context = repository.get(session_id)
    except KeyError:
        context = None

    if context is not None:
        if context.user_id != data.user_id or context.profile_id != data.profile_id:
            raise HTTPException(status_code=409, detail="SESSION_ID_CONFLICT")
        # context_data is a creation-time seed.  Do not overwrite facts that
        # the later conversation has extracted or that the user has supplied.
        fact_service.hydrate_context(context)
        if _normalize_legacy_default_skill(context):
            repository.save(context)
        return context, False

    profile_name = _seed_profile(fact_service, data)
    context = SessionContext(
        session_id=session_id,
        user_id=data.user_id,
        profile_id=data.profile_id,
        profile_name=profile_name,
    )
    fact_service.hydrate_context(context)
    _seed_context_facts(fact_service, context, data)
    _initialize_general_chat_state(context)
    get_conversation_state(context)
    repository.create(context)
    return context, True


def _initialize_general_chat_state(context: SessionContext) -> None:
    """Seed the only default entry skill without generating an opening turn."""
    context.interaction_state = {"active_skill": GENERAL_CHAT_ID}
    context.skill_states[RUNTIME_STATE_KEY] = {
        "session_id": context.session_id,
        "stage": "answer",
        "collected_info": {},
        "active_skill_id": GENERAL_CHAT_ID,
        "global_facts": {},
        "skill_facts": {GENERAL_CHAT_ID: {}},
        "stage_facts": {GENERAL_CHAT_ID: {"answer": {}}},
        "status_flags": {},
        "route_history": [],
        "conversation_memory": {},
    }
    context.skill_states[CAREER_PLAN_SKILL_ID] = {
        "target_skill": GENERAL_CHAT_ID,
        "stage": "answer",
        "status_flags": {},
        "route_history_count": 0,
        "intent_route": {},
    }
    context.skill_states[LEGACY_MAIN_PLANNER_SKILL_ID] = context.skill_states[CAREER_PLAN_SKILL_ID]


def _normalize_legacy_default_skill(context: SessionContext) -> bool:
    """Convert the former implicit ``main_planner`` entry into general chat.

    Child skills are explicit user choices and must never be rewritten while a
    session is resumed.  ``main_planner`` was never a user-facing entry in the
    single-stream contract, so persisted occurrences are legacy defaults.
    """
    runtime_state = context.skill_states.get(RUNTIME_STATE_KEY)
    runtime_active = str(runtime_state.get("active_skill_id") or "").strip() if isinstance(runtime_state, dict) else ""
    interaction_active = str((context.interaction_state or {}).get("active_skill") or "").strip()
    if interaction_active not in {"", LEGACY_MAIN_PLANNER_SKILL_ID} and runtime_active not in {"", LEGACY_MAIN_PLANNER_SKILL_ID}:
        return False
    if interaction_active not in {"", LEGACY_MAIN_PLANNER_SKILL_ID}:
        return False
    if runtime_active not in {"", LEGACY_MAIN_PLANNER_SKILL_ID}:
        return False
    _initialize_general_chat_state(context)
    return True
