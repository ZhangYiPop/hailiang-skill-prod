from __future__ import annotations

from time import perf_counter

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.encoders import jsonable_encoder

from hailiang_skills.api.routes.admin_assets import build_admin_assets_router
from hailiang_skills.api.routes.security_quarantine import build_security_quarantine_router
from hailiang_skills.api.routes.skill_analytics import build_skill_analytics_router
from hailiang_skills.api.routes.chat import build_chat_router
from hailiang_skills.api.routes.chat_stream import build_chat_stream_router
from hailiang_skills.api.routes.external_chat import build_external_chat_router
from hailiang_skills.api.routes.facts import build_facts_router
from hailiang_skills.api.routes.profiles import build_profiles_router
from hailiang_skills.core.fact_service import FactService
from hailiang_skills.core.registry import SkillRegistry
from hailiang_skills.llm.client import LLMClient
from hailiang_skills.llm.config import load_llm_config
from hailiang_skills.runtime_bridge import MainPlannerOrchestrator
from hailiang_skills.skills.admission import AdmissionSkill
from hailiang_skills.skills.chat import ChatSkill
from hailiang_skills.skills.convergence import ConvergenceSkill
from hailiang_skills.skills.facts_extractor import FactsExtractorSkill
from hailiang_skills.skills.logging_skill import LoggingSkill
from hailiang_skills.skills.path_drilldown import PathDrillDownSkill
from hailiang_skills.skills.planner import PlannerSkill
from hailiang_skills.skills.router import RouterSkill
from hailiang_skills.skills.school_intro import SchoolIntroSkill
from hailiang_skills.skills.terminate_or_recommend import TerminateOrRecommendSkill
from hailiang_skills.storage.repositories.profile_repo import (
    FileBackedProfileRepository,
)
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository
from hailiang_skills.storage.repositories.user_fact_repo import (
    FileBackedUserFactRepository,
)
from hailiang_skills.security.quarantine_store import QuarantineStore
from hailiang_skills.security.moderation_service import ModerationService
from hailiang_skills.core.concurrency import TurnCoordinator
from hailiang_skills.core.skill_ids import GENERAL_CHAT_SKILL_ID, LEGACY_MAIN_PLANNER_SKILL_ID
from hailiang_skills.core.telemetry import (
    REQUEST_DURATION,
    bind_telemetry,
    configure_otel,
    current_telemetry,
    prometheus_payload,
    reset_telemetry,
    span,
)
from hailiang_skills.storage.factory import build_storage_from_env
from hailiang_skills.storage.audit_store import EncryptedAuditStore
from hailiang_skills.core.audit import set_audit_store
from hailiang_skills.core.request_logging import append_http_request_record
from hailiang_skills.core.rate_limit import get_llm_rate_limiter
from hailiang_skills.core.deployment import deployment_environment, node_name, release_version, state_root
from hailiang_skills.storage.event_store import configure_event_store
from hailiang_skills.storage.repositories.postgres_repo import SessionVersionConflict
from pathlib import Path
import os
import json


_HTTP_ERROR_MESSAGES = {
    "INVALID_INPUT_JSON": "input 必须是合法的 JSON 对象字符串。",
    "UNSUPPORTED_ACTION": "input.action 不受支持。",
    "RUN_ID_CONFLICT": "run_id 已使用，不能重复提交。",
    "RUN_NOT_ACTIVE": "run_id 不是当前活动任务，无法停止。",
    "SESSION_ID_CONFLICT": "session_id 不属于当前 user_id 和 profile_id。",
    "PROFILE_ID_CONFLICT": "profile_id 已属于其他用户，不能复用。",
    "SESSION_UPDATE_CONFLICT": "会话同时被更新，请读取最新状态后重试。",
    "SSE_CAPACITY_EXCEEDED": "流式服务当前繁忙，请稍后重试。",
    "REQUEST_VALIDATION_ERROR": "请求字段校验失败。",
    "INVALID_ROUTE_SUGGESTION": "Skill 推荐来源无效或已失效。",
    "QUIT_SKILL_TARGET_MISMATCH": "target_skill_id 与当前活动 Skill 不一致。",
    "TARGET_SKILL_ALREADY_ACTIVE": "目标 Skill 已是当前活动 Skill，无需重复进入。",
    "SESSION_NOT_FOUND": "会话不存在。",
    "PROFILE_NOT_FOUND": "孩子档案不存在。",
    "PROFILE_ACCESS_DENIED": "孩子档案不属于当前用户。",
    "ASSISTANT_MESSAGE_NOT_FOUND": "助手消息不存在。",
    "MESSAGE_INTERACTION_NOT_FOUND": "消息交互项不存在。",
    "SESSION_USER_MISMATCH": "请求 user_id 与会话归属不一致。",
    "INVALID_INTERACTION_TYPE": "仅支持更新 fact_form 类型的消息交互。",
    "INTERACTION_INACTIVE": "消息交互项已失效。",
    "INVALID_SUBMITTED_FACT_KEYS": "提交的 Fact 字段与表单不一致。",
    "LOG_EXPORT_DISABLED": "生产环境禁止导出原始会话日志。",
    "LLM_RATE_LIMITED": "模型服务当前繁忙，请稍后重试。",
    "INVALID_API_KEY": "API Key 无效。",
    "EXTERNAL_API_NOT_CONFIGURED": "外部测试接口尚未配置 API Key。",
    "DIALOGUE_LAST_MESSAGE_MUST_BE_USER": "dialogue 最后一条消息必须是 user。",
}


