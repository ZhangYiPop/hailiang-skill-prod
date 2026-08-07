from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from hailiang_skills.skill_runtime.asset_lookup import lookup_assets
from hailiang_skills.skill_runtime.mcp_client import call_streamable_http_tool
from hailiang_skills.skill_runtime.models import (
    RoutingDecision,
    SessionState,
    SkillBundle,
    ToolCallRequest,
    ToolCallResult,
    ToolCapability,
    ToolRegistry,
    ToolSpec,
)
from hailiang_skills.skill_runtime.runtime_logger import RuntimeLogger, preview_text
from hailiang_skills.skill_runtime.state_tracker import ensure_runtime_state, merge_status_track_updates, snapshot_for_skill

MAX_RAG_RESULTS = 3
MAX_RAG_SNIPPET_CHARS = 1_200
MAX_WEB_SEARCH_RESULTS = 5
DEFAULT_WEB_SEARCH_REQUEST_SIZE = 10
STATUS_TRACK_TOOL_NAME = "status_track"
RAG_TOOL_NAME = "rag"
WEB_SEARCH_TOOL_NAME = "web_search"
MCP_TOOL_NAME = "mcp"
USER_PROFILE_TOOL_NAME = "user_profile"
SUBJECT_REQUIREMENTS_TOOL_NAME = "subject_requirements"
TOOL_ORDER = (
    WEB_SEARCH_TOOL_NAME,
    SUBJECT_REQUIREMENTS_TOOL_NAME,
    RAG_TOOL_NAME,
    STATUS_TRACK_TOOL_NAME,
    MCP_TOOL_NAME,
    USER_PROFILE_TOOL_NAME,
)

DEFAULT_TOOL_REGISTRY_SETTINGS: dict[str, Any] = {
    "tools": {
        WEB_SEARCH_TOOL_NAME: {
            "enabled": False,
            "description": "受控外部网页搜索：仅在本地资产不足时调用外部搜索，并要求明确披露外部来源。",
            "max_results": MAX_WEB_SEARCH_RESULTS,
            "provider": "http_json",
            "http": {
                "base_url": "",
                "method": "POST",
                "api_key_env": "WEB_SEARCH_API_KEY",
                "headers": {},
                "request_template": {
                    "scope": "webpage",
                    "includeSummary": False,
                    "size": DEFAULT_WEB_SEARCH_REQUEST_SIZE,
                    "includeRawContent": False,
                    "conciseSnippet": False,
                },
                "request_query_field": "query",
                "response_items_path": "webpages",
                "field_mapping": {
                    "title": "title",
                    "link": "link",
                    "snippet": "snippet",
                    "position": "position",
                    "score": "score",
                },
                "timeout_s": 30,
            },
        },
        RAG_TOOL_NAME: {
            "enabled": True,
            "description": "受控本地检索增强：仅检索已加载的 skill references 与仓库级 assets/generated 资产。",
        },
        SUBJECT_REQUIREMENTS_TOOL_NAME: {
            "enabled": True,
            "description": "选科要求结构化检索：按专业、职业目标或选科组合查询本 skill 的专业选科要求资产。",
        },
        STATUS_TRACK_TOOL_NAME: {
            "enabled": True,
            "description": "技能状态跟踪：读取并更新当前对话阶段与已采集信息。",
        },
        MCP_TOOL_NAME: {
            "enabled": True,
            "description": "通用 MCP 工具桥接：仅允许调用 runtime 配置白名单中的 MCP 服务与工具。",
            "servers": [
                {
                    "name": "metaso",
                    "label": "Metaso MCP",
                    "enabled": True,
                    "transport": "streamable_http",
                    "server_url": "https://metaso.cn/api/mcp",
                    "api_key_env": "METASO_API_KEY",
                    "timeout_s": 30,
                    "initialize_first": True,
                    "allowed_tools": [
                        {
                            "name": "metaso_web_search",
                            "description": "根据关键词搜索网页、文档、论文、图片、视频、播客等内容。",
                            "allowed_argument_keys": [
                                "q",
                                "scope",
                                "includeSummary",
                                "includeRawContent",
                                "size",
                            ],
                        }
                    ],
                }
            ],
        },
        USER_PROFILE_TOOL_NAME: {
            "enabled": False,
            "description": "预留给用户画像持久化能力，当前版本未启用。",
        },
    }
}


@dataclass(slots=True, frozen=True)
class LocalRagResult:
    title: str
    source: str
    score: int
    snippet: str


@dataclass(slots=True, frozen=True)
class WebSearchResult:
    title: str
    link: str
    snippet: str
    position: int
    score: str


@dataclass(slots=True, frozen=True)
class WebSearchExecution:
    results: tuple[WebSearchResult, ...] = ()
    error: str = ""


def default_tool_registry(config: dict[str, Any] | None = None) -> ToolRegistry:
    settings = _merge_tool_registry_settings(config)
    capabilities: list[ToolCapability] = []
    tool_settings = settings.get("tools", {})
    for tool_name in TOOL_ORDER:
        item = tool_settings.get(tool_name, {})
        enabled = bool(item.get("enabled", False))
        if tool_name == WEB_SEARCH_TOOL_NAME:
            enabled = enabled and _is_web_search_http_config_available(settings)
        if tool_name == MCP_TOOL_NAME:
            enabled = enabled and bool(_get_available_mcp_servers_from_settings(settings))
        description = str(item.get("description") or _default_tool_description(tool_name)).strip()
        capabilities.append(
            ToolCapability(
                name=tool_name,
                description=description,
                enabled=enabled,
            )
        )
    return ToolRegistry(
        capabilities=tuple(capabilities),
        settings=settings,
    )


