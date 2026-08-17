from __future__ import annotations

import hmac
import json
import os
import re
from collections.abc import Iterator
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from hailiang_skills.api.session_lifecycle import ContextData, open_or_resume_session
from hailiang_skills.core.concurrency import CapacityExceededError, TurnCoordinator
from hailiang_skills.core.fact_service import FactService
from hailiang_skills.core.rate_limit import LLMRateLimitError, LLMRateLimiter
from hailiang_skills.core.sse_protocol import SSE_V2_PROTOCOL
from hailiang_skills.core.streaming_runner import StreamingRunner
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository


INTERNAL_INFO_REFUSAL = (
    "抱歉，我不能提供或讨论平台的内部配置、工作指令或安全机制。"
    "请直接说明孩子的实际升学问题，我会在可服务范围内协助分析。"
)
_INTERNAL_INFO_PATTERNS = (
    re.compile(r"你是(?:什么|哪种|哪个)模型|什么模型|底层模型|基座模型|模型来源|模型版本|大模型来源"),
    re.compile(r"底层架构|系统架构|技术实现|后端实现|后台实现|怎么实现|如何工作|工作原理"),
    re.compile(r"调用(?:了哪些|什么)?(?:api|接口)|后台调用|外部服务|第三方接口|第三方服务|接口地址|接口文档", re.I),
    re.compile(r"deepseek|qwen|通义|chatgpt|openai|anthropic|claude", re.I),
)


def _requires_internal_info_refusal(content: str) -> bool:
    normalized = re.sub(r"\s+", "", content or "")
    return any(pattern.search(normalized) for pattern in _INTERNAL_INFO_PATTERNS)


class ExternalDialogueMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "model", "assistant"]
    content: str = Field(min_length=1)


class ExternalChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(default="default", min_length=1)
    max_tokens: int = Field(default=1024, ge=1, le=32768)
    stream: bool = False
    dialogue: list[ExternalDialogueMessage] = Field(min_length=1)


def _api_key_or_error(authorization: str | None, configured_key: str) -> None:
    if not configured_key:
        raise HTTPException(status_code=503, detail="EXTERNAL_API_NOT_CONFIGURED")
    prefix = "Bearer "
    supplied = authorization[len(prefix) :].strip() if authorization and authorization.startswith(prefix) else ""
    # compare_digest(str, str) only accepts ASCII on CPython. API keys are
    # normally ASCII, but encoding both values makes the boundary robust and
    # prevents a malformed/non-ASCII configured key from becoming HTTP 500.
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), configured_key.encode("utf-8")):
        raise HTTPException(status_code=401, detail="INVALID_API_KEY")


def _new_identity() -> tuple[str, str, str, str]:
    token = uuid4().hex
    return (
        f"external_user_{token}",
        f"external_profile_{token}",
        f"sess_external_{token}",
        f"run_external_{token}",
    )


def _event_payload(raw_sse: str) -> dict | None:
    lines = raw_sse.splitlines()
    if not lines or lines[0] != "event: state":
        return None
    data = "\n".join(line[5:].strip() for line in lines if line.startswith("data:"))
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _external_events(stream: Iterator[str], *, session_id: str, request_id: str) -> Iterator[str]:
    previous = ""
    terminal_status = "success"
    terminal_reason = "success"
    for raw_sse in stream:
        payload = _event_payload(raw_sse)
        if payload is None:
            continue
        assistant = payload.get("assistant") if isinstance(payload.get("assistant"), dict) else {}
        content = str(assistant.get("content") or "")
        delta = content[len(previous) :] if content.startswith(previous) else content
        previous = content
        status = str(payload.get("status") or "")
        if status in {"failed", "blocked"}:
            terminal_status = "failed"
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            terminal_reason = str(error.get("message") or "模型未能正常回答")
        if delta:
            yield _sse_json({"content": "", "choices": [{"delta": delta}], "status": "success", "reason": "success"})
    yield _sse_json(
        {
            "content": "",
            "choices": [{"delta": "", "finish_reason": "stop"}],
            "status": terminal_status,
            "reason": terminal_reason,
            "session_id": session_id,
            "request_id": request_id,
        }
    )