def _http_error_payload(detail: object, *, default_code: str) -> dict[str, object]:
    """Return the stable pre-SSE error envelope while retaining FastAPI detail."""
    code = default_code
    if isinstance(detail, dict) and isinstance(detail.get("code"), str):
        code = detail["code"]
    elif isinstance(detail, str):
        if detail in _HTTP_ERROR_MESSAGES:
            code = detail
        elif detail == "unsupported action":
            code = "UNSUPPORTED_ACTION"
        elif detail == "target_skill_id does not match active skill":
            code = "QUIT_SKILL_TARGET_MISMATCH"
        elif detail == "route_suggestion requires source_message_id and source_interaction_id":
            code = "INVALID_ROUTE_SUGGESTION"
        elif "SSE capacity is saturated" in detail:
            code = "SSE_CAPACITY_EXCEEDED"
        else:
            code = {
                "session not found": "SESSION_NOT_FOUND",
                "profile not found": "PROFILE_NOT_FOUND",
                "profile does not belong to current user": "PROFILE_ACCESS_DENIED",
                "assistant message not found": "ASSISTANT_MESSAGE_NOT_FOUND",
                "message interaction not found": "MESSAGE_INTERACTION_NOT_FOUND",
                "request user_id does not match session": "SESSION_USER_MISMATCH",
                "only fact_form interactions can be updated": "INVALID_INTERACTION_TYPE",
                "message interaction is no longer active": "INTERACTION_INACTIVE",
                "submitted fact keys do not match the form": "INVALID_SUBMITTED_FACT_KEYS",
                "production log export is disabled": "LOG_EXPORT_DISABLED",
            }.get(detail, code)
    return {
        "code": code,
        "message": (
            str(detail.get("message"))
            if isinstance(detail, dict) and isinstance(detail.get("message"), str)
            else _HTTP_ERROR_MESSAGES.get(code, "请求处理失败。")
        ),
        # Retained for callers that already depend on FastAPI's error shape.
        "detail": jsonable_encoder(detail),
    }


def _extract_request_context(request: Request, *, body_payload: dict[str, object] | None = None) -> dict[str, str]:
    payload = body_payload or {}
    session_id = str(request.path_params.get("session_id") or payload.get("session_id") or "")
    profile_id = str(request.query_params.get("profile_id") or payload.get("profile_id") or "")
    user_id = str(payload.get("user_id") or "")
    run_id = str(payload.get("run_id") or "")
    context_data = payload.get("context_data")
    if isinstance(context_data, dict):
        user_id = user_id or str(context_data.get("user_id") or "")
        profile_id = profile_id or str(context_data.get("profile_id") or "")
    return {
        "session_id": session_id,
        "profile_id": profile_id,
        "user_id": user_id,
        "run_id": run_id,
    }