def build_tool_specs(
    bundle: SkillBundle,
    state: SessionState,
    *,
    routing_decision: RoutingDecision | None = None,
    logger: RuntimeLogger | None = None,
) -> tuple[ToolSpec, ...]:
    asset_result = lookup_assets(bundle, state)
    specs: list[ToolSpec] = []
    rag_capable = _has_capability(bundle, RAG_TOOL_NAME)
    rag_enabled = (
        rag_capable
        and asset_result.tool_calling_allowed
        and (bool(asset_result.matched_assets) or asset_result.recommended_tool == RAG_TOOL_NAME)
    )
    if rag_capable:
        specs.append(
            ToolSpec(
                name=RAG_TOOL_NAME,
                description="检索当前 skill references 与 assets/generated 本地资产，用于回答资产内问题。",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "需要检索的查询词；为空时默认使用当前用户问题。",
                        }
                    },
                    "additionalProperties": False,
                },
                enabled=rag_enabled,
            )
        )
    web_search_capable = _has_capability(bundle, WEB_SEARCH_TOOL_NAME)
    web_search_enabled = (
        web_search_capable
        and asset_result.tool_calling_allowed
        and (asset_result.web_search_allowed or _routing_allows_web_search(routing_decision))
    )
    if web_search_capable:
        provider_name = str(bundle.tool_registry.settings.get("tools", {}).get(WEB_SEARCH_TOOL_NAME, {}).get("provider") or "unknown")
        specs.append(
            ToolSpec(
                name=WEB_SEARCH_TOOL_NAME,
                description=(
                    "调用受控外部网页搜索；仅在本地资产不足且允许外查时使用。"
                    f" 当前提供方: {provider_name}。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "需要搜索的网页查询语句；为空时默认使用当前用户问题。",
                        }
                    },
                    "additionalProperties": False,
                },
                enabled=web_search_enabled,
            )
        )
    subject_requirements_capable = _has_capability(bundle, SUBJECT_REQUIREMENTS_TOOL_NAME)
    subject_requirements_enabled = subject_requirements_capable and bool(_find_subject_requirements_script(bundle))
    if subject_requirements_capable:
        specs.append(
            ToolSpec(
                name=SUBJECT_REQUIREMENTS_TOOL_NAME,
                description=(
                    "查询选科参谋的结构化专业选科要求资产；当用户询问专业/职业对应选科要求，"
                    "或某个选科组合能覆盖哪些专业时使用。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "用户关于专业、职业或选科组合的问题；为空时默认使用当前用户问题。",
                        },
                        "major": {
                            "type": "string",
                            "description": "可选，明确专业或专业类，例如 计算机类、临床医学、法学。",
                        },
                        "career": {
                            "type": "string",
                            "description": "可选，职业目标，例如 医生、程序员、律师、警察。",
                        },
                        "subjects": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选，用户已选或候选科目组合，例如 ['物理', '化学']。",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多返回的记录或专业类数量，默认 12。",
                        },
                    },
                    "additionalProperties": False,
                },
                enabled=subject_requirements_enabled,
            )
        )
    status_track_capable = _has_capability(bundle, STATUS_TRACK_TOOL_NAME)
    status_track_enabled = status_track_capable and bool(_find_status_track_script(bundle))
    if status_track_capable:
        specs.append(
            ToolSpec(
                name=STATUS_TRACK_TOOL_NAME,
                description="调用 skill 自带的 status_track 脚本，返回新的阶段和采集信息。",
                parameters_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                enabled=status_track_enabled,
            )
        )
    mcp_capable = _has_capability(bundle, MCP_TOOL_NAME)
    mcp_enabled = mcp_capable and bool(_get_available_mcp_servers(bundle))
    if mcp_capable:
        server_names = [item["name"] for item in _get_available_mcp_servers(bundle)]
        specs.append(
            ToolSpec(
                name=MCP_TOOL_NAME,
                description=(
                    "调用受控 MCP 工具桥接；只能访问 runtime 白名单中的 server/tool。"
                    f" 当前可用 servers: {', '.join(server_names) if server_names else '(none)'}。"
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "server_name": {"type": "string", "description": "要调用的 MCP server 名称。"},
                        "tool_name": {"type": "string", "description": "目标 MCP tool 名称。"},
                        "arguments": {"type": "object", "description": "透传给 MCP tool 的参数对象。"},
                    },
                    "required": ["server_name", "tool_name"],
                    "additionalProperties": False,
                },
                enabled=mcp_enabled,
            )
        )
    specs.append(
        ToolSpec(
            name=USER_PROFILE_TOOL_NAME,
            description="预留 user_profile 工具位，当前未启用。",
            parameters_schema={"type": "object", "properties": {}, "additionalProperties": True},
            enabled=False,
        )
    )
    if logger:
        logger.log(
            "tool.auth.computed",
            enabled_tools=[item.name for item in specs if item.enabled],
            candidate_domains=list(asset_result.candidate_domains),
            matched_assets=asset_result.matched_assets,
            asset_lookup_web_search_allowed=asset_result.web_search_allowed,
            routing_allow_web_search=_routing_allows_web_search(routing_decision),
            web_search_provider=provider_name if web_search_capable else "",
            mcp_servers=[item["name"] for item in _get_available_mcp_servers(bundle)],
            recommended_tool=asset_result.recommended_tool,
            tool_calling_allowed=asset_result.tool_calling_allowed,
            unsupported_reason=asset_result.unsupported_reason,
        )
    return tuple(specs)


