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
from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID, canonical_skill_id
from hailiang_skills.core.streaming_runner import StreamingRunner, format_sse_event
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository
from hailiang_skills.storage.repositories.postgres_repo import SessionVersionConflict
from hailiang_skills.core.rate_limit import LLMRateLimitError, LLMRateLimiter
from hailiang_skills.core.logging import make_event
from hailiang_skills.core.message_interactions import ACTIVE, SELECTED, ensure_message_interactions, expire_active_interactions, update_interaction


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatInput(StrictInput):
    action: Literal["chat"]
    content: str = Field(min_length=1)
    source: Literal["chat"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class EnterSkillInput(StrictInput):
    action: Literal["enter_skill"]
    target_skill_id: str = Field(min_length=1)
    source: Literal["toolbar", "route_suggestion"]
    source_message_id: str | None = None
    source_interaction_id: str | None = None
    enable_thinking: bool = False
    return_reasoning: bool = False


class QuitSkillInput(StrictInput):
    action: Literal["quit_skill"]
    target_skill_id: str = Field(min_length=1)
    source: Literal["toolbar", "exit_button"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class ConfirmTeamHandoffInput(StrictInput):
    action: Literal["confirm_team_handoff"]
    source_message_id: str = Field(min_length=1)
    target_expert_id: str = Field(min_length=1)
    source: Literal["team_handoff"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class SwitchTeamMemberInput(StrictInput):
    action: Literal["switch_team_member"]
    target_expert_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source: Literal["toolbar"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class StopInput(StrictInput):
    action: Literal["stop"]
    source: Literal["composer"]


StreamInput = Annotated[ChatInput | EnterSkillInput | QuitSkillInput | ConfirmTeamHandoffInput | SwitchTeamMemberInput | StopInput, Field(discriminator="action")]


class ChatStreamRequest(StrictInput):
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    input: str = Field(min_length=1)
    context_data: ContextData | None = None


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
        if action == "stop":
            return StopInput.model_validate(payload)
        raise HTTPException(status_code=422, detail="unsupported action")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


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
            return context
        except SessionVersionConflict:
            context = repository.get(context.session_id)
    raise HTTPException(status_code=409, detail="SESSION_UPDATE_CONFLICT")


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
) -> APIRouter:
    router = APIRouter()
    runner = StreamingRunner(repository, fact_service, orchestrator, turn_coordinator=turn_coordinator)

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
        context, _ = open_or_resume_session(
            repository, fact_service, session_id=request.session_id, data=request.context_data
        )
        requested_expert_id = str((request.context_data.model_extra or {}).get("expert_id") or "").strip()
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
            if team is not None and context.session_meta.get("active_expert_id") != definition.agent_id:
                raise HTTPException(status_code=409, detail="EXPERT_SWITCH_REQUIRES_STRUCTURED_ACTION")
            if context.session_meta.get("expert_id") != definition.agent_id:
                context.session_meta["expert_id"] = definition.agent_id
                context.session_meta["active_expert_id"] = definition.agent_id
                context.session_meta.pop("expert_requested_skill_id", None)
                repository.save(context)

        if hasattr(runner, "supersede_active_run"):
            runner.supersede_active_run(request.session_id, context.user_id, next_run_id=request.run_id)
        if isinstance(input_data, ConfirmTeamHandoffInput):
            team_member_switch = _confirm_team_handoff(context, orchestrator, input_data)
            prepared_context = context
            context = _claim_external_run(repository, context, request.run_id, action=input_data.action)
            if context is not prepared_context:
                # Optimistic contention reloads the latest session. Re-apply
                # the confirmation so source context and card state survive.
                team_member_switch = _confirm_team_handoff(context, orchestrator, input_data)
            event = make_event("team_handoff_confirmed", {
                "team_id": str(context.session_meta.get("expert_team_id") or ""),
                "expert_id": input_data.target_expert_id,
                "source_message_id": input_data.source_message_id,
            })
            if hasattr(orchestrator, "_record_events"):
                orchestrator._record_events(context, [event])
            else:
                context.event_trace.append(event)
            try:
                repository.save(context)
            except SessionVersionConflict as exc:
                raise HTTPException(status_code=409, detail="SESSION_UPDATE_CONFLICT") from exc
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
            )
        elif isinstance(input_data, SwitchTeamMemberInput):
            team_member_switch = _switch_team_member(context, orchestrator, input_data)
            prepared_context = context
            context = _claim_external_run(repository, context, request.run_id, action=input_data.action)
            if context is not prepared_context:
                team_member_switch = _switch_team_member(context, orchestrator, input_data)
            event = make_event("team_member_selected_from_toolbar", {
                "team_id": str(context.session_meta.get("expert_team_id") or ""),
                "from_expert_id": team_member_switch["from_expert_id"],
                "expert_id": input_data.target_expert_id,
            })
            if hasattr(orchestrator, "_record_events"):
                orchestrator._record_events(context, [event])
            else:
                context.event_trace.append(event)
            try:
                repository.save(context)
            except SessionVersionConflict as exc:
                raise HTTPException(status_code=409, detail="SESSION_UPDATE_CONFLICT") from exc
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
            )
        elif isinstance(input_data, ChatInput):
            # A new free-form turn supersedes any unconfirmed coordinator
            # recommendation.  The same rule already applies to client-side
            # cards; persist it so stale cards cannot be confirmed through
            # a delayed request.
            expired = expire_active_interactions(context.messages)
            if expired:
                context.session_meta.pop("pending_team_handoff", None)
                repository.save(context)
            context = _claim_external_run(repository, context, request.run_id, action=input_data.action)
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
            )
        return StreamingResponse(
            _with_done_event(stream, session_id=request.session_id, run_id=request.run_id),
            media_type="text/event-stream",
            headers=_stream_headers(http_request),
        )

    return router