def _response_error_metadata(response: object) -> dict[str, object]:
    """Extract stable JSON error fields without logging the full response body."""
    raw_body = getattr(response, "body", None)
    if not isinstance(raw_body, (bytes, bytearray)) or not raw_body:
        return {}
    try:
        payload = json.loads(raw_body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    metadata: dict[str, object] = {}
    for key in ("code", "message", "detail"):
        if key in payload:
            metadata[f"response_error_{key}"] = payload[key]
    return metadata


async def _read_json_body(request: Request) -> dict[str, object] | None:
    content_type = request.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return None
    body = await request.body()
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def create_app() -> FastAPI:
    configure_otel()
    llm_config = load_llm_config()
    llm_client = LLMClient(llm_config)
    registry = SkillRegistry()
    for skill in [
        RouterSkill(llm_client),
        FactsExtractorSkill(llm_client),
        PlannerSkill(llm_client),
        ChatSkill(llm_client),
        AdmissionSkill(llm_client),
        ConvergenceSkill(llm_client),
        PathDrillDownSkill(llm_client),
        SchoolIntroSkill(llm_client),
        TerminateOrRecommendSkill(llm_client),
        LoggingSkill(),
    ]:
        registry.register(skill)

    storage = build_storage_from_env()
    repository = storage.session_repository
    fact_service = FactService(
        storage.user_fact_repository,
        storage.profile_repository,
    )

    app = FastAPI(title="hailiang-skills")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_http_error_payload(
                exc.detail,
                default_code="REQUEST_VALIDATION_ERROR" if exc.status_code == 422 else "HTTP_ERROR",
            ),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_http_error_payload(exc.errors(), default_code="REQUEST_VALIDATION_ERROR"),
        )
    app.state.storage = storage
    configure_event_store(storage.session_factory)
    app.state.turn_coordinator = TurnCoordinator()
    app.state.llm_rate_limiter = get_llm_rate_limiter()
    audit_store = None
    if storage.session_factory is not None and os.getenv("HAILIANG_AUDIT_ENCRYPTION_KEY"):
        audit_store = EncryptedAuditStore(storage.session_factory)
    set_audit_store(audit_store)
    app.state.audit_store = audit_store
    quarantine_root = Path(os.getenv("HAILIANG_SECURITY_QUARANTINE_DIR", str(state_root() / "security_quarantine")))
    quarantine_store = QuarantineStore(quarantine_root)
    app.state.quarantine_store = quarantine_store
    moderation_service = ModerationService(
        lexicon_dir=Path(__file__).resolve().parents[3] / "config" / "Sensitive_lexicon",
        quarantine_store=quarantine_store,
    )
    app.state.moderation_service = moderation_service
    orchestrator = MainPlannerOrchestrator(registry, llm_config, moderation_service=moderation_service)
    configured_origins = [item.strip() for item in os.getenv("HAILIANG_CORS_ORIGINS", "").split(",") if item.strip()]
    cors_origins = configured_origins or [
        "http://127.0.0.1:4174",
        "http://127.0.0.1:4175",
        "http://127.0.0.1:4176",
        "http://127.0.0.1:4177",
        "http://localhost:4174",
        "http://localhost:4175",
        "http://localhost:4176",
        "http://localhost:4177",
    ]
    # Allow arbitrary local dev ports (run.sh may move Vite from 4174 to 4177);
    # production origins still come from the explicit allow-list.
    cors_origin_regex = r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?$"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=cors_origin_regex,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "X-Trace-Id", "X-App-Version", "X-App-Node", "Retry-After"],
    )

    @app.exception_handler(SessionVersionConflict)
    async def session_version_conflict(_: Request, exc: SessionVersionConflict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                **_http_error_payload(
                    "session changed concurrently; reload context and retry",
                    default_code="SESSION_UPDATE_CONFLICT",
                ),
                "error": str(exc),
            },
        )

    @app.middleware("http")
    async def request_observability(request: Request, call_next):
        """Attach correlation metadata to every HTTP/SSE request and response."""
        started = perf_counter()
        request_id = request.headers.get("X-Request-Id") or f"req_{__import__('uuid').uuid4().hex[:16]}"
        request.state.hailiang_request_id = request_id
        traceparent = request.headers.get("traceparent", "")
        trace_parts = traceparent.split("-")
        incoming_trace_id = trace_parts[1] if len(trace_parts) == 4 and len(trace_parts[1]) == 32 else ""
        body_payload = await _read_json_body(request)
        request_context = _extract_request_context(request, body_payload=body_payload)
        session_id = request_context["session_id"]
        profile_id = request_context["profile_id"]
        run_id = request_context["run_id"]
        if request.method == "OPTIONS":
            response = await call_next(request)
            response.headers.setdefault("X-Request-Id", request_id)
            # Chrome may send a Private Network Access preflight when a page
            # on localhost calls a loopback API with a different hostname.
            # The local development server is intentionally loopback-only.
            if request.headers.get("Access-Control-Request-Private-Network") == "true":
                response.headers.setdefault("Access-Control-Allow-Private-Network", "true")
            return response
        # The project BFF is the trust boundary. It authenticates users before
        # calling this private algorithm service, so no browser/JWT auth runs here.
        route_template = request.url.path
        context, token = bind_telemetry(
            request_id=request_id,
            trace_id=incoming_trace_id,
            route=route_template,
            session_id=session_id,
            profile_id=profile_id,
            user_id=request_context["user_id"],
            run_id=run_id,
        )
        status_code = 500
        response_error_metadata: dict[str, object] = {}
        try:
            with span("http.request", node="http_request", attributes={"method": request.method, "route": route_template}):
                response = await call_next(request)
            matched_route = getattr(request.scope.get("route"), "path", route_template)
            status_code = response.status_code
            if status_code >= 400:
                response_error_metadata = _response_error_metadata(response)
            response.headers["X-Request-Id"] = request_id
            response.headers["X-Trace-Id"] = context.trace_id
            response.headers["X-App-Version"] = release_version()
            response.headers["X-App-Node"] = node_name()
            if REQUEST_DURATION:
                REQUEST_DURATION.labels(route=matched_route, method=request.method, status_code=str(response.status_code)).observe(perf_counter() - started)
            return response
        except Exception as exc:
            matched_route = getattr(request.scope.get("route"), "path", route_template)
            if REQUEST_DURATION:
                REQUEST_DURATION.labels(route=matched_route, method=request.method, status_code="500").observe(perf_counter() - started)
            status_code = 500
            request.state.request_observability_error = type(exc).__name__
            raise
        finally:
            matched_route = getattr(request.scope.get("route"), "path", route_template)
            append_http_request_record(
                method=request.method,
                route=matched_route,
                status_code=status_code,
                duration_ms=(perf_counter() - started) * 1000,
                request_id=request_id,
                trace_id=context.trace_id,
                span_id=context.span_id,
                session_id=context.session_id,
                profile_id=context.profile_id,
                user_id=context.user_id,
                run_id=context.run_id,
                error=str(getattr(request.state, "request_observability_error", "")) if status_code >= 500 else "",
                extra={
                    "source": "api_middleware",
                    "path": request.url.path,
                    **response_error_metadata,
                },
            )
            reset_telemetry(token)

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "skills": registry.names(),
            "runtime": {
                "entry_skill": GENERAL_CHAT_SKILL_ID,
                "skills": sorted(
                    key
                    for key in orchestrator.runtime_registry.enabled_bundles().keys()
                ),
            },
            "llm": {
                "provider": llm_config.provider,
                "model": llm_config.model,
                "enabled": llm_config.enabled,
                "api_key_env": llm_config.api_key_env,
                "route_suggestions": {
                    "monitor_every_turn": bool(
                        getattr(llm_config.route_suggestions, "monitor_every_turn", False)
                    ),
                },
            },
            "security": {
                "aliyun_available": moderation_service.cloud.available,
                "aliyun_failure_reason": moderation_service.cloud.failure_reason,
                "lexicon_version": moderation_service.local.lexicon.version,
                "lexicon_file_count": moderation_service.local.lexicon.file_count,
                "quarantine_available": quarantine_store.available,
            },
            "storage": {"backend": storage.backend, "ready": storage.ready()},
            "deployment": {"environment": deployment_environment(), "version": release_version(), "node": node_name()},
        }

    @app.get("/health/live")
    def health_live() -> dict:
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready():
        redis_ready = app.state.turn_coordinator.ready()
        audit_ready = storage.backend != "postgres" or audit_store is not None
        ready = storage.ready() and (storage.backend != "postgres" or redis_ready) and audit_ready
        limiter_ready = app.state.llm_rate_limiter.ready()
        ready = ready and limiter_ready
        payload = {
            "status": "ok" if ready else "not_ready",
            "postgres": storage.ready(),
            "redis": redis_ready,
            "storage_backend": storage.backend,
            "llm_enabled": llm_config.enabled,
            "audit_encryption": audit_store is not None,
            "llm_rate_limiter": limiter_ready,
            "environment": deployment_environment(),
            "version": release_version(),
            "node": node_name(),
        }
        return JSONResponse(status_code=200 if ready else 503, content=payload)

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        payload = prometheus_payload()
        if payload is None:
            return PlainTextResponse("prometheus-client is not installed", status_code=503)
        return PlainTextResponse(payload.decode("utf-8"), media_type="text/plain; version=0.0.4")

    app.include_router(build_chat_router(repository, orchestrator, fact_service, storage.user_metadata_repository), prefix="/api/v1")
    app.include_router(
        build_chat_stream_router(repository, fact_service, orchestrator, app.state.turn_coordinator, app.state.llm_rate_limiter), prefix="/api/v1"
    )
    app.include_router(
        build_external_chat_router(repository, fact_service, orchestrator, app.state.turn_coordinator, app.state.llm_rate_limiter),
        prefix="/api/v1",
    )
    app.include_router(build_facts_router(repository, fact_service), prefix="/api/v1")
    app.include_router(build_profiles_router(fact_service, storage.user_metadata_repository), prefix="/api/v1")
    app.include_router(build_admin_assets_router(), prefix="/api/v1")
    app.include_router(build_skill_analytics_router(orchestrator), prefix="/api/v1")
    app.include_router(
        build_security_quarantine_router(quarantine_store),
        prefix="/api/v1",
    )
    return app


app = create_app()
