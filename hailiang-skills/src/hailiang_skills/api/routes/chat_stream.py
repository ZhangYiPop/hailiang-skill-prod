from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hailiang_skills.api.session_lifecycle import ContextData, open_or_resume_session
from hailiang_skills.core.concurrency import CapacityExceededError, TurnCoordinator
from hailiang_skills.core.fact_service import FactService
from hailiang_skills.core.sse_protocol import SSE_V2_PROTOCOL
from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID, GENERAL_CHAT_SKILL_ID, canonical_skill_id
from hailiang_skills.core.streaming_runner import StreamingRunner, format_sse_event
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository
from hailiang_skills.storage.repositories.postgres_repo import SessionVersionConflict
from hailiang_skills.core.rate_limit import LLMRateLimitError, LLMRateLimiter
from hailiang_skills.core.logging import make_event
from hailiang_skills.core.telemetry import PROFILE_CONTEXT_MISMATCHES
from hailiang_skills.core.message_interactions import ACTIVE, SELECTED, ensure_message_interactions, expire_active_interactions, update_interaction
from hailiang_skills.api.profile_targeting import ProfileTargetResolver
from hailiang_skills.core.session_logging import append_session_events


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileBoundInput(StrictInput):
    # Retain the historical name for import compatibility.  A session action
    # can now explicitly select the session-local unbound context.
    context_scope: Literal["profile", "unbound"] | None = None
    profile_id: str | None = Field(default=None, min_length=1)