def _sse_json(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _refusal_stream(*, session_id: str, request_id: str) -> Iterator[str]:
    yield _sse_json({
        "content": "",
        "choices": [{"delta": INTERNAL_INFO_REFUSAL}],
        "status": "success",
        "reason": "success",
    })
    yield _sse_json({
        "content": "",
        "choices": [{"delta": "", "finish_reason": "stop"}],
        "status": "success",
        "reason": "success",
        "session_id": session_id,
        "request_id": request_id,
    })


def build_external_chat_router(
    repository: InMemorySessionRepository,
    fact_service: FactService,
    orchestrator,
    turn_coordinator: TurnCoordinator | None = None,
    llm_rate_limiter: LLMRateLimiter | None = None,
) -> APIRouter:
    router = APIRouter(tags=["external"])
    runner = StreamingRunner(repository, fact_service, orchestrator, turn_coordinator=turn_coordinator)

    @router.post("/external/chat", response_model=None)
    def post_external_chat(
        request: ExternalChatRequest,
        http_request: Request,
        authorization: str | None = Header(default=None),
    ):
        _api_key_or_error(authorization, os.getenv("HAILIANG_EXTERNAL_API_KEY", "").strip())
        if request.dialogue[-1].role != "user":
            raise HTTPException(status_code=422, detail="DIALOGUE_LAST_MESSAGE_MUST_BE_USER")

        user_id, profile_id, session_id, run_id = _new_identity()
        context_data = ContextData(
            student_name="external_test_user",
            user_id=user_id,
            profile_id=profile_id,
        )
        context, _ = open_or_resume_session(
            repository, fact_service, session_id=session_id, data=context_data
        )
        for message in request.dialogue[:-1]:
            context.add_message(
                "user" if message.role == "user" else "assistant",
                message.content,
                metadata={"source": "external_dialogue"},
            )
        if _requires_internal_info_refusal(request.dialogue[-1].content):
            context.add_message(
                "user",
                request.dialogue[-1].content,
                metadata={"source": "external_dialogue"},
            )
            context.add_message(
                "assistant",
                INTERNAL_INFO_REFUSAL,
                metadata={"source": "external_policy", "policy_action": "refuse_internal_info"},
            )
            repository.save(context)
            request_id = str(getattr(http_request.state, "hailiang_request_id", "") or "")
            if request.stream:
                return StreamingResponse(
                    _refusal_stream(session_id=session_id, request_id=request_id),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            return JSONResponse(
                status_code=200,
                content={
                    "content": INTERNAL_INFO_REFUSAL,
                    "choices": [],
                    "status": "success",
                    "reason": "success",
                    "session_id": session_id,
                    "request_id": request_id,
                },
            )
        repository.save(context)

        if llm_rate_limiter is not None:
            request_id = str(getattr(http_request.state, "hailiang_request_id", "") or "")
            try:
                llm_rate_limiter.reserve_for_request(request_id)
            except LLMRateLimitError as exc:
                raise HTTPException(status_code=429, detail="LLM_RATE_LIMITED", headers={"Retry-After": "1"}) from exc
        try:
            lease = runner.reserve_turn(session_id, user_id, run_id=run_id)
        except CapacityExceededError as exc:
            raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc

        stream = runner.stream_message(
            session_id,
            user_id,
            request.dialogue[-1].content,
            lease=lease,
            protocol=SSE_V2_PROTOCOL,
            source_endpoint="external/chat",
        )
        if request.stream:
            request_id = str(getattr(http_request.state, "hailiang_request_id", "") or "")
            return StreamingResponse(
                _external_events(stream, session_id=session_id, request_id=request_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        content = ""
        status = "success"
        reason = "success"
        for raw_sse in stream:
            payload = _event_payload(raw_sse)
            if payload is None:
                continue
            assistant = payload.get("assistant") if isinstance(payload.get("assistant"), dict) else {}
            content = str(assistant.get("content") or content)
            if str(payload.get("status") or "") in {"failed", "blocked"}:
                status = "failed"
                error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                reason = str(error.get("message") or "模型未能正常回答")
        return JSONResponse(
            status_code=200,
            content={
                "content": content if status == "success" else "",
                "choices": [],
                "status": status,
                "reason": reason,
                "session_id": session_id,
                "request_id": str(getattr(http_request.state, "hailiang_request_id", "") or ""),
            },
        )

    return router