def execute_tool_call(
    bundle: SkillBundle,
    state: SessionState,
    call: ToolCallRequest,
    *,
    routing_decision: RoutingDecision | None = None,
    logger: RuntimeLogger | None = None,
) -> ToolCallResult:
    if logger:
        logger.log("tool.execute.start", tool_name=call.name, call_id=call.id, arguments=call.arguments)
    spec_map = {
        spec.name: spec for spec in build_tool_specs(bundle, state, routing_decision=routing_decision, logger=logger)
    }
    spec = spec_map.get(call.name)
    if spec is None:
        result = ToolCallResult(
            id=call.id,
            name=call.name,
            ok=False,
            content="",
            error=f"工具 {call.name} 不存在。",
        )
        _log_tool_result(logger, result)
        return result
    if not spec.enabled:
        result = ToolCallResult(
            id=call.id,
            name=call.name,
            ok=False,
            content="",
            error=f"工具 {call.name} 当前未启用。",
        )
        _log_tool_result(logger, result)
        return result

    if call.name == RAG_TOOL_NAME:
        results = run_local_rag(bundle, state, query_override=_string_arg(call.arguments, "query"))
        if not results:
            result = ToolCallResult(id=call.id, name=call.name, ok=True, content="未命中本地资产结果。")
            _log_tool_result(logger, result)
            return result
        content_lines = []
        sources = []
        for index, result in enumerate(results, start=1):
            content_lines.append(
                f"result {index}\nsource={result.source}\nscore={result.score}\ntitle={result.title}\n{result.snippet}"
            )
            sources.append(result.source)
        tool_result = ToolCallResult(
            id=call.id,
            name=call.name,
            ok=True,
            content="\n\n".join(content_lines),
            sources=tuple(sources),
        )
        _log_tool_result(logger, tool_result)
        return tool_result

    if call.name == WEB_SEARCH_TOOL_NAME:
        execution = run_web_search(bundle, state, query_override=_string_arg(call.arguments, "query"))
        if execution.error:
            result = ToolCallResult(id=call.id, name=call.name, ok=False, content="", error=execution.error)
            _log_tool_result(logger, result)
            return result
        content_lines = []
        sources = []
        for index, result in enumerate(execution.results, start=1):
            content_lines.append(
                f"result {index}\ntitle={result.title}\nlink={result.link}\nposition={result.position}\nscore={result.score}\nsnippet={result.snippet or '(none)'}"
            )
            sources.append(result.link)
        tool_result = ToolCallResult(
            id=call.id,
            name=call.name,
            ok=True,
            content="\n\n".join(content_lines),
            sources=tuple(sources),
        )
        _log_tool_result(logger, tool_result)
        return tool_result

    if call.name == SUBJECT_REQUIREMENTS_TOOL_NAME:
        try:
            response = run_subject_requirements_lookup(bundle, state, call.arguments, logger=logger)
        except Exception as exc:  # noqa: BLE001
            result = ToolCallResult(id=call.id, name=call.name, ok=False, content="", error=str(exc))
            _log_tool_result(logger, result)
            return result
        tool_result = ToolCallResult(
            id=call.id,
            name=call.name,
            ok=True,
            content=json.dumps(response, ensure_ascii=False),
            sources=("subject_selection/subject_requirements.json",),
        )
        _log_tool_result(logger, tool_result)
        return tool_result

    if call.name == MCP_TOOL_NAME:
        mcp_result = run_mcp_tool(
            bundle,
            server_name=_string_arg(call.arguments, "server_name"),
            tool_name=_string_arg(call.arguments, "tool_name"),
            arguments=_dict_arg(call.arguments, "arguments"),
        )
        if mcp_result.error:
            result = ToolCallResult(id=call.id, name=call.name, ok=False, content="", error=mcp_result.error)
            _log_tool_result(logger, result)
            return result
        tool_result = ToolCallResult(
            id=call.id,
            name=call.name,
            ok=True,
            content=_format_mcp_payload(mcp_result.payload),
            sources=_extract_mcp_sources(mcp_result.payload),
        )
        _log_tool_result(logger, tool_result)
        return tool_result

    if call.name == STATUS_TRACK_TOOL_NAME:
        try:
            response = run_status_track(bundle, state, logger=logger)
        except Exception as exc:  # noqa: BLE001
            result = ToolCallResult(id=call.id, name=call.name, ok=False, content="", error=str(exc))
            _log_tool_result(logger, result)
            return result
        tool_result = ToolCallResult(
            id=call.id,
            name=call.name,
            ok=True,
            content=json.dumps(response, ensure_ascii=False),
        )
        _log_tool_result(logger, tool_result)
        return tool_result

    result = ToolCallResult(
        id=call.id,
        name=call.name,
        ok=False,
        content="",
        error=f"工具 {call.name} 暂未实现执行器。",
    )
    _log_tool_result(logger, result)
    return result


