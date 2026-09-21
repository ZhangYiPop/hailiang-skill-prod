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
from hailiang_skills.api.routes.workbench import build_deployment_router, build_workbench_router
from hailiang_skills.api.routes.chat import build_chat_router
from hailiang_skills.api.routes.chat_stream import build_chat_stream_router
from hailiang_skills.api.routes.external_chat import build_external_chat_router
from hailiang_skills.api.routes.token_usage import build_token_usage_router
from hailiang_skills.api.routes.diagnostics import build_diagnostics_router
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
from hailiang_skills.storage.repositories.profile_memory_repo import (
    InMemoryConversationMemoryRepository,
    InMemoryProfileMemoryRepository,
    PostgresConversationMemoryRepository,
    PostgresProfileMemoryRepository,
)
from hailiang_skills.workbench.factory import build_workbench_service
from hailiang_skills.workbench.catalog import load_current_release_entries, resolve_default_expert_id
from hailiang_skills.runtime_bridge.default_expert_team import default_expert_team_id
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
    "EXPERT_NOT_FOUND": "专家不存在或当前不可用。",
    "EXPERT_TEAM_NOT_FOUND": "专家团不存在或当前不可用。",
    "EXPERT_NOT_IN_ACTIVE_TEAM": "当前专家不属于已进入的专家团。",
    "EXPERT_TEAM_NOT_ACTIVE": "当前会话尚未进入专家团。",
    "TEAM_HANDOFF_NOT_ACTIVE": "该专家转交建议已失效。",
    "TEAM_HANDOFF_TARGET_NOT_ALLOWED": "该专家不在本次可转交范围内。",
    "SKILL_ENTRY_BLOCKED_IN_EXPERT_TEAM": "专家团内不能直接进入单个 Skill。",
    "DIALOGUE_LAST_MESSAGE_MUST_BE_USER": "dialogue 最后一条消息必须是 user。",
    "ACTIVE_RUN_MUST_STOP": "当前回答仍在生成，请先停止并等待完成后再切换孩子。",
    "INPUT_PROFILE_ID_FORBIDDEN": "SSE v2 不接受 input.profile_id；请仅使用 context_data.profile_id。",
    "LEGACY_EXPERT_FIELDS_FORBIDDEN": "请将专家团和专家状态放入 expert_context，不能使用顶层旧字段。",
    "EXPERT_CONTEXT_REQUIRED": "非停止操作必须提供 expert_context。",
    "EXPERT_CONTEXT_FIELDS_REQUIRED": "expert_context 必须同时提供 expert_team_id、expert_id 和 operation。",
    "EXPERT_CONTEXT_STALE": "专家上下文已更新，请使用服务端返回的最新状态继续。",
    "EXPERT_CONTEXT_OPERATION_INVALID": "expert_context.operation 与当前操作不匹配。",
    "CONTEXT_ACTIVATION_REQUIRED": "当前请求会切换孩子上下文；请使用 context_activation=auto，或先完成当前操作。",
    "CONFIGURATION_UPDATED": "专家团配置已更新，旧交互已失效，请刷新后重试。",
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
    # ``BaseHTTPMiddleware.call_next`` wraps downstream JSON responses in a
    # streaming response on recent Starlette versions.  That wrapper no longer
    # exposes ``body``, so retain the error code in a response header as a
    # durable, content-free diagnostic fallback.
    headers = getattr(response, "headers", {})
    header_code = headers.get("X-Hailiang-Error-Code") if hasattr(headers, "get") else None
    metadata: dict[str, object] = {}
    if isinstance(header_code, str) and header_code:
        metadata["response_error_code"] = header_code
    raw_body = getattr(response, "body", None)
    if not isinstance(raw_body, (bytes, bytearray)) or not raw_body:
        return metadata
    try:
        payload = json.loads(raw_body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return metadata
    if not isinstance(payload, dict):
        return metadata

    for key in ("code", "message", "detail"):
        if key in payload:
            metadata[f"response_error_{key}"] = payload[key]
    return metadata


def _request_error_metadata(request: Request) -> dict[str, object]:
    """Read the structured pre-stream error saved by an exception handler."""
    payload = getattr(request.state, "hailiang_error_payload", None)
    if not isinstance(payload, dict):
        return {}
    return {
        f"response_error_{key}": payload[key]
        for key in ("code", "message", "detail", "error", "upstream_detail")
        if key in payload
    }


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
    profile_memory_repository = (
        PostgresProfileMemoryRepository(storage.session_factory)
        if storage.session_factory is not None
        else InMemoryProfileMemoryRepository()
    )
    conversation_memory_repository = (
        PostgresConversationMemoryRepository(storage.session_factory)
        if storage.session_factory is not None
        else InMemoryConversationMemoryRepository()
    )

    app = FastAPI(title="hailiang-skills")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        payload = _http_error_payload(
            exc.detail,
            default_code="REQUEST_VALIDATION_ERROR" if exc.status_code == 422 else "HTTP_ERROR",
        )
        request.state.hailiang_error_payload = payload
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers={**(exc.headers or {}), "X-Hailiang-Error-Code": str(payload["code"])},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        payload = _http_error_payload(exc.errors(), default_code="REQUEST_VALIDATION_ERROR")
        request.state.hailiang_error_payload = payload
        return JSONResponse(
            status_code=422,
            content=payload,
            headers={"X-Hailiang-Error-Code": str(payload["code"])},
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
    # ``auto`` is the normal deployment mode: an active production
    # deployment is authoritative, while a fresh server falls back to the
    # complete bundled filesystem runtime until the first deployment is
    # activated.  The explicit values remain available for migration,
    # comparison and emergency rollback.
    business_config_source = os.getenv("HAILIANG_BUSINESS_CONFIG_SOURCE", "auto").strip().lower()
    if business_config_source not in {"auto", "filesystem", "database"}:
        raise RuntimeError("HAILIANG_BUSINESS_CONFIG_SOURCE 仅支持 auto、filesystem 或 database")
    # Workbench owns the local SQLite session factory in development, so it
    # must exist before a database-only runtime catalog can be read.
    workbench_service = build_workbench_service(storage, orchestrator=None)
    source_uses_database = business_config_source in {"auto", "database"}
    database_entries = load_current_release_entries(workbench_service.session_factory) if source_uses_database else None
    active_snapshot = None
    effective_business_config_source = business_config_source
    effective_business_config_entries = None
    default_expert_id = None
    if source_uses_database:
        active_snapshot = workbench_service.active_deployment_snapshot(deployment_environment())
        if active_snapshot is None:
            # A fresh server may have an empty database, or only staged/current
            # workbench objects without a production deployment.  Keep the
            # complete bundled runtime as the bootstrap source until an
            # immutable expert-team deployment is activated; never merge a
            # partial database catalog with filesystem objects.
            effective_business_config_source = "filesystem_fallback"
        else:
            # The filesystem coordinator is a compatibility default only.  A
            # database deployment is authoritative for its expert-team
            # coordinator and may use a different expert ID.
            active_entries = active_snapshot.get("entries", []) if isinstance(active_snapshot, dict) else []
            # The immutable deployment ZIP is the source of truth once an
            # active deployment exists.  Do not expose unrelated current DB
            # releases (or a newer staged object) to routing.  Old rows from
            # before package materialisation may not contain payload/files;
            # retain the DB-catalog fallback only for that legacy case so a
            # valid active deployment can still boot during migration.
            materialized_active_entries = [
                entry
                for entry in active_entries
                if isinstance(entry, dict)
                and isinstance(entry.get("payload"), dict)
                and isinstance(entry.get("dependency_locks"), list)
            ] if isinstance(active_entries, list) else []
            if materialized_active_entries and len(materialized_active_entries) == len(active_entries):
                effective_business_config_entries = materialized_active_entries
            else:
                effective_business_config_entries = database_entries
            root = active_snapshot.get("root", {}) if isinstance(active_snapshot, dict) else {}
            preferred_team_id = str(root.get("object_key") or "") if isinstance(root, dict) and root.get("object_type") == "expert_team" else ""
            if not preferred_team_id:
                preferred_team_id = default_expert_team_id()
            default_expert_id = resolve_default_expert_id(
                effective_business_config_entries or [],
                preferred_team_id=preferred_team_id,
            )
    orchestrator = MainPlannerOrchestrator(
        registry,
        llm_config,
        moderation_service=moderation_service,
        business_config_entries=effective_business_config_entries,
        default_expert_id=default_expert_id,
        profile_memory_repository=profile_memory_repository,
        conversation_memory_repository=conversation_memory_repository,
    )
    workbench_service.orchestrator = orchestrator
    app.state.workbench_service = workbench_service
    app.state.business_config_source = effective_business_config_source
    if effective_business_config_source in {"filesystem", "filesystem_fallback"} and os.getenv("HAILIANG_WORKBENCH_BOOTSTRAP", "false").lower() in {"1", "true", "yes", "on"}:
        workbench_service.bootstrap_from_runtime()
    if source_uses_database and active_snapshot is not None:
        workbench_service.install_runtime_catalog()
        workbench_service.install_active_deployment_runtime(deployment_environment())
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
    async def session_version_conflict(request: Request, exc: SessionVersionConflict) -> JSONResponse:
        payload = {
            **_http_error_payload(
                "session changed concurrently; reload context and retry",
                default_code="SESSION_UPDATE_CONFLICT",
            ),
            "error": str(exc),
        }
        request.state.hailiang_error_payload = payload
        return JSONResponse(
            status_code=409,
            content=payload,
            headers={"X-Hailiang-Error-Code": str(payload["code"])},
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
                response_error_metadata = {
                    **_response_error_metadata(response),
                    **_request_error_metadata(request),
                }
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
        expert_runtime = orchestrator.expert_runtime.health()
        runtime_ready = bool(expert_runtime["available"] and expert_runtime["expert_loaded"] and orchestrator.ms_agent_probe.available)
        return {
            "status": "ok" if runtime_ready else "degraded",
            "skills": registry.names(),
            "runtime": {
                "entry_expert": expert_runtime["default_expert_id"],
                "entry_skill": GENERAL_CHAT_SKILL_ID,
                "expert_runtime": expert_runtime,
                "ms_agent_available": orchestrator.ms_agent_probe.available,
                "skills": sorted(
                    key
                    for key in orchestrator.runtime_registry.enabled_bundles().keys()
                ),
                "filesystem_fallback_skills": list(
                    getattr(orchestrator, "runtime_catalog_fallback_skills", ())
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
            "business_config_source": app.state.business_config_source,
            "deployment": {"environment": deployment_environment(), "version": release_version(), "node": node_name()},
            "workbench": {
                "kernel_fingerprint": workbench_service.kernel_fingerprint,
                "object_count": len(workbench_service.list_objects(include_archived=True)),
            },
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
        expert_runtime = orchestrator.expert_runtime.health()
        runtime_ready = bool(expert_runtime["available"] and expert_runtime["expert_loaded"] and orchestrator.ms_agent_probe.available)
        ready = ready and limiter_ready and runtime_ready
        payload = {
            "status": "ok" if ready else "not_ready",
            "postgres": storage.ready(),
            "redis": redis_ready,
            "storage_backend": storage.backend,
            "llm_enabled": llm_config.enabled,
            "audit_encryption": audit_store is not None,
            "llm_rate_limiter": limiter_ready,
            "expert_runtime": expert_runtime,
            "ms_agent_available": orchestrator.ms_agent_probe.available,
            "filesystem_fallback_skills": list(
                getattr(orchestrator, "runtime_catalog_fallback_skills", ())
            ),
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

    app.include_router(build_chat_router(
        repository,
        orchestrator,
        fact_service,
        storage.user_metadata_repository,
        configuration_snapshot_resolver=lambda: workbench_service.active_deployment_snapshot(deployment_environment()),
    ), prefix="/api/v1")
    app.include_router(
        build_chat_stream_router(
            repository,
            fact_service,
            orchestrator,
            app.state.turn_coordinator,
            app.state.llm_rate_limiter,
            configuration_snapshot_resolver=lambda: workbench_service.active_deployment_snapshot(deployment_environment()),
            debug_configuration_snapshot_resolver=workbench_service.debug_configuration_snapshot,
        ),
        prefix="/api/v2",
    )
    app.include_router(
        build_external_chat_router(repository, fact_service, orchestrator, app.state.turn_coordinator, app.state.llm_rate_limiter),
        prefix="/api/v1",
    )
    app.include_router(build_facts_router(repository, fact_service), prefix="/api/v1")
    app.include_router(build_profiles_router(fact_service, storage.user_metadata_repository), prefix="/api/v1")
    app.include_router(build_admin_assets_router(), prefix="/api/v1")
    app.include_router(build_skill_analytics_router(orchestrator), prefix="/api/v1")
    app.include_router(build_workbench_router(workbench_service), prefix="/workbench/v1")
    app.include_router(build_deployment_router(workbench_service), prefix="/deployment/v1")
    app.include_router(build_token_usage_router(storage.engine), prefix="/deployment/v1")
    app.include_router(build_diagnostics_router(repository, storage.engine), prefix="/api/v1")
    app.include_router(
        build_security_quarantine_router(quarantine_store),
        prefix="/api/v1",
    )
    return app


app = create_app()