class ChatInput(ProfileBoundInput):
    action: Literal["chat"]
    expert_id: str | None = Field(default=None, min_length=1)
    expert_team_id: str | None = Field(default=None, min_length=1)
    content: str = Field(min_length=1)
    source: Literal["chat"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class EnterSkillInput(ProfileBoundInput):
    action: Literal["enter_skill"]
    target_skill_id: str = Field(min_length=1)
    source: Literal["toolbar", "route_suggestion"]
    source_message_id: str | None = None
    source_interaction_id: str | None = None
    enable_thinking: bool = False
    return_reasoning: bool = False


class QuitSkillInput(ProfileBoundInput):
    action: Literal["quit_skill"]
    target_skill_id: str = Field(min_length=1)
    source: Literal["toolbar", "exit_button"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class ConfirmTeamHandoffInput(ProfileBoundInput):
    action: Literal["confirm_team_handoff"]
    source_message_id: str = Field(min_length=1)
    target_expert_id: str = Field(min_length=1)
    source: Literal["team_handoff"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class SwitchTeamMemberInput(ProfileBoundInput):
    action: Literal["switch_team_member"]
    target_expert_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source: Literal["toolbar"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class OpenSessionInput(ProfileBoundInput):
    """Reserved wire contract for a future model-generated opening."""

    action: Literal["open_session"]
    opening_mode: Literal["model"]


class StopInput(StrictInput):
    action: Literal["stop"]
    source: Literal["composer"]


StreamInput = Annotated[ChatInput | EnterSkillInput | QuitSkillInput | ConfirmTeamHandoffInput | SwitchTeamMemberInput | OpenSessionInput | StopInput, Field(discriminator="action")]


class ChatStreamRequest(StrictInput):
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    input: str = Field(min_length=1)
    context_data: ContextData | None = None
    debug_session_id: str | None = Field(default=None, min_length=1)


def _parse_input(raw: str) -> StreamInput:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="INVALID_INPUT_JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="input must be a JSON object")
    try:
        action = payload.get("action")
        if action == "chat":
            return ChatInput.model_validate(payload)
        if action == "enter_skill":
            result = EnterSkillInput.model_validate(payload)
            if result.source == "route_suggestion" and not (
                result.source_message_id and result.source_interaction_id
            ):
                raise HTTPException(
                    status_code=422,
                    detail="route_suggestion requires source_message_id and source_interaction_id",
                )
            return result
        if action == "quit_skill":
            return QuitSkillInput.model_validate(payload)
        if action == "confirm_team_handoff":
            return ConfirmTeamHandoffInput.model_validate(payload)
        if action == "switch_team_member":
            return SwitchTeamMemberInput.model_validate(payload)
        if action == "open_session":
            return OpenSessionInput.model_validate(payload)
        if action == "stop":
            return StopInput.model_validate(payload)
        raise HTTPException(status_code=422, detail="unsupported action")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def _resolve_context_scope(input_data: ProfileBoundInput, data: ContextData) -> tuple[str, str | None]:
    """Resolve one turn's context from the BFF-selected context data.

    ``context_data`` is the forwarding service's current child selection, so
    it is the authoritative source for ordinary chat turns. ``input`` retains
    an optional ``profile_id`` solely for older callers; when supplied it must
    agree with the selected context rather than silently overriding it.
    """
    input_profile_id = str(input_data.profile_id or "").strip()
    context_profile_id = str(data.profile_id or "").strip()
    if input_profile_id and context_profile_id and input_profile_id != context_profile_id:
        raise HTTPException(status_code=409, detail="PROFILE_CONTEXT_MISMATCH")

    scope = input_data.context_scope or ("profile" if (context_profile_id or input_profile_id) else "unbound")
    if scope == "unbound":
        if input_profile_id:
            raise HTTPException(status_code=422, detail="UNBOUND_CONTEXT_MUST_NOT_INCLUDE_PROFILE_ID")
        if any((data.profile_id, data.student_name, data.school_year, data.grade)) or data.facts:
            raise HTTPException(status_code=422, detail="UNBOUND_CONTEXT_MUST_NOT_INCLUDE_PROFILE_DATA")
        return scope, None
    profile_id = context_profile_id or input_profile_id
    if not profile_id:
        raise HTTPException(status_code=422, detail="PROFILE_ID_REQUIRED")
    if not data.profile_id or not data.student_name:
        raise HTTPException(status_code=422, detail="PROFILE_CONTEXT_REQUIRED")
    return scope, profile_id


def _clear_legacy_implicit_expert_team(context) -> bool:
    """Migrate only the former automatic team entry back to general chat.

    Earlier releases wrote ``default_coordinator`` to every newly created
    profile branch. The public contract now keeps ordinary chat independent
    of experts; explicit team selection, direct expert selection and workbench
    snapshots must remain untouched.
    """
    meta = context.session_meta if isinstance(context.session_meta, dict) else {}
    if meta.get("expert_selection_source") != "default_coordinator":
        return False
    if meta.get("workbench_debug_session_id"):
        return False
    for key in (
        "expert_team_id",
        "active_expert_id",
        "expert_id",
        "expert_selection_source",
        "expert_requested_skill_id",
        "pending_team_handoff",
    ):
        meta.pop(key, None)
    active_skill = canonical_skill_id((context.interaction_state or {}).get("active_skill"))
    if active_skill in {"", CAREER_PLAN_SKILL_ID, "expert_direct"}:
        context.interaction_state["active_skill"] = GENERAL_CHAT_SKILL_ID
        runtime_state = context.skill_states.get("skill_runtime")
        if isinstance(runtime_state, dict):
            runtime_state["active_skill_id"] = GENERAL_CHAT_SKILL_ID
            runtime_state.setdefault("skill_facts", {}).setdefault(GENERAL_CHAT_SKILL_ID, {})
            runtime_state.setdefault("stage_facts", {}).setdefault(GENERAL_CHAT_SKILL_ID, {"answer": {}})
        planner_state = context.skill_states.get(CAREER_PLAN_SKILL_ID)
        if isinstance(planner_state, dict):
            planner_state["target_skill"] = GENERAL_CHAT_SKILL_ID
    return True


def _claim_external_run(repository, context, run_id: str, *, action: str):
    """Persist the BFF run id before work starts, so retries cannot duplicate a turn."""
    # An older worker can be finishing exactly while a new action supersedes
    # it. Reload on optimistic-lock contention so this new run is claimed once
    # against the latest context instead of surfacing a transient 409/500.
    for _ in range(3):
        used = context.session_meta.setdefault("external_run_ids", [])
        if not isinstance(used, list):
            used = []
            context.session_meta["external_run_ids"] = used
        if run_id in used:
            raise HTTPException(status_code=409, detail="RUN_ID_CONFLICT")
        used.append(run_id)
        ledger = context.session_meta.setdefault("run_ledger", {})
        if not isinstance(ledger, dict):
            ledger = {}
            context.session_meta["run_ledger"] = ledger
        ledger[run_id] = {"status": "running", "action": action}
        try:
            repository.save(context)
            if hasattr(repository, "record_run"):
                repository.record_run(context, run_id, action)
            return context
        except SessionVersionConflict:
            context = repository.get(context.session_id)
    raise HTTPException(status_code=409, detail="SESSION_UPDATE_CONFLICT")


def _commit_run_action(repository, context, run_id: str, *, action: str, apply):
    """Atomically persist an interactive action together with its run claim.

    A team-handoff confirmation changes both the interaction card and the
    active expert.  Persisting that state after a separate run-claim save
    leaves an avoidable optimistic-lock window (and used to surface as a 409
    to the person clicking the card).  Re-apply the action to a freshly read
    context on contention so validation is still authoritative and no stale
    card state is written.
    """
    for _ in range(3):
        prepared = apply(context)
        used = context.session_meta.setdefault("external_run_ids", [])
        if not isinstance(used, list):
            used = []
            context.session_meta["external_run_ids"] = used
        if run_id in used:
            raise HTTPException(status_code=409, detail="RUN_ID_CONFLICT")
        used.append(run_id)
        ledger = context.session_meta.setdefault("run_ledger", {})
        if not isinstance(ledger, dict):
            ledger = {}
            context.session_meta["run_ledger"] = ledger
        ledger[run_id] = {"status": "running", "action": action}
        try:
            repository.save(context)
            if hasattr(repository, "record_run"):
                repository.record_run(context, run_id, action)
            return context, prepared
        except SessionVersionConflict:
            context = repository.get(context.session_id)
    raise HTTPException(status_code=409, detail="SESSION_UPDATE_CONFLICT")


def _save_and_refresh_context(repository, context):
    """Persist a pre-stream mutation and continue from its stored version.

    PostgreSQL guards every session write with an optimistic version. A single
    API action may need to save setup state (for example a legacy-mode
    migration or an explicit expert choice) before it claims its run. Reload
    after that save so the run-claim write cannot reuse the pre-save version.
    """
    repository.save(context)
    return repository.get(context.session_id)


def _stream_headers(request: Request) -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-SSE-Protocol": SSE_V2_PROTOCOL,
    }


def _state_snapshot(raw_sse: str) -> dict | None:
    """Extract a v2 state payload so the terminal event can repeat it verbatim."""
    lines = raw_sse.splitlines()
    if not lines or lines[0] != "event: state":
        return None
    data_lines = [line[5:].strip() for line in lines if line.startswith("data:")]
    if not data_lines:
        return None
    try:
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _with_done_event(stream, *, session_id: str, run_id: str):
    """Append one terminal event carrying the final complete state snapshot."""
    final_snapshot: dict | None = None
    for raw_sse in stream:
        snapshot = _state_snapshot(raw_sse)
        if snapshot is not None:
            final_snapshot = snapshot
        yield raw_sse
    yield format_sse_event(
        "done",
        final_snapshot
        or {
            "protocol": SSE_V2_PROTOCOL,
            "session_id": session_id,
            "run_id": run_id,
            "status": "completed",
        },
    )


def _pending_native_form(context) -> bool:
    runtime = context.skill_states.get("skill_runtime", {}) if isinstance(context.skill_states, dict) else {}
    active_skill_id = str(runtime.get("active_skill_id") or "") if isinstance(runtime, dict) else ""
    skill_facts = runtime.get("skill_facts", {}) if isinstance(runtime, dict) else {}
    active_facts = skill_facts.get(active_skill_id, {}) if isinstance(skill_facts, dict) else {}
    return isinstance(active_facts, dict) and isinstance(active_facts.get("_pending_questionnaire"), dict)


def _conversation_excerpt(context, *, end_index: int | None = None) -> str:
    end = len(context.messages) if end_index is None else end_index + 1
    excerpt_lines: list[str] = []
    for message in context.messages[max(0, end - 8):end]:
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if metadata.get("hidden"):
            continue
        text = str(message.get("content") or "").strip()
        if not text:
            continue
        label = "用户" if message.get("role") == "user" else "专家"
        excerpt_lines.append(f"{label}：{text}")
    return "\n".join(excerpt_lines)[-4000:]


def _confirm_team_handoff(context, orchestrator, input_data: ConfirmTeamHandoffInput) -> dict:
    if _pending_native_form(context):
        raise HTTPException(status_code=409, detail="TEAM_SWITCH_BLOCKED_BY_PENDING_FORM")
    team_id = str(context.session_meta.get("expert_team_id") or "").strip()
    teams = getattr(orchestrator, "expert_team_registry", None)
    team = teams.get(team_id) if teams is not None else None
    if team is None:
        raise HTTPException(status_code=409, detail="EXPERT_TEAM_NOT_ACTIVE")
    source = next((
        message for message in context.messages
        if str(message.get("message_id") or "") == input_data.source_message_id and message.get("role") == "assistant"
    ), None)
    if source is None:
        raise HTTPException(status_code=404, detail="TEAM_HANDOFF_SOURCE_NOT_FOUND")
    handoff = source.get("team_handoff")
    if not isinstance(handoff, dict):
        metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        handoff = metadata.get("team_handoff")
    if not isinstance(handoff, dict) or str(handoff.get("team_id") or "") != team.team_id:
        raise HTTPException(status_code=409, detail="TEAM_HANDOFF_NOT_ACTIVE")
    candidates = handoff.get("candidates") if isinstance(handoff.get("candidates"), list) else []
    candidate = next((item for item in candidates if isinstance(item, dict) and str(item.get("expert_id") or "") == input_data.target_expert_id), None)
    if candidate is None:
        raise HTTPException(status_code=422, detail="TEAM_HANDOFF_TARGET_NOT_ALLOWED")
    interaction = ensure_message_interactions(source).get("team_handoff")
    if not isinstance(interaction, dict) or interaction.get("status") != ACTIVE:
        raise HTTPException(status_code=409, detail="TEAM_HANDOFF_NOT_ACTIVE")
    try:
        interaction = update_interaction(source, "team_handoff", status=SELECTED, selected_target_skill_id=input_data.target_expert_id)
        interaction["selected_target_expert_id"] = input_data.target_expert_id
    except KeyError:
        raise HTTPException(status_code=409, detail="TEAM_HANDOFF_NOT_ACTIVE") from None
    handoff["status"] = "selected"
    handoff["selected_target_expert_id"] = input_data.target_expert_id
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    if isinstance(metadata.get("team_handoff"), dict):
        metadata["team_handoff"].update({
            "status": "selected",
            "selected_target_expert_id": input_data.target_expert_id,
        })
    from_expert_id = str(context.session_meta.get("active_expert_id") or team.coordinator_expert_id)
    context.session_meta["active_expert_id"] = input_data.target_expert_id
    context.session_meta["expert_id"] = input_data.target_expert_id
    context.session_meta["expert_selection_source"] = "handoff_card"
    context.session_meta.pop("pending_team_handoff", None)
    source_user_message = ""
    source_index = context.messages.index(source)
    for message in reversed(context.messages[:source_index]):
        if message.get("role") == "user" and not (message.get("metadata") or {}).get("hidden"):
            source_user_message = str(message.get("content") or "")
            break
    switch_context = {
        "source": "team_handoff",
        "from_expert_id": from_expert_id,
        "target_expert_id": input_data.target_expert_id,
        "mention_name": str(candidate.get("mention_name") or "").strip(),
        "visible_user_message": f"@{str(candidate.get('mention_name') or '专家').strip()}",
        "source_user_message": source_user_message,
        "coordinator_reason": str(handoff.get("reason") or ""),
        "source_message_id": input_data.source_message_id,
        "conversation_excerpt": _conversation_excerpt(context, end_index=source_index),
    }
    return switch_context


def _switch_team_member(context, orchestrator, input_data: SwitchTeamMemberInput) -> dict:
    if _pending_native_form(context):
        raise HTTPException(status_code=409, detail="TEAM_SWITCH_BLOCKED_BY_PENDING_FORM")
    team_id = str(context.session_meta.get("expert_team_id") or "").strip()
    teams = getattr(orchestrator, "expert_team_registry", None)
    team = teams.get(team_id) if teams is not None else None
    if team is None:
        raise HTTPException(status_code=409, detail="EXPERT_TEAM_NOT_ACTIVE")
    content = input_data.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="TEAM_SWITCH_CONTENT_REQUIRED")
    member = team.member_for_expert(input_data.target_expert_id)
    if member is None:
        raise HTTPException(status_code=422, detail="EXPERT_NOT_IN_ACTIVE_TEAM")
    from_expert_id = str(context.session_meta.get("active_expert_id") or team.coordinator_expert_id)
    context.session_meta["active_expert_id"] = member.expert_id
    context.session_meta["expert_id"] = member.expert_id
    context.session_meta["expert_selection_source"] = "manual"
    context.session_meta.pop("pending_team_handoff", None)
    expire_active_interactions(context.messages)
    return {
        "source": "toolbar",
        "from_expert_id": from_expert_id,
        "target_expert_id": member.expert_id,
        "mention_name": member.mention_name,
        "content": content,
        "visible_user_message": f"@{member.mention_name} {content}",
        "source_message_id": None,
        "conversation_excerpt": _conversation_excerpt(context),
    }


def build_chat_stream_router(
    repository: InMemorySessionRepository,
    fact_service: FactService,
    orchestrator,
    turn_coordinator: TurnCoordinator | None = None,
    llm_rate_limiter: LLMRateLimiter | None = None,
    configuration_snapshot_resolver=None,
    debug_configuration_snapshot_resolver=None,
) -> APIRouter:
    router = APIRouter()
    runner = StreamingRunner(repository, fact_service, orchestrator, turn_coordinator=turn_coordinator)
    profile_target_resolver = ProfileTargetResolver()

    @router.post("/sessions/chat/stream")
    def post_chat_stream(request: ChatStreamRequest, http_request: Request) -> StreamingResponse:
        input_data = _parse_input(request.input)
        if isinstance(input_data, StopInput):
            try:
                context = repository.get(request.session_id)
            except KeyError as exc:
                raise HTTPException(status_code=409, detail="RUN_NOT_ACTIVE") from exc
            # ``stream_stop`` is a generator, so validation inside it would
            # otherwise happen after FastAPI has already returned HTTP 200.
            # Claim cancellation synchronously to keep a stale stop request a
            # deterministic 409 response.
            if not runner.cancel_run(request.session_id, context.user_id, request.run_id):
                raise HTTPException(status_code=409, detail="RUN_NOT_ACTIVE")
            stream = runner.stream_stop(
                request.session_id,
                context.user_id,
                run_id=request.run_id,
                source_endpoint="sessions/chat/stream",
            )
            return StreamingResponse(
                _with_done_event(stream, session_id=request.session_id, run_id=request.run_id),
                media_type="text/event-stream",
                headers=_stream_headers(http_request),
            )

        if request.context_data is None:
            raise HTTPException(
                status_code=422,
                detail="context_data is required for non-stop actions",
            )
        if isinstance(input_data, OpenSessionInput):
            # The contract is deliberately reserved now, while the current
            # product keeps client_opening presentation-only and non-persistent.
            raise HTTPException(status_code=501, detail="MODEL_OPENING_NOT_ENABLED")
        if llm_rate_limiter is not None:
            request_id = str(getattr(http_request.state, "hailiang_request_id", "") or "")
            try:
                llm_rate_limiter.reserve_for_request(request_id)
            except LLMRateLimitError as exc:
                raise HTTPException(
                    status_code=429,
                    detail="LLM_RATE_LIMITED",
                    headers={"Retry-After": "1"},
                ) from exc
        context_scope, selected_profile_id = _resolve_context_scope(input_data, request.context_data)
        if context_scope == "profile":
            resolution = profile_target_resolver.resolve(
                input_profile_id=str(selected_profile_id or ""),
                context_data=request.context_data,
            )
            target_profile_id: str | None = resolution.target_profile_id
            profile_context_status = resolution.status
            allow_context_seed = resolution.allow_context_seed
        else:
            resolution = None
            target_profile_id = None
            profile_context_status = "unbound"
            allow_context_seed = False
        try:
            existing_context = repository.get(request.session_id)
        except KeyError:
            existing_context = None
        if existing_context is not None and existing_context.user_id != request.context_data.user_id:
            raise HTTPException(status_code=409, detail="SESSION_ID_CONFLICT")
        if existing_context is not None and str(existing_context.profile_id or "") != str(target_profile_id or ""):
            ledger = existing_context.session_meta.get("run_ledger")
            running = [
                run_id
                for run_id, item in (ledger.items() if isinstance(ledger, dict) else [])
                if isinstance(item, dict) and item.get("status") == "running" and run_id != request.run_id
            ]
            if running:
                raise HTTPException(status_code=409, detail="ACTIVE_RUN_MUST_STOP")

        context, session_created = open_or_resume_session(
            repository,
            fact_service,
            session_id=request.session_id,
            data=request.context_data,
            target_profile_id=target_profile_id,
            allow_context_seed=allow_context_seed,
            context_scope=context_scope,
        )
        bound_debug_session_id = str(context.session_meta.get("workbench_debug_session_id") or "")
        if request.debug_session_id and bound_debug_session_id and request.debug_session_id != bound_debug_session_id:
            raise HTTPException(status_code=409, detail="DEBUG_SNAPSHOT_CONFLICT")
        preflight_context_saved = False
        if session_created and request.debug_session_id:
            if debug_configuration_snapshot_resolver is None:
                raise HTTPException(status_code=422, detail="DEBUG_SNAPSHOT_NOT_AVAILABLE")
            try:
                snapshot = debug_configuration_snapshot_resolver(request.debug_session_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            context.session_meta["configuration_snapshot"] = snapshot
            context.session_meta["workbench_debug_session_id"] = request.debug_session_id
            repository.save(context)
            preflight_context_saved = True
        elif session_created and configuration_snapshot_resolver is not None:
            snapshot = configuration_snapshot_resolver()
            if snapshot is not None:
                context.session_meta["configuration_snapshot"] = snapshot
                repository.save(context)
                preflight_context_saved = True
        if preflight_context_saved:
            context = repository.get(context.session_id)
        legacy_default_team_cleared = _clear_legacy_implicit_expert_team(context)
        profile_switched = bool(context.session_meta.get("_profile_switched"))

        profile_context_event = {
            "profile_id": context.profile_id,
            "profile_name": context.profile_name,
            "context_scope": context.context_scope,
            "context_label": context.context_label,
            "context_switched": profile_switched,
            "branch_version": int(context.session_meta.get("_active_branch_version") or 0),
            "profile_context_status": profile_context_status,
            "session_created": bool(session_created),
            "profile_switched": profile_switched,
        }
        context.session_meta["_sse_profile_context"] = profile_context_event
        if resolution is not None and resolution.status == "mismatched":
            if PROFILE_CONTEXT_MISMATCHES:
                PROFILE_CONTEXT_MISMATCHES.labels(authority=resolution.authority).inc()
            mismatch_event = make_event("profile_context_mismatch", {
                "input_profile_id": resolution.input_profile_id,
                "context_profile_id": resolution.context_profile_id,
                "target_profile_id": resolution.target_profile_id,
                "authority": resolution.authority,
            })
            context.event_trace.append(mismatch_event)
            append_session_events(request.session_id, [mismatch_event])

        requested_team_id = str(input_data.expert_team_id or "").strip() if isinstance(input_data, ChatInput) else ""
        if requested_team_id:
            teams = getattr(orchestrator, "expert_team_registry", None)
            team = teams.get(requested_team_id) if teams is not None else None
            if team is None:
                raise HTTPException(status_code=422, detail="EXPERT_TEAM_NOT_FOUND")
            context.session_meta["expert_team_id"] = team.team_id
            context.session_meta["expert_id"] = team.coordinator_expert_id
            context.session_meta["active_expert_id"] = team.coordinator_expert_id
            context.session_meta["expert_selection_source"] = "manual_team"
            context.session_meta.pop("expert_requested_skill_id", None)
            context.session_meta.pop("pending_team_handoff", None)
        requested_expert_id = str(input_data.expert_id or "").strip() if isinstance(input_data, ChatInput) else ""
        if requested_expert_id:
            expert_registry = getattr(orchestrator, "expert_registry", None)
            definition = expert_registry.get(requested_expert_id) if expert_registry is not None else None
            if definition is None:
                raise HTTPException(status_code=422, detail="EXPERT_NOT_FOUND")
            team_id = str(context.session_meta.get("expert_team_id") or "").strip()
            teams = getattr(orchestrator, "expert_team_registry", None)
            team = teams.get(team_id) if team_id and teams is not None else None
            if team is not None and definition.agent_id not in team.member_expert_ids:
                raise HTTPException(status_code=422, detail="EXPERT_NOT_IN_ACTIVE_TEAM")
            context.session_meta["expert_id"] = definition.agent_id
            context.session_meta["active_expert_id"] = definition.agent_id
            context.session_meta["expert_selection_source"] = "manual"
            context.session_meta.pop("expert_requested_skill_id", None)
            context.session_meta.pop("pending_team_handoff", None)

        if legacy_default_team_cleared or requested_team_id or requested_expert_id or profile_context_status == "mismatched":
            context = _save_and_refresh_context(repository, context)

        if not profile_switched and hasattr(runner, "supersede_active_run"):
            runner.supersede_active_run(request.session_id, context.user_id, next_run_id=request.run_id)
        if isinstance(input_data, ConfirmTeamHandoffInput):
            def apply_handoff(current_context):
                team_member_switch = _confirm_team_handoff(current_context, orchestrator, input_data)
                event = make_event("team_handoff_confirmed", {
                    "team_id": str(current_context.session_meta.get("expert_team_id") or ""),
                    "expert_id": input_data.target_expert_id,
                    "source_message_id": input_data.source_message_id,
                })
                # Keep the event in the same optimistic write as the selected
                # card and active-expert state.  The file index is appended
                # only after that write succeeds, avoiding orphan audit rows.
                current_context.event_trace.append(event)
                return team_member_switch, event

            context, (team_member_switch, event) = _commit_run_action(
                repository,
                context,
                request.run_id,
                action=input_data.action,
                apply=apply_handoff,
            )
            append_session_events(request.session_id, [event])
            try:
                lease = runner.reserve_turn(request.session_id, context.user_id, run_id=request.run_id)
            except CapacityExceededError as exc:
                raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
            stream = runner.stream_message(
                request.session_id,
                context.user_id,
                str(team_member_switch.get("source_user_message") or "专家接管"),
                enable_thinking=input_data.enable_thinking,
                return_reasoning=input_data.return_reasoning,
                team_member_switch=team_member_switch,
                lease=lease,
                protocol=SSE_V2_PROTOCOL,
                source_endpoint="sessions/chat/stream",
                initial_events=[("profile_context", profile_context_event)],
            )
        elif isinstance(input_data, SwitchTeamMemberInput):
            def apply_member_switch(current_context):
                team_member_switch = _switch_team_member(current_context, orchestrator, input_data)
                event = make_event("team_member_selected_from_toolbar", {
                    "team_id": str(current_context.session_meta.get("expert_team_id") or ""),
                    "from_expert_id": team_member_switch["from_expert_id"],
                    "expert_id": input_data.target_expert_id,
                })
                current_context.event_trace.append(event)
                return team_member_switch, event

            context, (team_member_switch, event) = _commit_run_action(
                repository,
                context,
                request.run_id,
                action=input_data.action,
                apply=apply_member_switch,
            )
            append_session_events(request.session_id, [event])
            try:
                lease = runner.reserve_turn(request.session_id, context.user_id, run_id=request.run_id)
            except CapacityExceededError as exc:
                raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
            stream = runner.stream_message(
                request.session_id,
                context.user_id,
                input_data.content,
                enable_thinking=input_data.enable_thinking,
                return_reasoning=input_data.return_reasoning,
                team_member_switch=team_member_switch,
                lease=lease,
                protocol=SSE_V2_PROTOCOL,
                source_endpoint="sessions/chat/stream",
                initial_events=[("profile_context", profile_context_event)],
            )
        elif isinstance(input_data, ChatInput):
            # A new free-form turn supersedes any unconfirmed coordinator
            # recommendation.  The same rule already applies to client-side
            # cards; persist it so stale cards cannot be confirmed through
            # a delayed request.  Expire those interactions in the same
            # optimistic write that claims this run: ``supersede_active_run``
            # may have advanced the stored version immediately beforehand.
            def apply_free_form_turn(current_context):
                expired = expire_active_interactions(current_context.messages)
                if expired:
                    current_context.session_meta.pop("pending_team_handoff", None)
                return expired

            context, _ = _commit_run_action(
                repository,
                context,
                request.run_id,
                action=input_data.action,
                apply=apply_free_form_turn,
            )
            try:
                lease = runner.reserve_turn(request.session_id, context.user_id, run_id=request.run_id)
            except CapacityExceededError as exc:
                raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
            stream = runner.stream_message(
                request.session_id,
                context.user_id,
                input_data.content,
                enable_thinking=input_data.enable_thinking,
                return_reasoning=input_data.return_reasoning,
                lease=lease,
                protocol=SSE_V2_PROTOCOL,
                source_endpoint="sessions/chat/stream",
                initial_events=[("profile_context", profile_context_event)],
            )
        else:
            action = "enter" if input_data.action == "enter_skill" else "exit"
            if isinstance(input_data, EnterSkillInput) and context.session_meta.get("expert_team_id"):
                raise HTTPException(status_code=409, detail="SKILL_ENTRY_BLOCKED_IN_EXPERT_TEAM")
            if isinstance(input_data, EnterSkillInput) and context.session_meta.get("expert_id"):
                # Toolbar/route-suggestion Skill entry is an explicit
                # standalone debug action.  Leave expert mode first so this
                # direct selection cannot accidentally bypass an expert's
                # locked Skill set.
                context.session_meta.pop("expert_id", None)
                context.session_meta.pop("active_expert_id", None)
                context.session_meta.pop("expert_requested_skill_id", None)
                repository.save(context)
            if isinstance(input_data, QuitSkillInput):
                active_skill = str(
                    context.interaction_state.get("active_skill")
                    or context.skill_states.get("skill_runtime", {}).get("active_skill_id")
                    or CAREER_PLAN_SKILL_ID
                )
                active_skill = canonical_skill_id(active_skill)
                if input_data.target_skill_id != active_skill:
                    raise HTTPException(status_code=409, detail="QUIT_SKILL_TARGET_MISMATCH")
            try:
                prepared = runner.prepare_skill_transition(
                    request.session_id,
                    context.user_id,
                    action=action,
                    target_skill_id=input_data.target_skill_id,
                    source=input_data.source,
                    source_message_id=getattr(input_data, "source_message_id", None),
                    source_interaction_id=getattr(input_data, "source_interaction_id", None),
                    run_id=request.run_id,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            context = _claim_external_run(repository, context, request.run_id, action=input_data.action)
            try:
                lease = runner.reserve_turn(request.session_id, context.user_id, run_id=request.run_id)
            except CapacityExceededError as exc:
                raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
            stream = runner.stream_skill_transition(
                request.session_id,
                context.user_id,
                action=action,
                target_skill_id=input_data.target_skill_id,
                source=input_data.source,
                source_message_id=getattr(input_data, "source_message_id", None),
                source_interaction_id=getattr(input_data, "source_interaction_id", None),
                enable_thinking=input_data.enable_thinking,
                return_reasoning=input_data.return_reasoning,
                prepared_transition=prepared,
                lease=lease,
                protocol=SSE_V2_PROTOCOL,
                source_endpoint="sessions/chat/stream",
                initial_events=[("profile_context", profile_context_event)],
            )
        return StreamingResponse(
            _with_done_event(stream, session_id=request.session_id, run_id=request.run_id),
            media_type="text/event-stream",
            headers=_stream_headers(http_request),
        )

    return router