def make_tool_call_request(name: str, arguments: dict[str, Any] | None = None, *, call_id: str | None = None) -> ToolCallRequest:
    return ToolCallRequest(id=call_id or f"call_{uuid4().hex[:8]}", name=name, arguments=arguments or {})


def run_local_rag(bundle: SkillBundle, state: SessionState, *, query_override: str | None = None) -> list[LocalRagResult]:
    if not _is_capability_enabled(bundle, RAG_TOOL_NAME):
        return []

    query = (query_override or _latest_user_message(state)).strip()
    terms = _query_terms(query)
    if not terms:
        return []

    results: list[LocalRagResult] = []
    for title, source, content in _iter_local_documents(bundle):
        score = _score_document(title=title, content=content, terms=terms)
        if score <= 0:
            continue
        results.append(
            LocalRagResult(
                title=title,
                source=source,
                score=score,
                snippet=_build_snippet(content, terms),
            )
        )

    return sorted(results, key=lambda item: (-item.score, item.source, item.title))[:MAX_RAG_RESULTS]


def run_web_search(bundle: SkillBundle, state: SessionState, *, query_override: str | None = None) -> WebSearchExecution:
    if not _is_capability_enabled(bundle, WEB_SEARCH_TOOL_NAME):
        return WebSearchExecution(error="web_search 未启用，或没有可用的公司搜索 API 配置。")

    query = (query_override or _latest_user_message(state)).strip()
    if not query:
        return WebSearchExecution(error="缺少可用于搜索的用户问题。")

    http_config = _get_web_search_http_config(bundle)
    if not http_config:
        return WebSearchExecution(error="web_search 未启用，或没有可用的公司搜索 API 配置。")
    return _run_http_json_web_search(query, http_config)


