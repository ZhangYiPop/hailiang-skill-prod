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


class StopInput(StrictInput):
    action: Literal["stop"]
    source: Literal["composer"]


StreamInput = Annotated[ChatInput | EnterSkillInput | QuitSkillInput | StopInput, Field(discriminator="action")]


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

        if hasattr(runner, "supersede_active_run"):
            runner.supersede_active_run(request.session_id, context.user_id, next_run_id=request.run_id)
        if isinstance(input_data, ChatInput):
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
