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
    student_name: str | None = Field(default=None, min_length=1)
    user_id: str = Field(min_length=1)
    profile_id: str | None = Field(default=None, min_length=1)
    school_year: str | None = Field(default=None, min_length=1)
    grade: str | None = Field(default=None, min_length=1)
    facts: dict[str, object] = Field(default_factory=dict)


def _seed_profile(
    fact_service: FactService,
    data: ContextData,
    *,
    profile_id: str | None = None,
    allow_context_seed: bool = True,
) -> str | None:
    """Resolve the shared profile while opening a new session.

    The forwarding backend owns the user/profile relationship.  The runtime
    therefore treats ``profile_id`` as the stable child identifier and does
    not require it to belong to the current ``user_id``.  Existing profiles
    are intentionally not renamed by another guardian opening a session.
    """
    target_profile_id = str(profile_id or data.profile_id).strip()
    profile_created = False
    try:
        get_profile_by_id = getattr(fact_service.profile_repo, "get_profile_by_id", None)
        if get_profile_by_id is not None:
            profile = get_profile_by_id(target_profile_id)
        else:
            # Compatibility for lightweight test/dummy repositories.
            get_profile = getattr(fact_service.profile_repo, "get_profile", None)
            if get_profile is None:
                raise KeyError(target_profile_id)
            profile = get_profile(data.user_id, target_profile_id)
    except KeyError:
        try:
            profile = fact_service.profile_repo.create_profile(
                data.user_id,
                profile_id=target_profile_id,
                name=str(data.student_name or "") if allow_context_seed else "",
                shared_facts_initialized=False,
            )
            profile_created = True
        except IntegrityError as exc:
            # Another request may have created the shared profile concurrently.
            try:
                get_profile_by_id = getattr(fact_service.profile_repo, "get_profile_by_id", None)
                if get_profile_by_id is None:
                    raise KeyError(target_profile_id)
                profile = get_profile_by_id(target_profile_id)
            except KeyError as conflict:
                raise HTTPException(status_code=409, detail="PROFILE_ID_CONFLICT") from conflict

    if allow_context_seed and str(profile.get("name") or "") != str(data.student_name or ""):
        try:
            profile = fact_service.profile_repo.update_profile(
                data.user_id,
                target_profile_id,
                name=str(data.student_name or ""),
            )
        except KeyError:
            # Shared profiles can be owned by another guardian. The trusted
            # forwarder remains authoritative for this session's name without
            # rewriting another owner's projection row.
            profile = {**profile, "name": data.student_name}

    profile_facts = fact_service.get_profile_facts(data.user_id, target_profile_id)
    if allow_context_seed and data.school_year is not None and data.grade is not None:
        school_facts = [{"school_year": data.school_year, "grade": data.grade}]
        profile_facts.set_fact(
            "profile_school_facts",
            school_facts,
            source_skill="project_backend",
            source_type="project_backend",
            source_label="context_data",
            scope="profile",
        )
    fact_service.profile_repo.save_profile_facts(data.user_id, target_profile_id, profile_facts)
    if not allow_context_seed and profile_created:
        return None
    return str(profile.get("name") or (str(data.student_name or "") if allow_context_seed else "")) or None


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


def open_or_resume_session(
    repository,
    fact_service: FactService,
    *,
    session_id: str,
    data: ContextData,
    target_profile_id: str | None = None,
    allow_context_seed: bool = True,
    context_scope: str = "profile",
) -> tuple[SessionContext, bool]:
    """Return ``(context, created)`` without generating an opening message."""
    try:
        context = repository.get(session_id)
    except KeyError:
        context = None

    if context_scope not in {"profile", "unbound"}:
        raise HTTPException(status_code=422, detail="INVALID_CONTEXT_SCOPE")
    target_profile_id = str(target_profile_id or data.profile_id or "").strip()
    if context_scope == "profile" and not target_profile_id:
        raise HTTPException(status_code=422, detail="PROFILE_ID_REQUIRED")
    if context is not None:
        if context.user_id != data.user_id:
            raise HTTPException(status_code=409, detail="SESSION_ID_CONFLICT")
        context.session_meta["_session_created"] = False
        context.session_meta["_profile_switched"] = False
        context.session_meta["_profile_branch_created"] = False
        previous_profile_id = str(context.profile_id or "")
        previous_profile_name = context.profile_name
        profile_name = None
        if context_scope == "profile":
            profile_name = _seed_profile(
                fact_service,
                data,
                profile_id=target_profile_id,
                allow_context_seed=allow_context_seed,
            )
        branch_created = False
        if context_scope == "unbound":
            if previous_profile_id:
                branch_created = context.activate_unbound_branch()
                context.append_context_switch(
                    from_profile_id=previous_profile_id,
                    from_profile_name=previous_profile_name,
                )
                context.session_meta["_profile_switched"] = True
        elif previous_profile_id != target_profile_id:
            branch_created = context.activate_profile_branch(
                target_profile_id,
                profile_name=profile_name,
            )
            context.append_context_switch(
                from_profile_id=previous_profile_id,
                from_profile_name=previous_profile_name,
            )
            context.session_meta["_profile_switched"] = True
        elif profile_name:
            context.profile_name = profile_name
        fact_service.hydrate_context(context)
        # Matched forwarding data is the application-side base projection and
        # refreshes on every request. A mismatch never reaches this writer.
        if context_scope == "profile" and allow_context_seed:
            _seed_context_facts(fact_service, context, data)
        if branch_created:
            _initialize_general_chat_state(context)
            get_conversation_state(context)
        context.session_meta["_profile_branch_created"] = branch_created
        if branch_created or previous_profile_id != target_profile_id or _normalize_legacy_default_skill(context):
            repository.save(context)
        return context, False

    profile_name = None
    if context_scope == "profile":
        profile_name = _seed_profile(
            fact_service,
            data,
            profile_id=target_profile_id,
            allow_context_seed=allow_context_seed,
        )
    context = SessionContext(
        session_id=session_id,
        user_id=data.user_id,
        profile_id=target_profile_id or None,
        profile_name=profile_name,
    )
    fact_service.hydrate_context(context)
    if allow_context_seed:
        _seed_context_facts(fact_service, context, data)
    _initialize_general_chat_state(context)
    get_conversation_state(context)
    context.session_meta["_session_created"] = True
    context.session_meta["_profile_branch_created"] = True
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