def run_subject_requirements_lookup(
    bundle: SkillBundle,
    state: SessionState,
    arguments: dict[str, Any] | None = None,
    *,
    logger: RuntimeLogger | None = None,
) -> dict[str, Any]:
    script_path = _find_subject_requirements_script(bundle)
    if not script_path:
        raise FileNotFoundError("未找到 subject_requirements_lookup.py 脚本。")

    payload = build_subject_requirements_payload(state, arguments or {})
    if logger:
        logger.log("subject_requirements.run.start", script_path=str(script_path), payload=payload)
    result = subprocess.run(
        [sys.executable, str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
        cwd=str(script_path.parent),
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        message = f"subject_requirements_lookup.py 执行失败: returncode={result.returncode}"
        if stderr:
            message += f"; stderr={preview_text(stderr, limit=600)}"
        elif stdout:
            message += f"; stdout={preview_text(stdout, limit=600)}"
        if logger:
            logger.log(
                "subject_requirements.run.failed",
                returncode=result.returncode,
                stderr=stderr,
                stdout_preview=preview_text(stdout, limit=600),
            )
        raise RuntimeError(message)
    try:
        response = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"subject_requirements_lookup.py 输出不是合法 JSON: {exc.msg}") from exc
    if not isinstance(response, dict):
        raise ValueError("subject_requirements_lookup.py 输出顶层必须是 JSON 对象")
    if logger:
        logger.log(
            "subject_requirements.run.completed",
            stdout_preview=preview_text(result.stdout, limit=800),
            stderr_preview=preview_text(result.stderr, limit=400),
        )
    return response


def build_subject_requirements_payload(state: SessionState, arguments: dict[str, Any]) -> dict[str, Any]:
    query = _string_arg(arguments, "query").strip() or _latest_user_message(state)
    payload: dict[str, Any] = {
        "query": query,
        "major": _string_arg(arguments, "major"),
        "career": _string_arg(arguments, "career"),
        "limit": _int_arg(arguments, "limit", 12),
    }
    subjects = arguments.get("subjects")
    if isinstance(subjects, list):
        payload["subjects"] = [str(item).strip() for item in subjects if str(item).strip()]
    elif isinstance(subjects, str):
        payload["subjects"] = subjects
    return payload


@dataclass(slots=True, frozen=True)
class MCPToolExecution:
    payload: dict[str, Any] | None = None
    error: str = ""


def run_mcp_tool(
    bundle: SkillBundle,
    *,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> MCPToolExecution:
    if not _is_capability_enabled(bundle, MCP_TOOL_NAME):
        return MCPToolExecution(error="mcp 未启用，或没有可用的 MCP server 配置。")
    if not server_name.strip() or not tool_name.strip():
        return MCPToolExecution(error="mcp 调用缺少 server_name 或 tool_name。")

    server_config = _find_mcp_server(bundle, server_name)
    if server_config is None:
        return MCPToolExecution(error=f"MCP server {server_name} 不存在或当前不可用。")
    allowed_tool = _find_mcp_allowed_tool(server_config, tool_name)
    if allowed_tool is None:
        return MCPToolExecution(error=f"MCP tool {tool_name} 不在 server {server_name} 的白名单中。")
    if not _mcp_arguments_allowed(allowed_tool, arguments):
        return MCPToolExecution(error=f"MCP tool {tool_name} 的参数不符合白名单限制。")

    transport = str(server_config.get("transport") or "streamable_http").strip()
    if transport != "streamable_http":
        return MCPToolExecution(error=f"MCP server {server_name} 的 transport {transport} 当前未实现。")

    result = call_streamable_http_tool(server_config, tool_name=tool_name, arguments=arguments)
    if result.error:
        return MCPToolExecution(error=result.error)
    return MCPToolExecution(payload=result.payload)


def run_status_track(
    bundle: SkillBundle,
    state: SessionState,
    *,
    logger: RuntimeLogger | None = None,
) -> dict[str, Any]:
    script_path = _find_status_track_script(bundle)
    if not script_path:
        raise FileNotFoundError("未找到 status_track.py 脚本。")

    ensure_runtime_state(state, bundle)
    payload = build_status_track_payload(bundle, state)
    if logger:
        logger.log("status_track.run.start", script_path=str(script_path), payload=payload)
    result = subprocess.run(
        [sys.executable, str(script_path)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
        cwd=str(script_path.parent),
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        message = f"status_track.py 执行失败: returncode={result.returncode}"
        if stderr:
            message += f"; stderr={preview_text(stderr, limit=600)}"
        elif stdout:
            message += f"; stdout={preview_text(stdout, limit=600)}"
        if logger:
            logger.log(
                "status_track.run.failed",
                returncode=result.returncode,
                stderr=stderr,
                stdout_preview=preview_text(stdout, limit=600),
            )
        raise RuntimeError(message)
    if logger:
        logger.log(
            "status_track.run.completed",
            stdout_preview=preview_text(result.stdout, limit=600),
            stderr_preview=preview_text(result.stderr, limit=400),
        )
    response = parse_status_track_response(result.stdout)
    merge_status_track_updates(bundle, state, response)
    return response


def build_status_track_payload(bundle: SkillBundle, state: SessionState) -> dict[str, Any]:
    ensure_runtime_state(state, bundle)
    messages_payload = [{"role": item.role, "content": item.content} for item in state.messages]
    latest_user_message = next((item.content for item in reversed(state.messages) if item.role == "user"), "")
    latest_assistant_message = next((item.content for item in reversed(state.messages) if item.role == "assistant"), "")
    collected_info = dict(state.collected_info)
    snapshot = snapshot_for_skill(bundle, state)
    return {
        "session_id": state.session_id,
        "stage": state.stage,
        "collected_info": collected_info,
        "info": collected_info,
        "messages": messages_payload,
        "latest_user_message": latest_user_message,
        "latest_assistant_message": latest_assistant_message,
        "skill": {
            "name": bundle.metadata.get("name") or bundle.root_name,
            "skill_id": bundle.metadata.get("skill_id", ""),
            "source": bundle.source,
        },
        "active_skill_id": snapshot["active_skill_id"],
        "contract": {
            "skill_id": bundle.contract.skill_id,
            "skill_role": bundle.contract.skill_role,
            "stages": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "required_facts": list(item.required_facts),
                    "enable_intent_check": item.enable_intent_check,
                    "enable_satisfaction_check": item.enable_satisfaction_check,
                }
                for item in bundle.contract.stages
            ],
            "routes": [
                {
                    "scene": item.scene,
                    "target_skill_id": item.target_skill_id,
                    "required_global_facts": list(item.required_global_facts),
                    "required_skill_facts": list(item.required_skill_facts),
                }
                for item in bundle.contract.routes
            ],
            "accepts_scenes": list(bundle.contract.accepts_scenes),
        },
        "global_facts": snapshot["global_facts"],
        "skill_facts_for_current_skill": snapshot["skill_facts_for_current_skill"],
        "stage_facts_for_current_skill": snapshot["stage_facts_for_current_skill"],
        "status_flags": snapshot["status_flags"],
        "route_candidates": [item.scene for item in bundle.contract.routes],
    }


def parse_status_track_response(raw_output: str) -> dict[str, Any]:
    try:
        data = json.loads(raw_output or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"status_track.py 输出不是合法 JSON: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise ValueError("status_track.py 输出顶层必须是 JSON 对象")

    stage = data.get("stage")
    if stage is not None and not isinstance(stage, str):
        raise ValueError("status_track.py 输出的 stage 必须是字符串")

    collected_info = data.get("collected_info", data.get("info", {}))
    if collected_info is None:
        collected_info = {}
    if not isinstance(collected_info, dict):
        raise ValueError("status_track.py 输出的 collected_info/info 必须是对象")

    global_facts_patch = data.get("global_facts_patch", {})
    if global_facts_patch is None:
        global_facts_patch = {}
    if not isinstance(global_facts_patch, dict):
        raise ValueError("status_track.py 输出的 global_facts_patch 必须是对象")

    skill_facts_patch = data.get("skill_facts_patch", {})
    if skill_facts_patch is None:
        skill_facts_patch = {}
    if not isinstance(skill_facts_patch, dict):
        raise ValueError("status_track.py 输出的 skill_facts_patch 必须是对象")

    stage_facts_patch = data.get("stage_facts_patch", {})
    if stage_facts_patch is None:
        stage_facts_patch = {}
    if not isinstance(stage_facts_patch, dict):
        raise ValueError("status_track.py 输出的 stage_facts_patch 必须是对象")

    status_flags_patch = data.get("status_flags_patch", {})
    if status_flags_patch is None:
        status_flags_patch = {}
    if not isinstance(status_flags_patch, dict):
        raise ValueError("status_track.py 输出的 status_flags_patch 必须是对象")

    route_signal = data.get("route_signal", {})
    if route_signal is None:
        route_signal = {}
    if not isinstance(route_signal, dict):
        raise ValueError("status_track.py 输出的 route_signal 必须是对象")

    return {
        "stage": stage or "init",
        "collected_info": collected_info,
        "global_facts_patch": global_facts_patch,
        "skill_facts_patch": skill_facts_patch,
        "stage_facts_patch": stage_facts_patch,
        "status_flags_patch": status_flags_patch,
        "route_signal": route_signal,
    }


def _is_capability_enabled(bundle: SkillBundle, name: str) -> bool:
    return any(item.name == name and item.enabled for item in bundle.tool_registry.capabilities)


def _has_capability(bundle: SkillBundle, name: str) -> bool:
    return any(item.name == name for item in bundle.tool_registry.capabilities)


def _routing_allows_web_search(routing_decision: RoutingDecision | None) -> bool:
    return bool(routing_decision and routing_decision.allow_web_search)


def _merge_tool_registry_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = json.loads(json.dumps(DEFAULT_TOOL_REGISTRY_SETTINGS, ensure_ascii=False))
    payload = config if isinstance(config, dict) else {}
    custom_tools = payload.get("tools", {})
    if not isinstance(custom_tools, dict):
        return settings
    merged_tools = settings["tools"]
    for tool_name, custom_tool in custom_tools.items():
        if not isinstance(custom_tool, dict):
            continue
        existing = merged_tools.get(tool_name, {})
        merged = dict(existing)
        for key, value in custom_tool.items():
            if key == "http" and isinstance(value, dict):
                merged_http = dict(existing.get("http", {})) if isinstance(existing.get("http"), dict) else {}
                merged_http.update(value)
                merged["http"] = merged_http
            elif key == "servers" and isinstance(value, list):
                merged["servers"] = [item for item in value if isinstance(item, dict)]
            else:
                merged[key] = value
        merged_tools[tool_name] = merged
    mcp_settings = merged_tools.get(MCP_TOOL_NAME, {})
    if isinstance(mcp_settings, dict):
        servers = mcp_settings.get("servers", [])
        if isinstance(servers, list):
            mcp_settings["servers"] = sorted(
                [item for item in servers if isinstance(item, dict)],
                key=lambda item: (int(item.get("priority", 100)), str(item.get("name") or "")),
            )
    return settings


def _default_tool_description(tool_name: str) -> str:
    return str(
        DEFAULT_TOOL_REGISTRY_SETTINGS["tools"].get(tool_name, {}).get("description") or f"{tool_name} tool"
    )


def _is_web_search_http_config_available(settings: dict[str, Any]) -> bool:
    tools_payload = settings.get("tools", {})
    if not isinstance(tools_payload, dict):
        return False
    web_search = tools_payload.get(WEB_SEARCH_TOOL_NAME, {})
    if not isinstance(web_search, dict) or not bool(web_search.get("enabled", False)):
        return False
    http_config = web_search.get("http", {})
    if not isinstance(http_config, dict):
        return False
    provider = str(web_search.get("provider") or "").strip()
    base_url = str(http_config.get("base_url") or "").strip()
    if provider != "http_json" or not base_url:
        return False
    api_key_env = str(http_config.get("api_key_env") or "").strip()
    return not api_key_env or bool(os.getenv(api_key_env, "").strip())


def _get_web_search_http_config(bundle: SkillBundle) -> dict[str, Any]:
    settings = bundle.tool_registry.settings
    tools_payload = settings.get("tools", {})
    if not isinstance(tools_payload, dict):
        return {}
    web_search = tools_payload.get(WEB_SEARCH_TOOL_NAME, {})
    if not isinstance(web_search, dict):
        return {}
    provider = str(web_search.get("provider") or "").strip()
    http_config = web_search.get("http", {})
    if provider != "http_json" or not isinstance(http_config, dict):
        return {}
    return http_config


def _get_available_mcp_servers(bundle: SkillBundle) -> list[dict[str, Any]]:
    return _get_available_mcp_servers_from_settings(bundle.tool_registry.settings)


def _get_available_mcp_servers_from_settings(settings: dict[str, Any]) -> list[dict[str, Any]]:
    tools_payload = settings.get("tools", {})
    if not isinstance(tools_payload, dict):
        return []
    mcp = tools_payload.get(MCP_TOOL_NAME, {})
    if not isinstance(mcp, dict) or not bool(mcp.get("enabled", False)):
        return []
    servers = mcp.get("servers", [])
    if not isinstance(servers, list):
        return []
    available: list[dict[str, Any]] = []
    for item in servers:
        if not isinstance(item, dict) or not bool(item.get("enabled", False)):
            continue
        api_key_env = str(item.get("api_key_env") or "").strip()
        if api_key_env and not os.getenv(api_key_env, "").strip():
            continue
        available.append(item)
    return available


def _run_http_json_web_search(query: str, http_config: dict[str, Any]) -> WebSearchExecution:
    base_url = str(http_config.get("base_url") or "").strip()
    method = str(http_config.get("method") or "POST").strip().upper()
    timeout_s = int(http_config.get("timeout_s") or 30)
    api_key_env = str(http_config.get("api_key_env") or "").strip()
    api_key = os.getenv(api_key_env, "").strip() if api_key_env else ""
    request_template = http_config.get("request_template", {})
    request_template = request_template if isinstance(request_template, dict) else {}
    request_query_field = str(http_config.get("request_query_field") or "query").strip() or "query"
    response_items_path = str(http_config.get("response_items_path") or "").strip() or "webpages"
    field_mapping = http_config.get("field_mapping", {})
    field_mapping = field_mapping if isinstance(field_mapping, dict) else {}
    label = str(http_config.get("label") or "公司搜索 API").strip()
    max_results = int(http_config.get("max_results") or _tool_max_results_from_http_config(http_config) or MAX_WEB_SEARCH_RESULTS)
    if not base_url:
        return WebSearchExecution(error=f"{label} 缺少 base_url。")
    if api_key_env and not api_key:
        return WebSearchExecution(error=f"{label} 缺少环境变量 {api_key_env}。")

    request_payload = dict(request_template)
    request_payload[request_query_field] = query
    headers = _build_http_headers(http_config, api_key)
    request = _build_http_json_request(base_url, method=method, payload=request_payload, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace").strip()
        return WebSearchExecution(error=f"{label} 搜索失败: HTTP {exc.code}: {error_body or 'empty body'}")
    except urllib.error.URLError as exc:
        return WebSearchExecution(error=f"{label} 搜索失败: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        return WebSearchExecution(error=f"{label} 搜索失败: {exc}")

    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        return WebSearchExecution(error=f"{label} 返回不是合法 JSON: {exc.msg}")
    items = _resolve_path(data, response_items_path)
    if not isinstance(items, list):
        return WebSearchExecution(error=f"{label} 返回中缺少列表路径 {response_items_path}。")
    results = _map_http_json_results(items, field_mapping, max_results=max_results)
    if not results:
        return WebSearchExecution(error=f"{label} 未返回可用的网页结果。")
    return WebSearchExecution(results=tuple(results))


def _build_http_headers(http_config: dict[str, Any], api_key: str) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    custom_headers = http_config.get("headers", {})
    if isinstance(custom_headers, dict):
        for key, value in custom_headers.items():
            if str(key).strip() and str(value).strip():
                headers[str(key).strip()] = str(value).strip()
    if api_key:
        headers.setdefault("Authorization", f"Bearer {api_key}")
    return headers


def _build_http_json_request(
    base_url: str,
    *,
    method: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> urllib.request.Request:
    if method == "GET":
        from urllib import parse as urllib_parse

        query_string = urllib_parse.urlencode(payload, doseq=True)
        separator = "&" if "?" in base_url else "?"
        url = f"{base_url}{separator}{query_string}" if query_string else base_url
        return urllib.request.Request(url, method="GET", headers=headers)
    request_body = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(base_url, data=request_body, method=method, headers=headers)


def _tool_max_results_from_http_config(http_config: dict[str, Any]) -> int:
    request_template = http_config.get("request_template", {})
    if not isinstance(request_template, dict):
        return MAX_WEB_SEARCH_RESULTS
    size = request_template.get("size", MAX_WEB_SEARCH_RESULTS)
    try:
        return min(int(size), MAX_WEB_SEARCH_RESULTS)
    except (TypeError, ValueError):
        return MAX_WEB_SEARCH_RESULTS


def _resolve_path(payload: Any, path: str) -> Any:
    current = payload
    for part in [item for item in path.split(".") if item]:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _map_http_json_results(
    items: list[Any],
    field_mapping: dict[str, Any],
    *,
    max_results: int,
) -> list[WebSearchResult]:
    title_key = str(field_mapping.get("title") or "title")
    link_key = str(field_mapping.get("link") or "link")
    snippet_key = str(field_mapping.get("snippet") or "snippet")
    position_key = str(field_mapping.get("position") or "position")
    score_key = str(field_mapping.get("score") or "score")
    results: list[WebSearchResult] = []
    for item in items[:max_results]:
        if not isinstance(item, dict):
            continue
        title = str(item.get(title_key) or "").strip()
        link = str(item.get(link_key) or "").strip()
        snippet = str(item.get(snippet_key) or "").strip()
        if not title or not link:
            continue
        try:
            position = int(item.get(position_key) or len(results) + 1)
        except (TypeError, ValueError):
            position = len(results) + 1
        results.append(
            WebSearchResult(
                title=title,
                link=link,
                snippet=snippet,
                position=position,
                score=str(item.get(score_key) or ""),
            )
        )
    return results


def _find_mcp_server(bundle: SkillBundle, server_name: str) -> dict[str, Any] | None:
    normalized = server_name.strip()
    for item in _get_available_mcp_servers(bundle):
        if str(item.get("name") or "").strip() == normalized:
            return item
    return None


def _find_mcp_allowed_tool(server_config: dict[str, Any], tool_name: str) -> dict[str, Any] | None:
    allowed_tools = server_config.get("allowed_tools", [])
    if not isinstance(allowed_tools, list):
        return None
    normalized = tool_name.strip()
    for item in allowed_tools:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip() == normalized:
            return item
    return None


def _mcp_arguments_allowed(allowed_tool: dict[str, Any], arguments: dict[str, Any]) -> bool:
    allowed_keys = allowed_tool.get("allowed_argument_keys")
    if not isinstance(allowed_keys, list):
        return True
    allowed = {str(item).strip() for item in allowed_keys if str(item).strip()}
    return set(arguments).issubset(allowed)


def _extract_mcp_webpages(result_payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    candidates: list[Any] = [result_payload]
    structured_content = result_payload.get("structuredContent")
    if structured_content is not None:
        candidates.append(structured_content)
    content = result_payload.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("json"), dict):
                candidates.append(item["json"])
            if isinstance(item.get("data"), dict):
                candidates.append(item["data"])
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                try:
                    parsed_text = json.loads(text)
                except json.JSONDecodeError:
                    continue
                candidates.append(parsed_text)

    for candidate in candidates:
        webpages = _extract_webpages_from_candidate(candidate)
        if webpages is not None:
            return webpages
    return None


def _extract_webpages_from_candidate(candidate: Any) -> list[dict[str, Any]] | None:
    if isinstance(candidate, dict):
        webpages = candidate.get("webpages")
        if isinstance(webpages, list):
            return webpages
        search_result = candidate.get("searchResult")
        if isinstance(search_result, dict):
            nested = search_result.get("webpages")
            if isinstance(nested, list):
                return nested
    return None


def _format_mcp_payload(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return "(empty)"
    webpages = _extract_mcp_webpages(payload)
    if isinstance(webpages, list) and webpages:
        lines: list[str] = []
        for index, item in enumerate(webpages[:MAX_WEB_SEARCH_RESULTS], start=1):
            if not isinstance(item, dict):
                continue
            lines.append(
                f"result {index}\n"
                f"title={str(item.get('title') or '').strip()}\n"
                f"link={str(item.get('link') or item.get('url') or '').strip()}\n"
                f"position={str(item.get('position') or index)}\n"
                f"score={str(item.get('score') or '')}\n"
                f"snippet={str(item.get('snippet') or item.get('summary') or '').strip() or '(none)'}"
            )
        return "\n\n".join(lines) if lines else json.dumps(payload, ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False)


def _extract_mcp_sources(payload: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ()
    webpages = _extract_mcp_webpages(payload)
    if not isinstance(webpages, list):
        return ()
    sources: list[str] = []
    for item in webpages[:MAX_WEB_SEARCH_RESULTS]:
        if not isinstance(item, dict):
            continue
        link = str(item.get("link") or item.get("url") or "").strip()
        if link:
            sources.append(link)
    return tuple(sources)


def _log_tool_result(logger: RuntimeLogger | None, result: ToolCallResult) -> None:
    if not logger:
        return
    logger.log(
        "tool.execute.result",
        tool_name=result.name,
        call_id=result.id,
        ok=result.ok,
        error=result.error,
        sources=list(result.sources),
        content_preview=preview_text(result.content, limit=800),
    )


def _find_status_track_script(bundle: SkillBundle):
    return bundle.scripts.get("script/status_track.py") or bundle.scripts.get("scripts/status_track.py")


def _find_subject_requirements_script(bundle: SkillBundle):
    return bundle.scripts.get("script/subject_requirements_lookup.py") or bundle.scripts.get(
        "scripts/subject_requirements_lookup.py"
    )


def _string_arg(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key, "")
    return value if isinstance(value, str) else ""


def _int_arg(arguments: dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(arguments.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _dict_arg(arguments: dict[str, Any], key: str) -> dict[str, Any]:
    value = arguments.get(key, {})
    return dict(value) if isinstance(value, dict) else {}


def _latest_user_message(state: SessionState) -> str:
    return next((item.content for item in reversed(state.messages) if item.role == "user"), "")


def _iter_local_documents(bundle: SkillBundle):
    for path, content in bundle.references.items():
        yield path, f"reference:{path}", content

    for path, content in bundle.local_assets.items():
        yield path, f"local_asset:{path}", content

    selected_domains = bundle.runtime_metadata.assets.generated_domains or tuple(sorted(bundle.asset_domains))
    for domain_name in selected_domains:
        domain = bundle.asset_domains.get(domain_name)
        if domain is None:
            continue
        for asset_file in domain.files:
            if asset_file.file_name == "asset_manifest.json":
                continue
            content = json.dumps(asset_file.payload, ensure_ascii=False, indent=2)
            title = f"{domain_name}/{asset_file.file_name}"
            yield title, f"asset:{asset_file.relative_path}", content


def _query_terms(query: str) -> list[str]:
    normalized = query.lower()
    terms = set(re.findall(r"[a-z0-9_]{2,}", normalized))

    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        terms.add(chunk)
        for size in (2, 3, 4):
            for index in range(0, max(len(chunk) - size + 1, 0)):
                terms.add(chunk[index : index + size])

    return sorted(terms, key=lambda item: (-len(item), item))


def _score_document(*, title: str, content: str, terms: list[str]) -> int:
    haystack = content.lower()
    title_haystack = title.lower()
    score = 0
    for term in terms:
        content_hits = haystack.count(term)
        title_hits = title_haystack.count(term)
        if not content_hits and not title_hits:
            continue
        score += content_hits * max(len(term), 2)
        score += title_hits * max(len(term), 2) * 4
    return score


def _build_snippet(content: str, terms: list[str]) -> str:
    normalized = content.lower()
    first_hit = min((normalized.find(term) for term in terms if normalized.find(term) >= 0), default=0)
    start = max(first_hit - 240, 0)
    end = min(start + MAX_RAG_SNIPPET_CHARS, len(content))
    snippet = content[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(content):
        snippet += "..."
    return snippet
