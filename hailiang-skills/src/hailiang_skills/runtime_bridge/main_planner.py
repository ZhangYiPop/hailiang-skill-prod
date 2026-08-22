from __future__ import annotations

import hashlib
import inspect
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from hailiang_skills.core.loop_defense import LoopDefense
from hailiang_skills.core.logging import make_event
from hailiang_skills.core.scenario_engine import ScenarioEngine
from hailiang_skills.core.session_logging import append_session_events
from hailiang_skills.core.telemetry import current_telemetry
from hailiang_skills.core.skill_ids import (
    CAREER_PLAN_SKILL_ID,
    EXPERT_DIRECT_EXECUTION_ID,
    GENERAL_CHAT_SKILL_ID,
    LEGACY_MAIN_PLANNER_SKILL_ID,
    canonical_skill_id,
)
from hailiang_skills.core.fact_prompt_builder import build_missing_fact_form_block
from hailiang_skills.core.skill_display import build_runtime_skill_catalog, build_skill_display
from hailiang_skills.core.status_labels import (
    normalize_ms_agent_progress_label,
    normalize_status_label,
    redact_skill_file_names,
)
from hailiang_skills.runtime_bridge.facts import (
    RUNTIME_STATE_KEY,
    runtime_state_payload,
    sync_context_to_runtime_state,
    sync_runtime_state_to_context,
)
from hailiang_skills.runtime_bridge.native_questionnaire import (
    attach_staged_questionnaire_form,
    consume_pending_questionnaire_answer,
    decode_questionnaire_reply,
    deterministic_questionnaire_fallback,
    flush_deferred_questionnaire_promotions,
    questionnaire_continuation_context,
    questionnaire_enabled,
    questionnaire_reply_is_valid,
    resolve_questionnaire_continuation,
    stage_questionnaire_form,
)
from hailiang_skills.runtime_bridge.native_path_options import resolve_native_path_options
from hailiang_skills.runtime_bridge.agentscope_expert_runtime import (
    AGENT_RUNTIME_STATE_KEY,
    AgentScopeExpertRuntime,
    DEFAULT_EXPERT_ID,
)
from hailiang_skills.runtime_bridge.expert_bundle import load_local_expert_registry
from hailiang_skills.runtime_bridge.expert_team_bundle import load_local_expert_team_registry
from hailiang_skills.runtime_bridge.imports import PROJECT_ROOT, ensure_skill_runtime_importable
from hailiang_skills.runtime_bridge.runtime_config import load_runtime_bridge_config
from hailiang_skills.skills.base import SkillResult

ensure_skill_runtime_importable()

from hailiang_skills.runtime_bridge.conversation_memory import (  # noqa: E402
    ConversationMemoryStore,
    supplement_questionnaire_evidence,
)
from hailiang_skills.runtime_bridge.ms_agent_adapter import MSAgentRuntimeAdapter  # noqa: E402
from hailiang_skills.skill_runtime.cli import (  # noqa: E402
    MAX_TOOL_CALLS_PER_TURN,
    _is_empty_tool_call,
    _sanitize_assistant_reply,
    _tool_exchange_messages,
)
from hailiang_skills.skill_runtime.intent_tracker import (  # noqa: E402
    apply_intent_update,
    looks_like_profile_slot_answer,
    looks_like_structured_fact_update,
    track_user_intent,
)
from hailiang_skills.skill_runtime.embedding_client import EmbeddingClient  # noqa: E402
from hailiang_skills.skill_runtime.intent_router import (  # noqa: E402
    CROSS_SKILL_SUGGESTION_MIN_CONFIDENCE,
    IntentRouteDecision,
    IntentRouter,
    is_short_contextual_reply,
)
from hailiang_skills.skill_runtime.llm_client import OpenAICompatibleChatClient  # noqa: E402
from hailiang_skills.skill_runtime.models import (  # noqa: E402
    ChatMessage,
    LLMConfig,
    PromptAssembly,
    RoutingDecision,
    RouteTarget,
    SessionState,
    ToolCallResult,
    ToolRoutingCandidate,
    ToolSpec,
)
from hailiang_skills.skill_runtime.profile_matrix import (  # noqa: E402
    ProfileMatrixRecommendation,
    recommend_scenes_from_profile_matrix,
)
from hailiang_skills.skill_runtime.runtime_logger import RuntimeLogger, default_log_file  # noqa: E402
from hailiang_skills.skill_runtime.runtime_router import (  # noqa: E402
    build_ms_agent_tool_routing_context,
    classify_tool_routing,
    parse_ms_agent_tool_routing,
)
from hailiang_skills.skill_runtime.session import run_status_hook_if_present  # noqa: E402
from hailiang_skills.skill_runtime.session import build_prompt_assembly  # noqa: E402
from hailiang_skills.skill_runtime.skill_registry import SkillRegistry as RuntimeSkillRegistry  # noqa: E402
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry  # noqa: E402
from hailiang_skills.skill_runtime.state_tracker import ensure_runtime_state, mark_route_interruption  # noqa: E402
from hailiang_skills.skill_runtime.tools import build_status_track_payload, build_tool_specs, execute_tool_call  # noqa: E402

class _PlanningMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _IncrementalAssistantMessageExtractor:
    """Decode a top-level JSON string while the planner response is streaming."""

    _KEY_PATTERN = re.compile(r'"assistant_message"\s*:\s*"')
    _DEPENDENCY_PATTERNS = (
        re.compile(r'"required_scripts"\s*:\s*(\[[^\]]*\])'),
        re.compile(r'"required_references"\s*:\s*(\[[^\]]*\])'),
        re.compile(r'"required_resources"\s*:\s*(\[[^\]]*\])'),
        re.compile(r'"required_packages"\s*:\s*(\[[^\]]*\])'),
    )
    _TOOL_ROUTING_PATTERN = re.compile(r'"tool_routing"\s*:\s*')

    def __init__(self, *, require_tool_routing_gate: bool = False) -> None:
        self._buffer = ""
        self._value_start: int | None = None
        self._decoded = ""
        self._require_tool_routing_gate = require_tool_routing_gate
        self.complete = False

    def feed(self, chunk: str) -> str:
        if not chunk or self.complete:
            return ""
        self._buffer += chunk
        if self._value_start is None:
            match = self._KEY_PATTERN.search(self._buffer)
            if match is None:
                return ""
            # A response that needs any local dependency is not final until the
            # selected context has been loaded and fed to the response model.
            for pattern in self._DEPENDENCY_PATTERNS:
                dependency_match = pattern.search(self._buffer[: match.start()])
                if dependency_match is None:
                    continue
                try:
                    if json.loads(dependency_match.group(1)):
                        return ""
                except (TypeError, ValueError):
                    return ""
            if self._require_tool_routing_gate:
                routing_payload = self._extract_tool_routing(self._buffer[: match.start()])
                routing_decision = parse_ms_agent_tool_routing(routing_payload)
                if routing_decision is None or routing_decision.required:
                    return ""
            self._value_start = match.end()

        safe_end = self._safe_json_string_end(self._value_start)
        raw_value = self._buffer[self._value_start : safe_end]
        try:
            decoded = json.loads(f'"{raw_value}"')
        except (TypeError, ValueError):
            return ""
        delta = decoded[len(self._decoded) :]
        self._decoded = decoded
        return delta

    def _safe_json_string_end(self, start: int) -> int:
        index = start
        safe_end = start
        while index < len(self._buffer):
            char = self._buffer[index]
            if char == '"':
                self.complete = True
                return index
            if char != "\\":
                index += 1
                safe_end = index
                continue
            if index + 1 >= len(self._buffer):
                break
            escape = self._buffer[index + 1]
            if escape == "u":
                if index + 6 > len(self._buffer):
                    break
                digits = self._buffer[index + 2 : index + 6]
                if not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                    break
                index += 6
            else:
                index += 2
            safe_end = index
        return safe_end

    def _extract_tool_routing(self, prefix: str) -> dict[str, Any] | None:
        match = self._TOOL_ROUTING_PATTERN.search(prefix)
        if match is None:
            return None
        start = match.end()
        while start < len(prefix) and prefix[start].isspace():
            start += 1
        if start >= len(prefix) or prefix[start] != "{":
            return None
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(prefix)):
            char = prefix[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        payload = json.loads(prefix[start : index + 1])
                    except (TypeError, ValueError):
                        return None
                    return payload if isinstance(payload, dict) else None
        return None


class _QuestionnaireContinuationExtractor:
    """Expose questionnaire prose as soon as its JSON string starts arriving."""

    _MESSAGE_PATTERN = re.compile(r'"assistant_message"\s*:\s*"')

    def __init__(self, allowed_question_ids: set[str]) -> None:
        del allowed_question_ids
        self._buffer = ""
        self._value_start: int | None = None
        self._decoded = ""
        self.complete = False

    def feed(self, chunk: str) -> str:
        if not chunk or self.complete:
            return ""
        self._buffer += chunk
        if self._value_start is None:
            message_match = self._MESSAGE_PATTERN.search(self._buffer)
            if message_match is None:
                return ""
            self._value_start = message_match.end()

        safe_end = self._safe_json_string_end(self._value_start)
        raw_value = self._buffer[self._value_start : safe_end]
        try:
            decoded = json.loads(f'"{raw_value}"')
        except (TypeError, ValueError):
            return ""
        delta = decoded[len(self._decoded) :]
        self._decoded = decoded
        return delta

    def _safe_json_string_end(self, start: int) -> int:
        index = start
        safe_end = start
        while index < len(self._buffer):
            char = self._buffer[index]
            if char == '"':
                self.complete = True
                return index
            if char != "\\":
                index += 1
                safe_end = index
                continue
            if index + 1 >= len(self._buffer):
                break
            if self._buffer[index + 1] == "u":
                if index + 6 > len(self._buffer):
                    break
                if not re.fullmatch(r"[0-9a-fA-F]{4}", self._buffer[index + 2 : index + 6]):
                    break
                index += 6
            else:
                index += 2
            safe_end = index
        return safe_end


class _RuntimePlannerLLM:
    planner_name = "hailiang_runtime_llm"

    def __init__(
        self,
        client: OpenAICompatibleChatClient,
        logger: RuntimeLogger | None = None,
        skill_dir: Path | None = None,
        stream_reply_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        tool_routing_context: str = "",
        require_tool_routing_gate: bool = False,
    ) -> None:
        self.client = client
        self.logger = logger
        self.skill_dir = Path(skill_dir) if skill_dir else None
        self.last_error: str | None = None
        self.last_raw_response: str | None = None
        self.last_combined_response: str = ""
        self.stream_reply_callback = stream_reply_callback
        self.cancel_check = cancel_check
        self.tool_routing_context = tool_routing_context
        self.require_tool_routing_gate = require_tool_routing_gate
        self.streamed_combined_response = False
        self.last_tool_routing_payload: dict[str, Any] | None = None
        self.first_visible_delta_ms: int | None = None

    def generate(self, messages: list[Any]) -> _PlanningMessage:
        prompt = str(messages[-1].content if messages and hasattr(messages[-1], "content") else messages[-1])
        enhanced_prompt = (
            "你是 ms-agent SkillAnalyzer 与正文生成合并执行器。\n"
            "请在同一次模型调用中完成三件事：判断本轮需要按需加载哪些 "
            "references/scripts/resources，判断是否需要工具，再根据当前 Skill 和上下文生成最终用户回复。\n"
            "必须只返回 JSON 对象，并严格按以下字段顺序输出：can_handle、required_scripts、"
            "required_references、required_resources、required_packages、tool_routing、assistant_message、"
            "plan_summary_short、plan_summary、steps、parameters、reasoning、questionnaire_response。\n"
            "所有依赖选择字段和 tool_routing 必须出现在 assistant_message 之前；"
            "详细规划字段必须放在 assistant_message 之后，以便正文尽早开始流式输出。\n"
            "plan_summary_short 必须是最多 12 个中文字符，并以‘正在’开头，用来展示当前执行动作。\n"
            "steps[].action 会直接作为用户看到的 intent.label：必须是最多 12 个中文字符，并以‘正在’开头，用‘正在+动宾短语’总结当前思考步骤，不要输出句号、编号或内部文件名。\n"
            "如果没有明确需要加载的文件，对应数组返回 []。\n\n"
            "tool_routing 必须是对象："
            '{"required":false,"candidates":[],"allow_web_search":false,'
            '"candidate_domains":[],"query_focus":"","reason":""}。'
            "candidates 每项必须是 "
            '{"kind":"tool|script","name":"web_search|rag|subject_requirements|status_track|mcp|script",'
            '"intent_label":"5-10个中文字符","reason":"..."}。'
            "kind=script 时 name 只能是 script；required 必须与 candidates 是否非空一致；"
            "allow_web_search 必须与是否包含 web_search 候选一致。\n"
            "需要任何 reference、resource、package、script 或工具时 assistant_message 返回空字符串，"
            "等待服务端加载或执行后再生成正文；完全不需要依赖和工具时才直接生成完整 assistant_message。\n\n"
            "assistant_message 必须是面向用户的最终正文，不要包含内部规划、JSON 或文件名。\n"
            "如果当前 Skill 启用了 Native Questionnaire Protocol，额外返回 questionnaire_response 对象，"
            "其内容必须严格遵循该协议；普通回答时 questionnaire_response 返回 null。\n\n"
            "# MS-Agent 原始规划 prompt\n"
            f"{prompt}"
        )
        reference_catalog = self._reference_catalog()
        if reference_catalog:
            enhanced_prompt += f"\n\n# Available Local Reference Paths\n{reference_catalog}"
        if self.tool_routing_context:
            enhanced_prompt += f"\n\n# Tool Routing Policy And Catalog\n{self.tool_routing_context}"
        try:
            request_messages = [
                ChatMessage(role="system", content="You are a strict JSON planner for MS-Agent skill loading."),
                ChatMessage(role="user", content=enhanced_prompt),
            ]
            content = self._complete_planner_response(request_messages)
            self.last_error = None
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            content = (
                '{"can_handle":true,"plan_summary":"fallback empty lazy load plan",'
                '"steps":[{"step":1,"action":"load SKILL.md only","type":"reference"}],'
                '"required_scripts":[],"required_references":[],"required_resources":[],'
                '"required_packages":[],"parameters":{},"reasoning":"planner LLM unavailable; fallback used"}'
            )
        self.last_raw_response = content
        self.last_combined_response = self._extract_combined_response(content)
        payload = _try_parse_json(content) or _extract_json_object(content)
        self.last_tool_routing_payload = (
            payload.get("tool_routing")
            if isinstance(payload, dict) and isinstance(payload.get("tool_routing"), dict)
            else None
        )
        return _PlanningMessage(_normalize_runtime_planner_response(content))

    def _complete_planner_response(self, messages: list[ChatMessage]) -> str:
        stream_complete = getattr(self.client, "stream_complete", None)
        if not callable(stream_complete):
            complete_kwargs: dict[str, Any] = {"logger": self.logger}
            try:
                if "request_purpose" in inspect.signature(self.client.complete).parameters:
                    complete_kwargs["request_purpose"] = "main_combined_response"
            except (TypeError, ValueError):
                pass
            return self.client.complete(messages, **complete_kwargs)

        extractor = _IncrementalAssistantMessageExtractor(
            require_tool_routing_gate=self.require_tool_routing_gate
        )
        started = time.perf_counter()
        content_parts: list[str] = []
        stream_kwargs: dict[str, Any] = {"logger": self.logger}
        try:
            supports_cancel = "cancel_check" in inspect.signature(stream_complete).parameters
            if "request_purpose" in inspect.signature(stream_complete).parameters:
                stream_kwargs["request_purpose"] = "main_combined_response"
        except (TypeError, ValueError):
            supports_cancel = False
        if supports_cancel and self.cancel_check is not None:
            stream_kwargs["cancel_check"] = self.cancel_check
        for chunk in stream_complete(messages, **stream_kwargs):
            content_delta = str(getattr(chunk, "content_delta", "") or "")
            if not content_delta:
                continue
            content_parts.append(content_delta)
            visible_delta = extractor.feed(content_delta)
            if visible_delta and self.first_visible_delta_ms is None:
                self.first_visible_delta_ms = int((time.perf_counter() - started) * 1000)
                if self.logger:
                    self.logger.log(
                        "planner.first_visible_delta",
                        request_purpose="main_combined_response",
                        planner_to_first_text_ms=self.first_visible_delta_ms,
                    )
            if visible_delta and self.stream_reply_callback is not None:
                self.stream_reply_callback(visible_delta)
                self.streamed_combined_response = True
        content = "".join(content_parts).strip()
        if self.logger:
            self.logger.log(
                "planner.stream.completed",
                request_purpose="main_combined_response",
                duration_ms=int((time.perf_counter() - started) * 1000),
                planner_to_first_text_ms=self.first_visible_delta_ms,
            )
        if content:
            return content
        complete_kwargs = {"logger": self.logger}
        try:
            if "request_purpose" in inspect.signature(self.client.complete).parameters:
                complete_kwargs["request_purpose"] = "main_combined_response_retry"
        except (TypeError, ValueError):
            pass
        return self.client.complete(messages, **complete_kwargs)

    def _reference_catalog(self) -> str:
        if not self.skill_dir:
            return ""
        references_dir = self.skill_dir / "references"
        if not references_dir.is_dir():
            return ""
        paths: list[str] = []
        for path in sorted(item for item in references_dir.rglob("*") if item.is_file()):
            if path.name.startswith(".") or path.suffix.lower() in {".pyc", ".zip", ".db"}:
                continue
            paths.append(f"- {path.relative_to(self.skill_dir)}")
        return "\n".join(paths)

    @staticmethod
    def _extract_combined_response(value: str) -> str:
        payload = _try_parse_json(value) or _extract_json_object(value)
        if not isinstance(payload, dict):
            return ""
        questionnaire = payload.get("questionnaire_response")
        if isinstance(questionnaire, dict):
            return json.dumps(questionnaire, ensure_ascii=False)
        assistant_message = payload.get("assistant_message")
        return str(assistant_message).strip() if isinstance(assistant_message, str) else ""


def _try_parse_json(value: str) -> Any | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _normalize_runtime_planner_response(value: str) -> str:
    payload = _try_parse_json(value)
    if not isinstance(payload, dict):
        payload = _extract_json_object(value)
    if not isinstance(payload, dict):
        payload = {
            "can_handle": True,
            "plan_summary": "fallback empty lazy load plan",
            "steps": [{"step": 1, "action": "continue with SKILL.md only", "type": "code"}],
            "required_scripts": [],
            "required_references": [],
            "required_resources": [],
            "required_packages": [],
            "parameters": {},
            "reasoning": "planner response was not valid JSON; fallback used",
            "plan_summary_short": "继续规划",
        }
    normalized = {
        "can_handle": bool(payload.get("can_handle", True)),
        "plan_summary": str(payload.get("plan_summary") or payload.get("contribution") or "continue skill turn"),
        "steps": _normalize_planner_steps(payload.get("steps")),
        "required_scripts": _normalize_planner_list(payload.get("required_scripts")),
        "required_references": _normalize_planner_list(payload.get("required_references")),
        "required_resources": _normalize_planner_list(payload.get("required_resources")),
        "required_packages": _normalize_planner_list(payload.get("required_packages")),
        "parameters": payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {},
        "reasoning": str(payload.get("reasoning") or "normalized by Hailiang runtime planner adapter"),
        "plan_summary_short": str(payload.get("plan_summary_short") or "继续规划")[:10],
    }
    if not normalized["steps"]:
        normalized["steps"] = [{"step": 1, "action": "continue with SKILL.md only", "type": "code"}]
    return json.dumps(normalized, ensure_ascii=False)


def _extract_json_object(value: str) -> dict[str, Any] | None:
    text = str(value or "").strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    parsed = _try_parse_json(text[start : end + 1])
    return parsed if isinstance(parsed, dict) else None


def _normalize_planner_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _normalize_planner_steps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(value, start=1):
        if isinstance(item, dict):
            steps.append(
                {
                    "step": item.get("step") or index,
                    "action": str(item.get("action") or item.get("description") or "continue planning"),
                    "type": str(item.get("type") or "code"),
                }
            )
        elif str(item or "").strip():
            steps.append({"step": index, "action": str(item), "type": "code"})
    return steps


def _is_recoverable_ms_agent_plan_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return (
        "SkillAnalyzer did not produce an executable plan" in text
        or "can_handle=False" in text
        or "Failed to parse JSON" in text
    )


def _truncate_debug_text(value: str, *, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(truncated)"


def _compact_skill_entry_context(value: Any, *, depth: int = 0) -> Any:
    """Bound transition context so the low-latency entry prompt stays small."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate_debug_text(value, limit=600)
    if depth >= 3:
        return _truncate_debug_text(
            json.dumps(value, ensure_ascii=False, default=str),
            limit=600,
        )
    if isinstance(value, dict):
        return {
            str(key): _compact_skill_entry_context(item, depth=depth + 1)
            for key, item in list(value.items())[:16]
        }
    if isinstance(value, (list, tuple)):
        return [
            _compact_skill_entry_context(item, depth=depth + 1)
            for item in list(value)[:8]
        ]
    return _truncate_debug_text(str(value), limit=600)


def _reference_title_from_path(path: str) -> str:
    name = Path(path).name or path
    return name.rsplit(".", 1)[0].replace("_", " ").replace("-", " ").strip() or path


def _summarize_ms_agent_runtime_trace(raw_trace: dict[str, Any]) -> dict[str, Any]:
    loaded = raw_trace.get("loaded") if isinstance(raw_trace.get("loaded"), dict) else {}
    planner = raw_trace.get("planner") if isinstance(raw_trace.get("planner"), dict) else {}
    summarized_planner = {
        "name": planner.get("name", ""),
        "error": planner.get("error"),
    }
    raw_response = str(planner.get("raw_response") or "")
    if raw_response:
        summarized_planner["raw_response_preview"] = _truncate_debug_text(raw_response, limit=1200)
    return {
        "runtime": raw_trace.get("runtime"),
        "skill_key": raw_trace.get("skill_key"),
        "turn": raw_trace.get("turn"),
        "planner": summarized_planner,
        "plan": raw_trace.get("plan") if isinstance(raw_trace.get("plan"), dict) else {},
        "loaded": {
            "references": [
                {"name": item.get("name", ""), "path": item.get("path", "")}
                for item in loaded.get("references", [])
                if isinstance(item, dict)
            ],
            "scripts": [
                {"name": item.get("name", ""), "path": item.get("path", "")}
                for item in loaded.get("scripts", [])
                if isinstance(item, dict)
            ],
            "resources": [
                {"name": item.get("name", ""), "path": item.get("path", "")}
                for item in loaded.get("resources", [])
                if isinstance(item, dict)
            ],
        },
        "previous_lazy_load": raw_trace.get("previous_lazy_load") or {},
        "lazy_load_diff": raw_trace.get("lazy_load_diff") or {},
        "execution_outputs": [
            {
                "script": item.get("script") or item.get("name") or item.get("path", ""),
                "ok": item.get("ok"),
                "exit_code": item.get("exit_code"),
                "args": item.get("args") if isinstance(item.get("args"), list) else [],
                "stdin_payload": item.get("stdin_payload") if isinstance(item.get("stdin_payload"), dict) else {},
                "duration_ms": item.get("duration_ms"),
                "execution_mode": item.get("execution_mode") or "sandbox",
                "error": item.get("error", ""),
                "stdout_preview": _truncate_debug_text(str(item.get("stdout") or ""), limit=800),
                "stderr_preview": _truncate_debug_text(str(item.get("stderr") or ""), limit=800),
            }
            for item in raw_trace.get("execution_outputs", [])
            if isinstance(item, dict)
        ],
    }


def _ms_agent_loaded_reference_context(loaded_context) -> list[dict[str, str]]:
    references = getattr(loaded_context, "references", None) or []
    items: list[dict[str, str]] = []
    for reference in references:
        if not isinstance(reference, dict):
            continue
        path = str(reference.get("path") or reference.get("name") or "")
        content = str(reference.get("content") or "")
        if not path:
            continue
        items.append(
            {
                "path": path,
                "name": Path(path).name,
                "title": _reference_title_from_path(path),
                "snippet": _truncate_debug_text(content.strip(), limit=1400),
            }
        )
    return items


def _normalize_plan_summary(plan: dict[str, Any]) -> tuple[str, str]:
    short = str(plan.get("plan_summary_short") or "").strip()
    if 5 <= len(short) <= 10:
        return short, "ms_agent"
    if len(short) > 10:
        return short[:10], "ms_agent_truncated"
    summary = str(plan.get("plan_summary") or "").strip()
    if "八字" in summary or "命理" in summary:
        return "准备命理分析", "fallback_rule"
    if "选科" in summary:
        return "分析选科方案", "fallback_rule"
    if "升学" in summary:
        return "梳理升学路径", "fallback_rule"
    if "测试" in summary:
        return "验证Skill", "fallback_rule"
    return _bounded_plan_summary(_compact_status_label(summary, fallback="规划执行中")), "fallback_rule"


def _compact_plan_summary(plan: dict[str, Any]) -> str:
    return _normalize_plan_summary(plan)[0]


def _bounded_plan_summary(text: str) -> str:
    summary = str(text or "").strip()
    if len(summary) > 10:
        return summary[:10]
    if len(summary) < 5:
        return "规划执行中"
    return summary


def _compact_status_label(action: str, *, fallback: str) -> str:
    text = re.sub(r"[`*_#\[\]（）()<>《》\"'“”]", "", str(action or "")).strip()
    text = re.sub(r"\s+", "", text)
    if not text:
        return fallback
    if "缺失" in text or "补全" in text or "采集" in text:
        return "补全信息"
    if "提取" in text:
        return "提取信息"
    if "加载" in text and ("参考" in text or ".md" in text or "资料" in text):
        return "加载资料"
    if "调用" in text or "脚本" in text:
        return "调用脚本"
    if "生成" in text or "输出" in text:
        return "生成回复"
    if "分析" in text or "判断" in text:
        return "分析需求"
    # Labels cross the SSE boundary and are rendered verbatim by the client.
    # Do not cut a user-facing step summary halfway through a sentence.
    return text


TOOL_INTENT_LABELS = {
    "web_search": "查询实时资料",
    "rag": "检索本地资料",
    "subject_requirements": "查询选科要求",
    "status_track": "更新规划进度",
    "mcp": "调用外部工具",
    "script": "运行规划计算",
}


def _routing_candidate_names(decision: RoutingDecision) -> set[str]:
    return {
        item.name
        for item in decision.candidates
        if item.kind == "tool" and item.name
    }


def _authorize_requested_tool_specs(
    specs: tuple[ToolSpec, ...],
    decision: RoutingDecision,
) -> tuple[ToolSpec, ...]:
    if decision.source != "ms_agent":
        return specs
    requested = _routing_candidate_names(decision)
    return tuple(
        replace(item, enabled=bool(item.enabled and item.name in requested))
        for item in specs
    )


def _candidate_for_name(decision: RoutingDecision, name: str) -> ToolRoutingCandidate | None:
    return next(
        (
            item
            for item in decision.candidates
            if item.name == name and item.kind in {"tool", "script"}
        ),
        None,
    )


def _tool_intent_label(decision: RoutingDecision, name: str) -> str:
    candidate = _candidate_for_name(decision, name)
    proposed = normalize_status_label(candidate.intent_label if candidate else "")
    if 5 <= len(proposed) <= 10 and re.search(r"[\u4e00-\u9fff]", proposed):
        return proposed
    return TOOL_INTENT_LABELS.get(name, "调用规划工具")


PROJECT_RUNTIME_SKILLS_ROOT = PROJECT_ROOT / "runtime_skills"
PROJECT_RUNTIME_AGENTS_ROOT = PROJECT_ROOT / "runtime_agents"
PROJECT_RUNTIME_AGENT_TEAMS_ROOT = PROJECT_ROOT / "runtime_agent_teams"
MAIN_PLANNER_ID = CAREER_PLAN_SKILL_ID
GENERAL_CHAT_ID = GENERAL_CHAT_SKILL_ID
JUNIOR_MULTI_PATH_SKILL_ID = "junior_multi_path_planning"
MULTI_PATH_SKILL_IDS = {"multi_path_planning", JUNIOR_MULTI_PATH_SKILL_ID}
PRIMARY_STAGE_KEYWORDS = (
    "小学",
    "小一",
    "小二",
    "小三",
    "小四",
    "小五",
    "小六",
    "一年级",
    "二年级",
    "三年级",
    "四年级",
    "五年级",
    "六年级",
)
JUNIOR_STAGE_KEYWORDS = (
    "初中",
    "初一",
    "初二",
    "初三",
    "七年级",
    "八年级",
    "九年级",
    "中考",
)
SENIOR_STAGE_KEYWORDS = (
    "高中",
    "高一",
    "高二",
    "高三",
)
SENIOR_CONTEXT_KEYWORDS = ("选科", "本科", "特控线", "一段线", "二段线")
SUBJECT_GROUP_KEYWORDS = ("物理", "历史", "化学", "生物", "政治", "地理")
TOP_SCORE_KEYWORDS = ("上游", "学霸", "尖子", "前列", "拔尖", "重高", "优高", "重点高中", "特控线")
MID_SCORE_KEYWORDS = ("中游", "中等", "一般", "普通", "普娃", "普本", "还行")
LOW_SCORE_KEYWORDS = ("下游", "偏后", "较差", "成绩弱", "成绩差", "基础弱", "保底", "考不上", "职高")
TALENT_POSITIVE_KEYWORDS = (
    "有特长",
    "有兴趣",
    "特长",
    "美术",
    "音乐",
    "体育",
    "舞蹈",
    "编程",
    "书法",
    "乐器",
    "足球",
    "篮球",
    "竞赛",
)
TALENT_NEGATIVE_KEYWORDS = (
    "无特长",
    "没特长",
    "没有特长",
    "无兴趣",
    "没兴趣",
    "没有兴趣",
    "没什么兴趣",
    "没有明显特长",
)
PLANNING_AMBIGUITY_KEYWORDS = (
    "怎么规划",
    "如何规划",
    "怎么走",
    "走什么路",
    "适合什么",
    "有什么方向",
    "推荐什么",
    "不知道怎么选",
    "不知道该怎么",
    "拿不准",
    "有点迷糊",
    "迷茫",
)
GENERAL_CHAT_EXCLUSION_KEYWORDS = (
    "升学",
    "规划",
    "孩子",
    "年级",
    "成绩",
    "分数",
    "学校",
    "大学",
    "专业",
    "选科",
    "提分",
    "兴趣",
    "特长",
    "路径",
    "中考",
    "高考",
)
HAILIANG_TARGETS = {
    "mock_admission": {
        "skill": "admission",
        "scenario": "admission_simulation",
        "scene": "模拟升学",
        "phase": "admission_analysis",
    },
    "multi_path_planning": {
        "skill": "convergence",
        "scenario": "multi_path_planning",
        "scene": "多元路径规划",
        "phase": "match_paths",
    },
}
SCENE_HINTS = (
    (
        "多元路径规划",
        (
            "多元路径规划",
            "多元路径",
            "多元升学路径",
            "替代路径",
            "路径收敛",
            "升学路径",
            "还有什么路",
            "还有哪些路",
            "还有别的路",
            "除了普通高考",
            "除了高考",
            "别的升学路",
            "其他升学路径"
        ),
    ),
    ("模拟升学", ("模拟升学", "模拟录取", "能上什么大学", "可报学校", "学校层次", "分数能上")),
    ("提分", ("提分", "提分规划", "提升成绩", "怎么补分")),
    ("兴趣探索", ("兴趣探索", "兴趣方向", "兴趣培养")),
    ("前景探路", ("前景探路", "就业前景", "专业前景", "职业前景")),
    ("选科参谋", ("选科参谋", "选科", "科目组合")),
)
DEFAULT_OPENING_PATTERN = re.compile(
    r"默认开场：\s*[\r\n]+[\"“](.*?)[\"”]",
    re.S,
)
FIRST_VISIT_OPENING_PATTERN = re.compile(
    r"首次（无历史档案）\*\*：\s*[\"“](.*?)[\"”]",
    re.S,
)
LEADING_HELLO_PATTERN = re.compile(r"^你好[，,。.\s]*")


def _format_parent_opening(opening: str, parent_name: str | None) -> str:
    normalized_opening = " ".join(str(opening or "").split()).strip()
    normalized_name = (parent_name or "").strip()
    if not normalized_name:
        return normalized_opening
    opening_without_hello = LEADING_HELLO_PATTERN.sub("", normalized_opening).strip()
    if not opening_without_hello:
        return f"{normalized_name} 你好。"
    return f"{normalized_name} 你好，{opening_without_hello}"


def _infer_school_stage_from_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    if any(keyword in normalized for keyword in JUNIOR_STAGE_KEYWORDS):
        return "junior"
    if any(keyword in normalized for keyword in SENIOR_STAGE_KEYWORDS):
        return "senior"
    if _looks_like_senior_context(normalized):
        return "senior"
    return ""


def _infer_profile_stage_label_from_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    if any(keyword in normalized for keyword in PRIMARY_STAGE_KEYWORDS):
        return "小学"
    if any(keyword in normalized for keyword in JUNIOR_STAGE_KEYWORDS):
        return "初中"
    if any(keyword in normalized for keyword in SENIOR_STAGE_KEYWORDS):
        return "高中"
    if _looks_like_senior_context(normalized):
        return "高中"
    return ""


def _looks_like_senior_context(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if "高考" in normalized and any(keyword in normalized for keyword in SUBJECT_GROUP_KEYWORDS):
        return True
    if any(keyword in normalized for keyword in SENIOR_CONTEXT_KEYWORDS) and re.search(r"\d{3}", normalized):
        return True
    if any(keyword in normalized for keyword in SUBJECT_GROUP_KEYWORDS) and re.search(r"\d{3}", normalized):
        return True
    return False


def _extract_grade_value_from_text(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    grade_patterns = (
        "小学",
        "初中",
        "高中",
        "初一",
        "初二",
        "初三",
        "高一",
        "高二",
        "高三",
        "一年级",
        "二年级",
        "三年级",
        "四年级",
        "五年级",
        "六年级",
        "七年级",
        "八年级",
        "九年级",
    )
    for pattern in grade_patterns:
        if pattern in normalized:
            return pattern
    return ""


def _is_multi_path_scene(scene: str) -> bool:
    return scene in {"多元路径规划", "多元路径", "多元升学路径", "初中多元路径规划"}


def _looks_like_planning_request(text: str) -> bool:
    normalized = str(text or "").strip()
    return bool(normalized) and any(keyword in normalized for keyword in GENERAL_CHAT_EXCLUSION_KEYWORDS)


class MainPlannerOrchestrator:
    """Hailiang API orchestrator backed by the career/general-chat route model."""

    def __init__(self, registry, llm_config, moderation_service=None) -> None:
        self.registry = registry
        self.llm_config = llm_config
        self.moderation_service = moderation_service
        self.runtime_bridge_config = load_runtime_bridge_config()
        self.scenario_engine = ScenarioEngine()
        self.loop_defense = LoopDefense()
        self.runtime_registry = self._load_runtime_registry()
        # Expert Bundles are validated at boot. They reference this registry
        # only; no Skill from an expert package is ever loaded or installed.
        self.expert_registry = load_local_expert_registry(
            PROJECT_RUNTIME_AGENTS_ROOT,
            self.runtime_registry,
        )
        self.expert_team_registry = load_local_expert_team_registry(
            PROJECT_RUNTIME_AGENT_TEAMS_ROOT,
            self.expert_registry,
        )
        self.main_bundle = self.runtime_registry.get_raw(MAIN_PLANNER_ID)
        if self.main_bundle is None:
            raise RuntimeError("skill-runtime career_plan_entity skill is not available")
        self.runtime_client = self._build_runtime_client(llm_config)
        self.route_suggestion_monitor_every_turn = bool(
            getattr(getattr(llm_config, "route_suggestions", None), "monitor_every_turn", False)
        )
        self.route_suggestion_client = self._build_runtime_client(
            llm_config,
            {
                "enable_thinking": False,
                "return_reasoning": False,
                "temperature": 0.1,
                "max_tokens": 700,
            },
        )
        self.questionnaire_client = self._build_runtime_client(
            llm_config,
            {
                "enable_thinking": False,
                "return_reasoning": False,
                "temperature": 0.2,
                "max_tokens": 600,
            },
        )
        self.ms_agent_adapter = MSAgentRuntimeAdapter(
            runtime_dir=self.runtime_bridge_config.runtime_dir,
            sandbox_prewarm_enabled=self.runtime_bridge_config.sandbox_prewarm_enabled,
            local_fast_path_enabled=self.runtime_bridge_config.local_fast_path_enabled,
        )
        self.ms_agent_probe = self.ms_agent_adapter.runtime_probe
        self.ms_agent_runtime = self.ms_agent_adapter
        # The registry keeps ``main_planner`` as a compatibility alias, but
        # never prewarm the same bundle twice.  Duplicate sandbox startup made
        # local boot and the first request noticeably slower.
        prewarm_bundles = {
            skill_id: bundle
            for skill_id, bundle in self.runtime_registry.enabled_bundles().items()
        }
        self.sandbox_prewarm_steps = self.ms_agent_adapter.prewarm_runtime_skills(prewarm_bundles)
        self.memory_store = ConversationMemoryStore(
            runtime_dir=self.runtime_bridge_config.runtime_dir,
            enabled=self.runtime_bridge_config.memory_enabled,
            active_window_messages=self.runtime_bridge_config.active_window_messages,
        )
        self.embedding_client = self._build_embedding_client(llm_config)
        self.intent_router = IntentRouter(
            bundles=self.runtime_registry.enabled_bundles(),
            main_skill_id=MAIN_PLANNER_ID,
            embedding_client=self.embedding_client,
            llm_client=self.runtime_client,
            embedding_cache_enabled=bool(getattr(llm_config.embedding, "cache_enabled", True)),
            embedding_cache_dir=str(getattr(llm_config.embedding, "cache_dir", "") or ""),
        )
        self.expert_runtime = AgentScopeExpertRuntime(
            self.expert_registry,
            self.runtime_registry,
            team_registry=self.expert_team_registry,
            default_expert_id=DEFAULT_EXPERT_ID,
            client_factory=self._runtime_client_for_context,
            event_recorder=self._record_events,
        )

    def _load_runtime_registry(self) -> RuntimeSkillRegistry:
        project_registry = load_local_skill_registry(
            PROJECT_RUNTIME_SKILLS_ROOT,
            enabled_by_id=self.runtime_bridge_config.skill_enabled_by_id,
        )
        bundles = dict(project_registry.bundles)
        main_bundle = project_registry.get_raw(MAIN_PLANNER_ID)
        if main_bundle:
            relaxed_routes = [
                RouteTarget(
                    scene=route.scene,
                    target_skill_id=route.target_skill_id,
                    required_global_facts=(),
                    required_skill_facts=(),
                )
                for route in main_bundle.contract.routes
                if project_registry.is_enabled(route.target_skill_id)
            ]
            existing = {(route.scene, route.target_skill_id) for route in relaxed_routes}
            extra_routes = []
            for scene, target in [
                ("多元路径规划", "multi_path_planning"),
                ("多元路径", "multi_path_planning"),
                ("多元升学路径", "multi_path_planning"),
                ("初中多元路径规划", JUNIOR_MULTI_PATH_SKILL_ID),
            ]:
                if (scene, target) not in existing and project_registry.is_enabled(target):
                    extra_routes.append(
                        RouteTarget(
                            scene=scene,
                            target_skill_id=target,
                            required_global_facts=(),
                            required_skill_facts=(),
                        )
                    )
            if extra_routes:
                relaxed_routes.extend(extra_routes)
            main_bundle.contract = replace(
                main_bundle.contract,
                routes=tuple(relaxed_routes),
            )
        return RuntimeSkillRegistry(
            bundles=bundles,
            enabled_by_id=dict(project_registry.enabled_by_id),
        )

    def _build_runtime_client(self, llm_config, thinking_options: dict[str, Any] | None = None):
        if not llm_config.enabled:
            return None
        thinking_options = thinking_options or {}
        runtime_config = LLMConfig(
            provider=llm_config.provider,
            base_url=llm_config.base_url,
            model=llm_config.model,
            api_key_env=llm_config.api_key_env,
            api_key=llm_config.api_key or "",
            timeout_s=llm_config.timeout_s,
            temperature=float(thinking_options.get("temperature", llm_config.temperature)),
            max_tokens=int(thinking_options.get("max_tokens", llm_config.max_tokens)),
            enable_thinking=bool(thinking_options.get("enable_thinking", llm_config.enable_thinking)),
            return_reasoning=bool(thinking_options.get("return_reasoning", llm_config.return_reasoning)),
        )
        return OpenAICompatibleChatClient(runtime_config)

    def _build_embedding_client(self, llm_config):
        router_config = self.main_bundle.runtime_metadata.planner.intent_router
        embedding_config = getattr(llm_config, "embedding", None)
        embedding_enabled = bool(getattr(embedding_config, "enabled", False))
        if not embedding_enabled:
            return None
        api_key_env = str(getattr(embedding_config, "api_key_env", "") or llm_config.api_key_env)
        api_key = os.getenv(api_key_env, "").strip()
        if not api_key:
            return None
        return EmbeddingClient(
            base_url=str(getattr(embedding_config, "base_url", "") or llm_config.base_url),
            api_key=api_key,
            model=str(getattr(embedding_config, "model", "") or router_config.embedding_model),
            timeout_s=int(getattr(embedding_config, "timeout_s", 0) or router_config.embedding_timeout_s),
            max_batch_size=int(getattr(embedding_config, "max_batch_size", 10) or 10),
        )

    def build_opening_message(
        self,
        *,
        parent_name: str | None = None,
        profile_name: str | None = None,
    ) -> str:
        del profile_name
        for pattern in (DEFAULT_OPENING_PATTERN, FIRST_VISIT_OPENING_PATTERN):
            match = pattern.search(self.main_bundle.skill_markdown)
            if match:
                return _format_parent_opening(match.group(1), parent_name)
        return ""

    def _record_events(self, context, events: list[dict]) -> None:
        if not events:
            return
        turn_meta = {
            "turn_id": (context.session_meta or {}).get("active_turn_id"),
            "turn_index": (context.session_meta or {}).get("active_turn_index"),
        }
        enriched_events: list[dict] = []
        for event in events:
            if not isinstance(event, dict):
                enriched_events.append(event)
                continue
            payload = event.get("payload")
            if isinstance(payload, dict):
                enriched_payload = dict(payload)
                for key, value in turn_meta.items():
                    if value is not None and key not in enriched_payload:
                        enriched_payload[key] = value
                enriched_events.append({**event, "payload": enriched_payload})
            else:
                enriched_events.append(event)
        context.event_trace.extend(enriched_events)
        append_session_events(context.session_id, enriched_events)

    def _record_prompt_assembly_from_skill(self, context, skill) -> None:
        if not hasattr(skill, "get_prompt_for_llm"):
            return
        record = skill.get_prompt_for_llm()
        if not record:
            return
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
        self._record_events(context, [make_event("prompt_assembly", record)])

    def _emit_runtime_status(self, context, stage: str, label: str) -> None:
        callback = (context.session_meta or {}).get("status_callback")
        display_label = normalize_status_label(label)
        if callable(callback) and display_label:
            callback({"stage": stage, "label": display_label})

    def _emit_tool_status(
        self,
        context,
        *,
        name: str,
        label: str,
        detail: str = "",
    ) -> None:
        callback = (context.session_meta or {}).get("status_callback")
        display_label = normalize_status_label(label)
        if callable(callback) and display_label:
            callback(
                {
                    "stage": f"tool_{name}",
                    "label": display_label,
                    "detail": detail,
                    "source": "tool_runtime",
                }
            )

    def _emit_ms_agent_plan_statuses(self, context, plan: dict[str, Any]) -> None:
        steps = plan.get("steps") if isinstance(plan, dict) else []
        if not isinstance(steps, list):
            return
        runtime_state = context.skill_states.get(RUNTIME_STATE_KEY, {}) if isinstance(context.skill_states, dict) else {}
        active_skill_id = str(runtime_state.get("active_skill_id") or "") if isinstance(runtime_state, dict) else ""
        bundle = self.runtime_registry.get(active_skill_id) if active_skill_id else None
        skill_root = getattr(bundle, "root_dir", None)
        summary = str(plan.get("plan_summary_short") or "").strip() or _compact_plan_summary(plan)
        for index, item in enumerate(steps):
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or item.get("step") or "").strip()
            if not action:
                continue
            stage = "intent" if index == 0 else f"ms_agent_step_{index + 1}"
            callback = (context.session_meta or {}).get("status_callback")
            public_action = redact_skill_file_names(action, skill_root)
            label = normalize_ms_agent_progress_label(
                _compact_status_label(public_action, fallback=f"执行步骤{index + 1}"),
                fallback=f"执行步骤{index + 1}",
            )
            if callable(callback) and label:
                callback(
                    {
                        "stage": stage,
                        "label": label,
                        "detail": action,
                        "summary": summary,
                        "source": "ms_agent",
                    }
                )

    def _assistant_message_metadata(self, context, active_skill: str | None = None) -> dict[str, str]:
        metadata = build_skill_display(context, active_skill=active_skill, runtime_registry=self.runtime_registry)
        turn_id = str((context.session_meta or {}).get("active_turn_id") or "")
        if turn_id:
            metadata["turn_id"] = turn_id
        return metadata

    def _is_current_stream_generation(self, context) -> bool:
        meta = context.session_meta or {}
        generation_by_thread = meta.get("stream_generation_by_thread")
        if not isinstance(generation_by_thread, dict):
            return True
        thread_generation = generation_by_thread.get(str(threading.get_ident()))
        if not thread_generation:
            return True
        return (
            thread_generation == meta.get("active_stream_generation")
            and thread_generation != meta.get("cancelled_stream_generation")
        )

    def _emit_reply_delta(self, context, text: str) -> None:
        if not (context.session_meta or {}).get("stream_final_reply"):
            return
        if not self._is_current_stream_generation(context):
            return
        callback = (context.session_meta or {}).get("reply_delta_callback")
        if not callable(callback):
            return
        for chunk in self._chunk_reply_for_stream(text):
            callback(chunk)

    def _emit_reasoning_delta(self, context, text: str) -> None:
        if not (context.session_meta or {}).get("stream_final_reply"):
            return
        if not self._is_current_stream_generation(context):
            return
        callback = (context.session_meta or {}).get("reasoning_delta_callback")
        if callable(callback) and text:
            callback(text)

    def _has_streamed_reply(self, context) -> bool:
        parts = (context.session_meta or {}).get("streamed_reply_parts")
        return isinstance(parts, list) and any(isinstance(item, str) and item for item in parts)

    def _record_runtime_prompt(
        self,
        context,
        *,
        phase: str,
        bundle,
        skill_name: str,
        assembly: PromptAssembly,
        messages: list[ChatMessage],
        llm_response: str | dict[str, Any] | None = None,
        reasoning: str = "",
    ) -> None:
        if not messages:
            return
        base_record = {
            "skill_name": skill_name,
            "skill_id": bundle.runtime_metadata.skill_id or skill_name,
            "skill_type": bundle.runtime_metadata.skill_type,
            "phase": phase,
            "prompt_key": "skill_runtime",
            "prompt_title": "Skill Runtime LLM Prompt",
            "assembled_from": list(assembly.assembled_from),
            "reference_strategy": assembly.reference_strategy,
            "retrieved_sources": [item.source_path for item in assembly.retrieved_items],
            "retrieved_count": len(assembly.retrieved_items),
            "generated_asset_domains": list(assembly.generated_asset_domains),
            "local_asset_paths": list(assembly.local_asset_paths),
            "variables": {
                "messages": [
                    {
                        "role": item.role,
                        "content": item.content,
                        "name": item.name,
                        "tool_call_id": item.tool_call_id,
                    }
                    for item in messages
                ],
                "thinking": self._runtime_thinking_options(context),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        records: list[dict[str, Any]] = [
            {
                **base_record,
                "layer": "core",
                "prompt_title": "Skill Runtime Core Prompt",
                "prompt_content": assembly.core_prompt,
            }
        ]
        if assembly.retrieval_prompt:
            records.append(
                {
                    **base_record,
                    "layer": "retrieval",
                    "prompt_title": "Skill Runtime Retrieval Context",
                    "prompt_content": assembly.retrieval_prompt,
                }
            )
        final_record = {
            **base_record,
            "layer": "final",
            "prompt_title": "Skill Runtime Final Prompt",
            "prompt_content": assembly.final_prompt,
        }
        if llm_response is not None:
            final_record["llm_response"] = llm_response
        if reasoning:
            final_record["llm_reasoning"] = reasoning
        records.append(final_record)
        if llm_response is None:
            self._record_retrieval_context_event(
                context,
                phase=phase,
                bundle=bundle,
                skill_name=skill_name,
                assembly=assembly,
            )
        self._record_events(context, [make_event("prompt_assembly", record) for record in records])

    def _record_retrieval_context_event(
        self,
        context,
        *,
        phase: str,
        bundle,
        skill_name: str,
        assembly: PromptAssembly,
    ) -> None:
        if not assembly.retrieved_items:
            return
        payload = {
            "phase": phase,
            "skill_name": skill_name,
            "skill_id": bundle.runtime_metadata.skill_id or skill_name,
            "skill_type": bundle.runtime_metadata.skill_type,
            "retrieved_count": len(assembly.retrieved_items),
            "generated_asset_domains": list(assembly.generated_asset_domains),
            "local_asset_paths": list(assembly.local_asset_paths),
            "items": [
                {
                    "index": index,
                    "source_type": item.source_type,
                    "source_path": item.source_path,
                    "title": item.title,
                    "score": item.score,
                    "snippet": item.snippet,
                }
                for index, item in enumerate(assembly.retrieved_items, start=1)
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        reference_items = [
            item
            for item in payload["items"]
            if str(item.get("source_type") or "").lower() == "reference"
            or str(item.get("source_path") or "").startswith("references/")
            or "/references/" in str(item.get("source_path") or "")
        ]
        reference_payload = {
            **payload,
            "retrieved_count": len(reference_items),
            "items": reference_items,
            "source_event_type": "retrieval_context",
        }
        events = [make_event("retrieval_context", payload)]
        if reference_items:
            events.append(make_event("reference_context", reference_payload))
        self._record_events(context, events)

    def _record_ms_agent_reference_context_event(self, context, *, skill_name: str, loaded_context) -> None:
        references = getattr(loaded_context, "references", None) or []
        if not references:
            return
        items: list[dict[str, Any]] = []
        for index, reference in enumerate(references, start=1):
            path = str(reference.get("path") or reference.get("name") or "")
            content = str(reference.get("content") or "")
            items.append(
                {
                    "index": index,
                    "source_type": "reference",
                    "source_path": path,
                    "title": _reference_title_from_path(path),
                    "score": None,
                    "snippet": _truncate_debug_text(content.strip(), limit=900),
                }
            )
        payload = {
            "phase": "ms_agent_lazy_load",
            "skill_name": skill_name,
            "skill_id": skill_name,
            "skill_type": "native",
            "retrieved_count": len(items),
            "generated_asset_domains": [],
            "local_asset_paths": [],
            "items": items,
            "source_event_type": "ms_agent_runtime",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._record_events(context, [make_event("reference_context", payload)])

    def _record_tool_result_event(self, context, *, call, result: ToolCallResult) -> None:
        parsed_content = _try_parse_json(result.content)
        payload: dict[str, Any] = {
            "tool_name": result.name,
            "call_id": result.id,
            "ok": result.ok,
            "error": result.error,
            "arguments": call.arguments,
            "sources": list(result.sources),
            "content": _truncate_debug_text(result.content, limit=20_000),
            "content_preview": _truncate_debug_text(result.content, limit=1_200),
            "content_truncated": len(result.content or "") > 20_000,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if parsed_content is not None:
            payload["parsed_content"] = parsed_content
        self._record_events(context, [make_event("tool_result", payload)])

    def _runtime_thinking_options(self, context) -> dict[str, bool]:
        meta = context.session_meta or {}
        return {
            "enable_thinking": bool(meta.get("enable_thinking", self.llm_config.enable_thinking)),
            "return_reasoning": bool(meta.get("return_reasoning", self.llm_config.return_reasoning)),
        }

    def _runtime_client_for_context(self, context):
        meta = context.session_meta or {}
        if "enable_thinking" in meta or "return_reasoning" in meta:
            return self._build_runtime_client(self.llm_config, self._runtime_thinking_options(context))
        return self.runtime_client

    def _apply_legacy_llm_options(self, context) -> None:
        options = self._runtime_thinking_options(context)
        for skill_name in self.registry.names():
            skill = self.registry.get(skill_name)
            llm_client = getattr(skill, "llm_client", None)
            config = getattr(llm_client, "config", None)
            if config is None:
                continue
            if hasattr(config, "enable_thinking"):
                config.enable_thinking = options["enable_thinking"]
            if hasattr(config, "return_reasoning"):
                config.return_reasoning = options["return_reasoning"]

    def _chunk_reply_for_stream(self, text: str, chunk_size: int = 24) -> list[str]:
        if not text:
            return []
        chunks: list[str] = []
        current = ""
        for char in text:
            current += char
            if char in "。！？；\n" or len(current) >= chunk_size:
                chunks.append(current)
                current = ""
        if current:
            chunks.append(current)
        return chunks

    def _prepare_ms_agent_native_turn(
        self,
        bundle,
        state: SessionState,
        client: OpenAICompatibleChatClient,
        logger: RuntimeLogger,
        context,
    ) -> str | None:
        if bundle.runtime_metadata.skill_type != "native":
            return None

        skill_name = bundle.contract.skill_id or bundle.root_name
        if not self.ms_agent_probe.available:
            detail = self.ms_agent_probe.error or self.ms_agent_probe.status
            reply = (
                "当前 Runtime Skill 底层依赖 ms-agent，但运行环境尚不可用，"
                f"因此无法执行 `{skill_name}`。请先安装并配置 ms-agent 后重试。"
            )
            logger.log("ms_agent_runtime.unavailable", skill_id=skill_name, detail=detail)
            self._emit_reply_delta(context, reply)
            self._record_events(
                context,
                [
                    make_event(
                        "ms_agent_runtime",
                        {
                            "step": "runtime_health",
                            "status": self.ms_agent_probe.status,
                            "skill_id": skill_name,
                            "error": detail,
                        },
                    )
                ],
            )
            return reply

        latest_user_message = next((item.content for item in reversed(state.messages) if item.role == "user"), "")
        stream_combined_response = bool(
            state.status_flags.pop("ms_agent_stream_combined_response", False)
        )
        require_tool_routing_gate = bool(
            state.status_flags.pop("ms_agent_require_tool_routing_gate", False)
        )
        stream_reply_callback = None
        if stream_combined_response and not questionnaire_enabled(bundle):
            callback = (context.session_meta or {}).get("reply_delta_callback")
            if (context.session_meta or {}).get("stream_final_reply") and callable(callback):
                stream_reply_callback = callback

        def cancel_check() -> bool:
            callback = (context.session_meta or {}).get("stream_cancel_check")
            if callable(callback):
                try:
                    return bool(callback())
                except Exception:
                    pass
            return not self._is_current_stream_generation(context)

        planner_llm = _RuntimePlannerLLM(
            client,
            logger=logger,
            skill_dir=bundle.root_dir,
            stream_reply_callback=stream_reply_callback,
            cancel_check=cancel_check,
            tool_routing_context=build_ms_agent_tool_routing_context(bundle, state),
            require_tool_routing_gate=require_tool_routing_gate,
        )
        planner_memory = dict(state.conversation_memory or {})
        # The uncovered raw turns are supplied through ``history`` below. Keep
        # memory_context focused on summary/facts so ms-agent does not serialize
        # the same conversation twice.
        planner_memory.pop("recent_messages", None)
        memory_facts = (
            planner_memory.get("facts")
            if isinstance(planner_memory.get("facts"), dict)
            else {}
        )
        memory_global_facts = (
            memory_facts.get("global")
            if isinstance(memory_facts.get("global"), dict)
            else {}
        )
        planner_memory["facts"] = {
            **memory_facts,
            # Runtime facts are newer when both sources contain the same key,
            # while memory-only extracted facts must remain available.
            "global": {**memory_global_facts, **dict(state.global_facts or {})},
            "current_skill": dict(state.skill_facts.get(skill_name, {}) or {}),
            "current_stage": dict(
                state.stage_facts.get(skill_name, {}).get(state.stage, {}) or {}
            ),
        }
        planner_memory["status"] = {
            **(
                planner_memory.get("status")
                if isinstance(planner_memory.get("status"), dict)
                else {}
            ),
            "active_skill_id": skill_name,
            "stage": state.stage,
        }
        try:
            planner_messages = self._conversation_messages_for_model(state)
            loaded_context, current_lazy_load, steps = self.ms_agent_runtime.run_single_skill_turn(
                skill_id=skill_name,
                skill_dir=bundle.root_dir,
                planner_llm=planner_llm,
                message=latest_user_message,
                history=planner_messages[:-1],
                turn=len([item for item in state.messages if item.role == "user"]),
                previous_lazy_load=dict(state.status_flags.get("ms_agent_last_lazy_load") or {}),
                execute_scripts=False,
                memory_context=planner_memory,
            )
        except Exception as exc:  # noqa: BLE001
            if _is_recoverable_ms_agent_plan_error(exc):
                detail = f"{type(exc).__name__}: {exc}"
                logger.log("ms_agent_runtime.fallback_to_hailiang_prompt", skill_id=skill_name, error=detail)
                state.status_flags["ms_agent_runtime"] = {
                    "runtime": "ms_agent_single_skill",
                    "skill_key": f"{skill_name}@fallback",
                    "plan": {
                        "can_handle": True,
                        "plan_summary": "MS-Agent planner failed; continuing with Hailiang prompt assembly",
                        "plan_summary_short": "继续规划",
                        "plan_summary_short_source": "fallback_runtime",
                        "steps": [],
                        "required_scripts": [],
                        "required_references": [],
                        "required_resources": [],
                        "required_packages": [],
                        "parameters": {},
                        "reasoning": detail,
                    },
                    "planner": {
                        "name": getattr(planner_llm, "planner_name", "hailiang_runtime_llm"),
                        "error": detail,
                        "raw_response_preview": _truncate_debug_text(
                            planner_llm.last_raw_response or "",
                            limit=1200,
                        ),
                    },
                    "execution_outputs": [],
                }
                self._record_events(
                    context,
                    [
                        make_event(
                            "ms_agent_runtime",
                            {
                                "step": "fallback_runtime",
                                "status": "warning",
                                "skill_id": skill_name,
                                "detail": "MS-Agent planner failed; continued with Hailiang prompt assembly",
                                "error": detail,
                            },
                        )
                    ],
                )
                return None
            self._emit_model_error(context, exc, terminal=False)
            reply = (
                "ms-agent Runtime Skill 执行失败，当前无法继续本场景。"
                f"错误信息：{type(exc).__name__}: {exc}"
            )
            logger.log("ms_agent_runtime.failed", skill_id=skill_name, error=str(exc))
            self._emit_reply_delta(context, reply)
            self._record_events(
                context,
                [
                    make_event(
                        "ms_agent_runtime",
                        {
                            "step": "fallback_runtime",
                            "status": "error",
                            "skill_id": skill_name,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                ],
            )
            return reply

        # The planner adapter has already supplied a valid local lazy-load plan
        # when its auxiliary LLM call fails. Keep that diagnostic in the runtime
        # trace, but do not emit a client-facing model error after recovery.

        planner_routing_decision = parse_ms_agent_tool_routing(
            planner_llm.last_tool_routing_payload
        )
        if loaded_context.scripts:
            script_decision = planner_routing_decision or RoutingDecision()
            self._emit_tool_status(
                context,
                name="script",
                label=_tool_intent_label(script_decision, "script"),
                detail="服务端已授权执行当前 Skill 所需脚本",
            )
            steps = [
                step
                for step in steps
                if not (step.name == "script_execution" and step.status == "skipped")
            ]
            execution_outputs, script_steps = self.ms_agent_runtime.execute_scripts_in_sandbox(
                skill_id=skill_name,
                skill_dir=bundle.root_dir,
                loaded_scripts=list(loaded_context.scripts or []),
                execute_scripts=True,
                script_inputs=self._ms_agent_script_inputs(
                    skill_name=skill_name,
                    bundle=bundle,
                    state=state,
                    context=context,
                    latest_user_message=latest_user_message,
                    plan=loaded_context.plan,
                ),
            )
            loaded_context.execution_outputs = execution_outputs
            loaded_context.raw_trace["execution_outputs"] = execution_outputs
            steps.extend(script_steps)
            if any(item.get("ok") is False for item in execution_outputs if isinstance(item, dict)):
                self._emit_tool_status(
                    context,
                    name="script",
                    label="规划计算未完成",
                    detail="Skill 脚本执行失败，后续将使用安全兜底",
                )

        state.status_flags["ms_agent_last_lazy_load"] = current_lazy_load
        state.status_flags["ms_agent_last_lazy_load_skill_id"] = skill_name
        state.status_flags["ms_agent_loaded_reference_context_skill_id"] = skill_name
        state.status_flags["ms_agent_loaded_reference_context"] = _ms_agent_loaded_reference_context(loaded_context)
        state.status_flags["ms_agent_runtime"] = _summarize_ms_agent_runtime_trace(loaded_context.raw_trace)
        state.status_flags.pop("ms_agent_combined_response", None)
        state.status_flags.pop("ms_agent_tool_routing", None)
        if planner_llm.last_tool_routing_payload is not None:
            state.status_flags["ms_agent_tool_routing"] = planner_llm.last_tool_routing_payload
        combined_response = str(
            getattr(loaded_context, "combined_response", "")
            or planner_llm.last_combined_response
            or ""
        )
        has_loaded_dependencies = bool(
            loaded_context.scripts
            or getattr(loaded_context, "references", None)
            or getattr(loaded_context, "resources", None)
            or getattr(loaded_context, "packages", None)
        )
        if not has_loaded_dependencies and combined_response:
            state.status_flags["ms_agent_combined_response"] = combined_response
            if planner_llm.streamed_combined_response:
                state.status_flags["ms_agent_combined_response_streamed"] = True
        plan = state.status_flags["ms_agent_runtime"].get("plan")
        if isinstance(plan, dict):
            last_metrics = getattr(client, "last_request_metrics", None)
            llm_metrics = last_metrics() if callable(last_metrics) else {}
            if llm_metrics:
                plan["llm_metrics"] = llm_metrics
            plan["planner_to_first_text_ms"] = planner_llm.first_visible_delta_ms
            plan["selected_references"] = [
                str(item.get("path") or item.get("name") or "")
                for item in _ms_agent_loaded_reference_context(loaded_context)
            ]
            raw_planner_plan = _try_parse_json(planner_llm.last_raw_response or "")
            if isinstance(raw_planner_plan, dict):
                if raw_planner_plan.get("plan_summary_short") and not plan.get("plan_summary_short"):
                    plan["plan_summary_short"] = raw_planner_plan.get("plan_summary_short")
                if raw_planner_plan.get("plan_summary") and not plan.get("plan_summary"):
                    plan["plan_summary"] = raw_planner_plan.get("plan_summary")
            plan_summary_short, plan_summary_source = _normalize_plan_summary(plan)
            plan["plan_summary_short"] = plan_summary_short
            plan["plan_summary_short_source"] = plan_summary_source
            logger.log(
                "ms_agent.plan_summary",
                skill_id=skill_name,
                plan_summary=str(plan.get("plan_summary") or ""),
                plan_summary_short=plan_summary_short,
                plan_summary_short_source=plan_summary_source,
                summary_limit="5-10 chars",
                steps_count=len(plan.get("steps") or []),
            )
            self._record_events(
                context,
                [
                    make_event(
                        "ms_agent_plan_summary",
                        {
                            "skill_id": skill_name,
                            "plan_summary": str(plan.get("plan_summary") or ""),
                            "plan_summary_short": plan_summary_short,
                            "plan_summary_short_source": plan_summary_source,
                            "summary_limit": "5-10 chars",
                            "steps_count": len(plan.get("steps") or []),
                            "request_purpose": llm_metrics.get("request_purpose"),
                            "prompt_chars": llm_metrics.get("prompt_chars"),
                            "input_tokens": llm_metrics.get("input_tokens"),
                            "output_tokens": llm_metrics.get("output_tokens"),
                            "ttft_ms": llm_metrics.get("ttft_ms"),
                            "llm_duration_ms": llm_metrics.get("duration_ms"),
                        },
                    )
                ],
            )
            self._emit_ms_agent_plan_statuses(context, plan)
        for step in steps:
            step_name = "runtime_health" if step.name == "ms_agent_health" else step.name
            logger.log(
                f"ms_agent.{step_name}",
                status=step.status,
                detail=step.detail,
                payload=step.payload,
            )
        self._record_events(
            context,
            [
                make_event(
                        "ms_agent_runtime",
                        {
                            "step": "runtime_health" if step.name == "ms_agent_health" else step.name,
                            "status": step.status,
                            "detail": step.detail,
                            "payload": step.payload,
                        "skill_id": skill_name,
                    },
                )
                for step in steps
            ],
        )
        self._record_ms_agent_reference_context_event(context, skill_name=skill_name, loaded_context=loaded_context)
        return None

    def _ms_agent_script_inputs(
        self,
        *,
        skill_name: str,
        bundle,
        state: SessionState,
        context,
        latest_user_message: str,
        plan: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        parameters = dict(plan.get("parameters") or {}) if isinstance(plan, dict) else {}
        query = str(parameters.get("query") or latest_user_message or "")
        if query == "<from_session_context>":
            query = latest_user_message
        user_id = str(getattr(context, "user_id", "") or state.session_id or "")
        turn_index = len([item for item in state.messages if item.role == "user"])
        messages = [
            {"role": item.role, "content": item.content}
            for item in state.messages
        ]
        base_payload = {
            "query": query,
            "user_id": user_id,
            "session_id": str(getattr(context, "session_id", "") or state.session_id),
            "profile_id": str(getattr(context, "profile_id", "") or ""),
            "active_skill_id": skill_name,
            "turn_index": turn_index,
            "parameters": parameters,
            "planner_parameters": parameters,
            "facts": dict(state.global_facts),
            "messages": messages,
            "recent_messages": messages[-self.runtime_bridge_config.active_window_messages :],
        }
        for key, value in parameters.items():
            if value in (None, "", []):
                continue
            if isinstance(value, str) and value == "<from_session_context>":
                continue
            base_payload[key] = value

        status_payload = {
            **build_status_track_payload(bundle, state),
            **base_payload,
            "latest_user_message": latest_user_message,
        }
        profile_payload = dict(base_payload)
        profile_payload["action"] = str(parameters.get("action") or "read")
        profile_payload["user_id"] = user_id
        if profile_payload["action"] == "init":
            profile_payload.setdefault("base_info", {})
        if profile_payload["action"] == "save":
            profile_payload.setdefault("child_data", {})
        return {
            "*": base_payload,
            "__default__": base_payload,
            "status_track.py": status_payload,
            "scripts/status_track.py": status_payload,
            "profile_op.py": profile_payload,
            "scripts/profile_op.py": profile_payload,
        }

    def _resolve_runtime_reply(
        self,
        bundle,
        state: SessionState,
        client: OpenAICompatibleChatClient,
        logger: RuntimeLogger,
        context,
    ) -> tuple[str, str]:
        current_bundle = self.runtime_registry.get(state.active_skill_id) or bundle
        logger.log(
            "turn.resolve.start",
            latest_user_message=next((item.content for item in reversed(state.messages) if item.role == "user"), ""),
            stage=state.stage,
            active_skill_id=state.active_skill_id,
        )
        routing_mode = self.runtime_bridge_config.tool_routing_mode
        routing_decision: RoutingDecision | None = None
        initial_tool_specs: tuple[ToolSpec, ...] = ()
        if routing_mode == "standalone":
            routing_decision = classify_tool_routing(
                current_bundle,
                state,
                client,
                logger=logger,
            )
            initial_tool_specs = build_tool_specs(
                current_bundle,
                state,
                routing_decision=routing_decision,
                logger=logger,
            )
            state.status_flags["ms_agent_stream_combined_response"] = not any(
                item.enabled for item in initial_tool_specs
            )
        else:
            # The incremental JSON extractor waits for a valid no-tool
            # tool_routing object before exposing assistant_message.
            state.status_flags["ms_agent_stream_combined_response"] = True
            state.status_flags["ms_agent_require_tool_routing_gate"] = True
        try:
            unavailable_reply = self._prepare_ms_agent_native_turn(
                current_bundle,
                state,
                client,
                logger,
                context,
            )
        finally:
            state.status_flags.pop("ms_agent_stream_combined_response", None)
            state.status_flags.pop("ms_agent_require_tool_routing_gate", None)
        if unavailable_reply is not None:
            return unavailable_reply, ""
        merged_routing_payload = state.status_flags.pop("ms_agent_tool_routing", None)
        if routing_mode == "ms_agent":
            routing_decision = parse_ms_agent_tool_routing(merged_routing_payload)
            if routing_decision is None:
                logger.log(
                    "routing.ms_agent.invalid",
                    fallback_enabled=self.runtime_bridge_config.tool_routing_fallback_on_invalid,
                    payload_type=type(merged_routing_payload).__name__,
                )
                self._record_events(
                    context,
                    [
                        make_event(
                            "tool_routing_fallback",
                            {
                                "source": "ms_agent",
                                "reason": "missing_or_invalid_tool_routing",
                                "fallback_enabled": self.runtime_bridge_config.tool_routing_fallback_on_invalid,
                            },
                        )
                    ],
                )
                if self.runtime_bridge_config.tool_routing_fallback_on_invalid:
                    routing_decision = classify_tool_routing(
                        current_bundle,
                        state,
                        client,
                        logger=logger,
                    )
                else:
                    routing_decision = RoutingDecision(source="invalid_fail_closed")
            raw_specs = build_tool_specs(
                current_bundle,
                state,
                routing_decision=routing_decision,
                logger=logger,
            )
            initial_tool_specs = _authorize_requested_tool_specs(raw_specs, routing_decision)
            logger.log(
                "routing.ms_agent.decision",
                source=routing_decision.source,
                required=routing_decision.required,
                candidates=[
                    {"kind": item.kind, "name": item.name, "intent_label": item.intent_label}
                    for item in routing_decision.candidates
                ],
                authorized_tools=[item.name for item in initial_tool_specs if item.enabled],
                rejected_tools=[
                    item.name
                    for item in routing_decision.candidates
                    if item.kind == "tool"
                    and not any(spec.name == item.name and spec.enabled for spec in initial_tool_specs)
                ],
            )
        assert routing_decision is not None
        combined_response = str(state.status_flags.pop("ms_agent_combined_response", "") or "")
        combined_response_streamed = bool(
            state.status_flags.pop("ms_agent_combined_response_streamed", False)
        )
        if questionnaire_enabled(current_bundle):
            if routing_decision.required:
                logger.log(
                    "routing.ms_agent.questionnaire_tools_suppressed",
                    candidates=[item.name for item in routing_decision.candidates],
                )
            return self._resolve_questionnaire_reply(
                current_bundle,
                state,
                client,
                logger,
                context,
                raw_reply=combined_response or None,
            )

        tool_results: tuple[ToolCallResult, ...] = ()
        transient_messages: tuple[ChatMessage, ...] = ()
        preferred_mode = "native"
        used_tool_names: list[str] = []
        skill_catalog = self._runtime_prompt_skill_catalog(state)

        if combined_response:
            combined_tool_specs = initial_tool_specs
            if not any(item.enabled for item in combined_tool_specs):
                reply = _sanitize_assistant_reply(
                    combined_response,
                    response_policy=current_bundle.runtime_metadata.response_policy,
                )
                self._emit_runtime_status(context, "response", "正在生成回复")
                if not combined_response_streamed:
                    self._emit_reply_delta(context, reply)
                self._record_events(
                    context,
                    [
                        make_event(
                            "ms_agent_runtime",
                            {
                                "step": "combined_response",
                                "status": "success",
                                "skill_id": current_bundle.contract.skill_id or current_bundle.root_name,
                                "detail": "MS-Agent plan and runtime response completed in one model call",
                                "payload": {"stream": combined_response_streamed},
                            },
                        )
                    ],
                )
                return reply, ""

        for tool_index in range(MAX_TOOL_CALLS_PER_TURN + 1):
            current_bundle = self.runtime_registry.get(state.active_skill_id) or bundle

            if tool_results:
                assembly = build_prompt_assembly(
                    current_bundle,
                    state,
                    tool_results=tool_results,
                    tool_mode="none",
                    available_tool_specs=(),
                    max_tool_calls=0,
                    routing_decision=routing_decision,
                    skill_catalog=skill_catalog,
                )
                messages = self._messages_from_assembly(state, assembly, transient_messages)
                return self._stream_runtime_final_text(
                    current_bundle,
                    current_bundle.contract.skill_id or current_bundle.root_name,
                    assembly,
                    messages,
                    client,
                    logger,
                    context,
                    phase="runtime_final_response_after_tools",
                )

            tool_specs = _authorize_requested_tool_specs(
                build_tool_specs(
                    current_bundle,
                    state,
                    routing_decision=routing_decision,
                    logger=logger,
                ),
                routing_decision,
            )
            assembly = build_prompt_assembly(
                current_bundle,
                state,
                tool_results=tool_results,
                tool_mode=preferred_mode,
                available_tool_specs=tool_specs,
                max_tool_calls=MAX_TOOL_CALLS_PER_TURN,
                routing_decision=routing_decision,
                skill_catalog=skill_catalog,
            )
            messages = self._messages_from_assembly(state, assembly, transient_messages)
            enabled_tool_specs = tuple(item for item in tool_specs if item.enabled)
            if not enabled_tool_specs:
                return self._stream_runtime_final_text(
                    current_bundle,
                    current_bundle.contract.skill_id or current_bundle.root_name,
                    assembly,
                    messages,
                    client,
                    logger,
                    context,
                    phase="runtime_final_response",
                )

            self._record_runtime_prompt(
                context,
                phase="runtime_tool_or_response",
                bundle=current_bundle,
                skill_name=current_bundle.contract.skill_id or current_bundle.root_name,
                assembly=assembly,
                messages=messages,
            )
            turn_result = client.complete_with_tools(
                messages,
                tool_specs,
                preferred_mode=preferred_mode,
                logger=logger,
            )
            if not turn_result.tool_calls:
                final_text = _sanitize_assistant_reply(
                    turn_result.final_text,
                    response_policy=current_bundle.runtime_metadata.response_policy,
                )
                self._emit_runtime_status(context, "response", "正在生成回复")
                self._emit_reply_delta(context, final_text)
                self._record_runtime_prompt(
                    context,
                    phase="runtime_tool_or_response",
                    bundle=current_bundle,
                    skill_name=current_bundle.contract.skill_id or current_bundle.root_name,
                    assembly=assembly,
                    messages=messages,
                    llm_response=final_text,
                )
                return final_text, ""

            if tool_index >= MAX_TOOL_CALLS_PER_TURN:
                final_text = "本轮工具调用次数已达上限；如果你愿意，我可以先基于现有信息给出一个保守建议。"
                self._emit_runtime_status(context, "response", "正在生成回复")
                self._emit_reply_delta(context, final_text)
                return final_text, ""

            next_transient_messages: list[ChatMessage] = list(transient_messages)
            next_tool_results: list[ToolCallResult] = list(tool_results)
            enabled_tool_names = {item.name for item in enabled_tool_specs}
            for call in turn_result.tool_calls:
                if call.name not in enabled_tool_names:
                    result = ToolCallResult(
                        id=call.id,
                        name=call.name,
                        ok=False,
                        content="",
                        error=f"工具 {call.name} 未通过本轮服务端授权。",
                    )
                    logger.log(
                        "tool.call.rejected",
                        reason="not_authorized",
                        tool_name=call.name,
                        call_id=call.id,
                    )
                elif call.name in used_tool_names:
                    result = ToolCallResult(
                        id=call.id,
                        name=call.name,
                        ok=False,
                        content="",
                        error=f"工具 {call.name} 在本轮已调用过，不能重复循环调用。",
                    )
                    logger.log("tool.call.rejected", reason="duplicate", tool_name=call.name, call_id=call.id)
                elif _is_empty_tool_call(call):
                    result = ToolCallResult(
                        id=call.id,
                        name=call.name,
                        ok=False,
                        content="",
                        error=f"工具 {call.name} 缺少必要参数。",
                    )
                    logger.log("tool.call.rejected", reason="empty_query", tool_name=call.name, call_id=call.id)
                else:
                    self._emit_tool_status(
                        context,
                        name=call.name,
                        label=_tool_intent_label(routing_decision, call.name),
                        detail=f"服务端已授权执行工具 {call.name}",
                    )
                    result = execute_tool_call(
                        current_bundle,
                        state,
                        call,
                        routing_decision=routing_decision,
                        logger=logger,
                    )
                    used_tool_names.append(call.name)
                    if not result.ok:
                        self._emit_tool_status(
                            context,
                            name=call.name,
                            label="资料查询未完成",
                            detail=result.error or f"工具 {call.name} 执行失败",
                        )
                self._record_tool_result_event(context, call=call, result=result)
                next_transient_messages.extend(_tool_exchange_messages(call, result, tool_mode=turn_result.tool_mode))
                next_tool_results.append(result)
            tool_results = tuple(next_tool_results)
            transient_messages = tuple(next_transient_messages)
            preferred_mode = turn_result.tool_mode

        final_text = "当前无法稳定完成工具调用流程；如果你愿意，我先基于现有信息给出方向性建议。"
        self._emit_runtime_status(context, "response", "正在生成回复")
        self._emit_reply_delta(context, final_text)
        return final_text, ""

    def _questionnaire_continuation_messages(
        self,
        bundle,
        state: SessionState,
        user_message: str,
        continuation: dict[str, Any],
    ) -> list[ChatMessage]:
        metadata = bundle.runtime_metadata
        memory = state.conversation_memory if isinstance(state.conversation_memory, dict) else {}
        memory_facts = memory.get("facts") if isinstance(memory.get("facts"), dict) else {}
        recent_messages = memory.get("recent_messages") if isinstance(memory.get("recent_messages"), list) else []
        skill_facts = state.skill_facts.get(continuation["skill_id"], {})
        reconciliation = continuation.get("answer_reconciliation", {})
        reconciliation_enabled = isinstance(reconciliation, dict) and reconciliation.get("enabled")
        payload = {
            "skill": {
                "name": metadata.name,
                "brief": metadata.brief,
                "description": metadata.description,
                "stage": state.stage,
            },
            "answers": continuation["answers"],
            "tier": continuation["tier"],
            "runtime_facts": {
                "global": state.global_facts,
                "current_skill": {
                    key: value
                    for key, value in skill_facts.items()
                    if key not in {
                        "_pending_questionnaire",
                        "_questionnaire_reconciliation_completed",
                        "_reused_answer_sources",
                        "last_questionnaire_error",
                    }
                },
            },
            "conversation": {
                "summary": str(memory.get("summary") or ""),
                "facts": memory_facts,
                # The evidence list below already carries the same user turns
                # with stable source IDs during first-form reconciliation.
                "recent_messages": [] if reconciliation_enabled else recent_messages,
            },
            "latest_user_answer": user_message,
            "max_fields_per_form": continuation["max_fields_per_form"],
            "question_catalog": continuation["question_catalog"],
            "answer_reconciliation": reconciliation,
        }
        return [
            ChatMessage(
                role="system",
                content=(
                    "你是问卷对话推进助手。服务端已经完成已提交表单答案的解析和候选问题过滤。"
                    "当 answer_reconciliation.enabled=true 时，你还要在同一次响应中比对安全 fact_sources、"
                    "用户 message_sources 与 question_catalog，识别用户已经明确回答过的项目。"
                    "必须逐项检查 question_catalog 中的每个未答问题；同一条用户原话包含多个答案时，"
                    "要分别写入 resolved_answers，不得只在 assistant_message 中提到却遗漏结构化答案。"
                    "只在证据明确且 confidence>=min_confidence 时写入 resolved_answers；"
                    "每项严格使用 {question_id,value,source_id,evidence,confidence}，source_id 必须来自给定来源。"
                    "fact 来源只能匹配 eligible_question_ids，value 必须忠实等于 fact 值；"
                    "message 来源的 evidence 必须是用户原话中的短句，并明确包含答案值。不得从助手消息、旧总结或常识猜测答案。"
                    "从排除 resolved_answers 后剩余的 question_catalog 中选择最合适的下一批问题，"
                    "并先给出简短、有依据的阶段性说明和自然引导；说明可以自然确认本轮识别到的答案。"
                    "不得虚构、改写或直接在正文中提问；不得给出最终专业结论。"
                    "只返回一个 JSON 对象，字段必须严格按以下顺序："
                    '{"assistant_message":"面向用户的阶段性说明和引导",'
                    '"resolved_answers":[{"question_id":"合法问题ID","value":"答案",'
                    '"source_id":"给定来源ID","evidence":"用户原话或空字符串","confidence":0.95}],'
                    '"question_ids":["合法问题ID"],"collection_complete":false}。'
                    "不启用答案对齐或没有可靠答案时 resolved_answers 必须为 []。"
                    "question_ids 只能来自 question_catalog，数量不得超过 max_fields_per_form。"
                    "question_ids 不得包含 resolved_answers 中的项目。只要仍有未回答项目，collection_complete 必须为 false。"
                    "不要输出 Markdown 代码块。"
                ),
            ),
            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        ]

    def _resolve_lightweight_questionnaire(
        self,
        bundle,
        state: SessionState,
        logger: RuntimeLogger,
        context,
        user_message: str,
    ) -> tuple[str, str] | None:
        continuation = questionnaire_continuation_context(bundle, state)
        if continuation is None:
            return None
        messages = self._questionnaire_continuation_messages(
            bundle,
            state,
            user_message,
            continuation,
        )
        self._emit_runtime_status(context, "response", "正在生成回复")
        started = time.perf_counter()
        first_delta_ms: int | None = None
        raw_parts: list[str] = []
        streamed = False
        client = self.questionnaire_client
        try:
            if client is None:
                raw_reply = ""
            elif callable(getattr(client, "stream_complete", None)):
                extractor = _QuestionnaireContinuationExtractor(
                    {
                        str(item["question_id"])
                        for item in continuation["question_catalog"]
                    }
                )
                stream_kwargs: dict[str, Any] = {"logger": logger}
                if "request_purpose" in inspect.signature(client.stream_complete).parameters:
                    stream_kwargs["request_purpose"] = "questionnaire_continuation"
                if "cancel_check" in inspect.signature(client.stream_complete).parameters:
                    stream_kwargs["cancel_check"] = lambda: not self._is_current_stream_generation(context)
                for chunk in client.stream_complete(messages, **stream_kwargs):
                    delta = str(getattr(chunk, "content_delta", "") or "")
                    if not delta:
                        continue
                    raw_parts.append(delta)
                    visible = extractor.feed(delta)
                    if visible:
                        if first_delta_ms is None:
                            first_delta_ms = int((time.perf_counter() - started) * 1000)
                        callback = (context.session_meta or {}).get("reply_delta_callback")
                        if (context.session_meta or {}).get("stream_final_reply") and callable(callback):
                            callback(visible)
                            streamed = True
                raw_reply = "".join(raw_parts).strip()
            else:
                complete_kwargs: dict[str, Any] = {"logger": logger}
                if "request_purpose" in inspect.signature(client.complete).parameters:
                    complete_kwargs["request_purpose"] = "questionnaire_continuation"
                raw_reply = str(client.complete(messages, **complete_kwargs) or "")
        except Exception as exc:  # noqa: BLE001
            logger.log("questionnaire.continuation.failed", error=f"{type(exc).__name__}: {exc}")
            raw_reply = ""

        reply, block, decision = resolve_questionnaire_continuation(bundle, state, raw_reply)
        reply = _sanitize_assistant_reply(
            reply,
            response_policy=bundle.runtime_metadata.response_policy,
        )
        stage_questionnaire_form(state, bundle, block)
        if not streamed:
            self._emit_reply_delta(context, reply)
            if first_delta_ms is None:
                first_delta_ms = int((time.perf_counter() - started) * 1000)
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.log(
            "questionnaire.continuation.completed",
            request_purpose="questionnaire_continuation",
            duration_ms=duration_ms,
            first_delta_ms=first_delta_ms,
            prompt_chars=sum(len(item.content) for item in messages),
            selected_question_ids=decision.get("selected_question_ids", []),
            resolved_question_ids=[
                item.get("question_id") for item in decision.get("resolved_answers", [])
            ],
            rejected_resolved_question_ids=decision.get("rejected_resolved_answers", []),
            fallback_used=decision.get("fallback_used", False),
        )
        self._record_events(
            context,
            [
                make_event(
                    "native_questionnaire_response",
                    {
                        "skill_id": bundle.contract.skill_id or bundle.root_name,
                        "duration_ms": duration_ms,
                        "first_delta_ms": first_delta_ms,
                        "form_emitted": bool(block),
                        "request_purpose": "questionnaire_continuation",
                        "fallback_used": bool(decision.get("fallback_used")),
                        "question_ids": decision.get("selected_question_ids", []),
                        "resolved_question_ids": [
                            item.get("question_id") for item in decision.get("resolved_answers", [])
                        ],
                    },
                )
            ],
        )
        return reply, ""

    def _skill_entry_messages(
        self,
        bundle,
        state: SessionState,
    ) -> list[ChatMessage]:
        metadata = bundle.runtime_metadata
        memory = state.conversation_memory if isinstance(state.conversation_memory, dict) else {}
        handoff = state.status_flags.get("last_handoff_context")
        handoff = handoff if isinstance(handoff, dict) else {}
        skill_id = str(bundle.contract.skill_id or bundle.root_name)
        skill_facts = state.skill_facts.get(skill_id, {})
        public_skill_facts = {
            key: value
            for key, value in (skill_facts.items() if isinstance(skill_facts, dict) else [])
            if not str(key).startswith("_") and key not in {"last_questionnaire_error"}
        }
        payload = {
            "skill": {
                "name": _compact_skill_entry_context(metadata.name),
                "brief": _compact_skill_entry_context(metadata.brief),
                "description": _compact_skill_entry_context(metadata.description),
                "stage": state.stage,
            },
            "handoff": _compact_skill_entry_context(handoff),
            "facts": {
                "global": _compact_skill_entry_context(state.global_facts),
                "current_skill": _compact_skill_entry_context(public_skill_facts),
                "memory": _compact_skill_entry_context(
                    memory.get("facts") if isinstance(memory.get("facts"), dict) else {}
                ),
            },
            "conversation_summary": _truncate_debug_text(str(memory.get("summary") or ""), limit=1800),
        }
        return [
            ChatMessage(
                role="system",
                content=(
                    "你负责用户显式点击进入专项顾问后的首轮承接。目标 Skill 已由服务端确定，不需要再做路由、"
                    "工具选择、文件选择或完整方案规划。请基于 Skill 简介、handoff 和已知 facts，直接输出简短自然的中文正文。"
                    "若已有明确问题，先承接并给出一条有用的初步判断，再问一个最关键的澄清问题；"
                    "若信息不足，说明接下来能怎样帮助，并只问一个最关键的问题。不要重复询问 facts 中已有的信息。"
                    "不要提及 Skill ID、内部字段、文件、路由、模型或工具，不要输出 JSON 或 Markdown 标题。"
                    "正文控制在 220 个汉字以内，不得声称已经完成需要资料或工具才能完成的专业结论。"
                ),
            ),
            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        ]

    def _resolve_lightweight_skill_entry(
        self,
        bundle,
        state: SessionState,
        logger: RuntimeLogger,
        context,
    ) -> tuple[str, str]:
        messages = self._skill_entry_messages(bundle, state)
        self._emit_runtime_status(context, "response", "正在生成回复")
        started = time.perf_counter()
        first_delta_ms: int | None = None
        parts: list[str] = []
        streamed = False
        client = self.questionnaire_client
        try:
            if client is not None and callable(getattr(client, "stream_complete", None)):
                stream_kwargs: dict[str, Any] = {"logger": logger}
                signature = inspect.signature(client.stream_complete)
                if "request_purpose" in signature.parameters:
                    stream_kwargs["request_purpose"] = "skill_entry_response"
                if "cancel_check" in signature.parameters:
                    stream_kwargs["cancel_check"] = lambda: not self._is_current_stream_generation(context)
                for chunk in client.stream_complete(messages, **stream_kwargs):
                    delta = str(getattr(chunk, "content_delta", "") or "")
                    if not delta:
                        continue
                    parts.append(delta)
                    if first_delta_ms is None:
                        first_delta_ms = int((time.perf_counter() - started) * 1000)
                    callback = (context.session_meta or {}).get("reply_delta_callback")
                    if (context.session_meta or {}).get("stream_final_reply") and callable(callback):
                        callback(delta)
                        streamed = True
                reply = "".join(parts).strip()
            elif client is not None:
                complete_kwargs: dict[str, Any] = {"logger": logger}
                if "request_purpose" in inspect.signature(client.complete).parameters:
                    complete_kwargs["request_purpose"] = "skill_entry_response"
                reply = str(client.complete(messages, **complete_kwargs) or "").strip()
            else:
                reply = ""
        except Exception as exc:  # noqa: BLE001
            logger.log("skill_entry.fast_path.failed", error=f"{type(exc).__name__}: {exc}")
            reply = ""

        reply = _sanitize_assistant_reply(
            reply,
            response_policy=bundle.runtime_metadata.response_policy,
        ).strip()
        if not reply:
            name = str(bundle.runtime_metadata.name or bundle.contract.skill_id or "专项顾问")
            reply = f"已进入「{name}」。我会接着处理刚才的问题，请先告诉我你现在最想优先解决的一个重点。"
        if not streamed:
            self._emit_reply_delta(context, reply)
            if first_delta_ms is None:
                first_delta_ms = int((time.perf_counter() - started) * 1000)
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.log(
            "skill_entry.fast_path.completed",
            request_purpose="skill_entry_response",
            duration_ms=duration_ms,
            first_delta_ms=first_delta_ms,
            prompt_chars=sum(len(item.content) for item in messages),
            active_skill_id=state.active_skill_id,
        )
        self._record_events(
            context,
            [
                make_event(
                    "skill_entry_fast_response",
                    {
                        "skill_id": state.active_skill_id,
                        "duration_ms": duration_ms,
                        "first_delta_ms": first_delta_ms,
                        "request_purpose": "skill_entry_response",
                    },
                )
            ],
        )
        return reply, ""

    def _resolve_questionnaire_reply(
        self,
        bundle,
        state: SessionState,
        client: OpenAICompatibleChatClient,
        logger: RuntimeLogger,
        context,
        *,
        raw_reply: str | None = None,
    ) -> tuple[str, str]:
        """Use a non-streaming internal envelope, then stream only user-visible text."""
        self._emit_runtime_status(context, "response", "正在生成回复")
        assembly = build_prompt_assembly(
            bundle,
            state,
            tool_mode="none",
            available_tool_specs=(),
            max_tool_calls=0,
            skill_catalog=self._runtime_prompt_skill_catalog(state),
        )
        messages = self._messages_from_assembly(state, assembly, ())
        self._record_runtime_prompt(
            context,
            phase="native_questionnaire_response",
            bundle=bundle,
            skill_name=bundle.contract.skill_id or bundle.root_name,
            assembly=assembly,
            messages=messages,
        )
        started = time.perf_counter()
        combined_response = raw_reply is not None
        if raw_reply is None:
            turn_result = client.complete_with_tools(messages, (), preferred_mode="none", logger=logger)
            raw_reply = str(turn_result.final_text or "")
        invalid_replies: list[str] = []
        retry_count = 0
        if not questionnaire_reply_is_valid(raw_reply):
            invalid_replies.append(raw_reply)
            if not combined_response:
                retry_count = 1
                retry_messages = [
                    *messages,
                    ChatMessage(role="assistant", content=raw_reply),
                    ChatMessage(
                        role="user",
                        content=(
                            "Your previous response did not follow the Native Questionnaire Protocol. "
                            "Return exactly one JSON object now with assistant_message, state_patch, and "
                            "question_ids. Use only question_id values from question_catalog. For a "
                            "multi_path_planning matching/output conclusion, put canonical path_id values "
                            "from path_catalog in state_patch.matched_paths; use [] when no path option "
                            "should be shown. Use [] question_ids when no form is needed. Do not include "
                            "Markdown fences or any text outside JSON."
                        ),
                    ),
                ]
                retry_result = client.complete_with_tools(
                    retry_messages,
                    (),
                    preferred_mode="none",
                    logger=logger,
                )
                raw_reply = str(retry_result.final_text or "")
        if questionnaire_reply_is_valid(raw_reply):
            reply, block = decode_questionnaire_reply(bundle, state, raw_reply)
        else:
            invalid_replies.append(raw_reply)
            reply, block = deterministic_questionnaire_fallback(bundle, state, *invalid_replies)
        reply = _sanitize_assistant_reply(
            reply,
            response_policy=bundle.runtime_metadata.response_policy,
        )
        stage_questionnaire_form(state, bundle, block)
        deferred_promotions = flush_deferred_questionnaire_promotions(state, context, bundle) if block is None else []
        self._emit_reply_delta(context, reply)
        duration_ms = int((time.perf_counter() - started) * 1000)
        self._record_runtime_prompt(
            context,
            phase="native_questionnaire_response",
            bundle=bundle,
            skill_name=bundle.contract.skill_id or bundle.root_name,
            assembly=assembly,
            messages=messages,
            llm_response=raw_reply,
        )
        self._record_events(
            context,
            [
                make_event(
                    "native_questionnaire_response",
                    {
                        "skill_id": bundle.contract.skill_id or bundle.root_name,
                        "duration_ms": duration_ms,
                        "form_emitted": bool(block),
                        "retry_count": retry_count,
                        "fallback_used": bool(invalid_replies and not questionnaire_reply_is_valid(raw_reply)),
                        "deferred_promotions": deferred_promotions,
                    },
                )
            ],
        )
        return reply, ""

    def _stream_runtime_final_text(
        self,
        bundle,
        skill_name: str,
        assembly: PromptAssembly,
        messages: list[ChatMessage],
        client: OpenAICompatibleChatClient,
        logger: RuntimeLogger,
        context,
        *,
        phase: str,
    ) -> tuple[str, str]:
        self._emit_runtime_status(context, "response", "正在生成回复")
        self._record_runtime_prompt(
            context,
            phase=phase,
            bundle=bundle,
            skill_name=skill_name,
            assembly=assembly,
            messages=messages,
        )
        started = time.perf_counter()
        if not hasattr(client, "stream_complete"):
            turn_result = client.complete_with_tools(messages, (), preferred_mode="none", logger=logger)
            reply = _sanitize_assistant_reply(
                turn_result.final_text,
                response_policy=bundle.runtime_metadata.response_policy,
            )
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._emit_reply_delta(context, reply)
            logger.log("turn.resolve.final_text.timing", phase=phase, duration_ms=duration_ms)
            self._record_events(
                context,
                [
                    make_event(
                        "runtime_llm_timing",
                        {
                            "phase": phase,
                            "skill_id": skill_name,
                            "duration_ms": duration_ms,
                        },
                    ),
                    make_event(
                        "ms_agent_runtime",
                        {
                            "step": "llm_output",
                            "status": "success",
                            "skill_id": skill_name,
                            "detail": "runtime LLM completed final response",
                            "payload": {
                                "phase": phase,
                                "duration_ms": duration_ms,
                                "stream": False,
                            },
                        },
                    ),
                ],
            )
            self._record_runtime_prompt(
                context,
                phase=phase,
                bundle=bundle,
                skill_name=skill_name,
                assembly=assembly,
                messages=messages,
                llm_response=reply,
            )
            return reply, ""
        reply_parts: list[str] = []
        reasoning_parts: list[str] = []
        active_generation = str((context.session_meta or {}).get("active_stream_generation") or "")
        stream_kwargs = {"logger": logger}
        # Keep injected/fake clients used by existing integrations backward
        # compatible.  The production OpenAI-compatible client accepts the
        # cancellation callback and closes its upstream stream promptly.
        if "cancel_check" in inspect.signature(client.stream_complete).parameters:
            def cancel_check() -> bool:
                callback = (context.session_meta or {}).get("stream_cancel_check")
                if callable(callback):
                    try:
                        return bool(callback())
                    except Exception:
                        # Retain the durable marker as a safe fallback when a
                        # cancellation backend is temporarily unavailable.
                        pass
                return str((context.session_meta or {}).get("cancelled_stream_generation") or "") == active_generation

            stream_kwargs["cancel_check"] = cancel_check
        if "request_purpose" in inspect.signature(client.stream_complete).parameters:
            stream_kwargs["request_purpose"] = "runtime_final_response"
        for chunk in client.stream_complete(messages, **stream_kwargs):
            if chunk.reasoning_delta:
                reasoning_parts.append(chunk.reasoning_delta)
                self._emit_reasoning_delta(context, chunk.reasoning_delta)
            if chunk.content_delta:
                reply_parts.append(chunk.content_delta)
                callback = (context.session_meta or {}).get("reply_delta_callback")
                if (context.session_meta or {}).get("stream_final_reply") and callable(callback):
                    callback(chunk.content_delta)
        reply = _sanitize_assistant_reply(
            "".join(reply_parts),
            response_policy=bundle.runtime_metadata.response_policy,
        )
        reasoning = "".join(reasoning_parts).strip()
        duration_ms = int((time.perf_counter() - started) * 1000)
        empty_stream_retry = False
        if not reply.strip():
            empty_stream_retry = True
            self._record_events(
                context,
                [
                    make_event(
                        "ms_agent_runtime",
                        {
                            "step": "llm_output_empty",
                            "status": "warning",
                            "skill_id": skill_name,
                            "detail": "runtime LLM stream returned reasoning but no assistant content; retrying non-stream final response",
                            "payload": {
                                "phase": phase,
                                "stream": True,
                                "reasoning_chars": len(reasoning),
                            },
                        },
                    )
                ],
            )
            try:
                retry_result = client.complete_with_tools(messages, (), preferred_mode="none", logger=logger)
                reply = _sanitize_assistant_reply(
                    retry_result.final_text,
                    response_policy=bundle.runtime_metadata.response_policy,
                )
            except Exception as exc:  # pragma: no cover - defensive fallback
                logger.log("turn.resolve.final_text.empty_retry_failed", phase=phase, error=str(exc))
            if not reply.strip():
                reply = "刚才这轮回复生成不完整，我没有拿到可展示的正文。你可以再发一次，我会基于当前信息继续回答。"
            self._emit_reply_delta(context, reply)
        logger.log(
            "turn.resolve.final_text.stream",
            final_text_preview=reply[:240],
            reasoning_preview=reasoning[:240],
            duration_ms=duration_ms,
        )
        self._record_events(
            context,
            [
                make_event(
                    "runtime_llm_timing",
                    {
                        "phase": phase,
                        "skill_id": skill_name,
                        "duration_ms": duration_ms,
                    },
                ),
                make_event(
                    "ms_agent_runtime",
                    {
                        "step": "llm_output",
                        "status": "success",
                        "skill_id": skill_name,
                        "detail": "runtime LLM completed streamed final response",
                        "payload": {
                            "phase": phase,
                            "duration_ms": duration_ms,
                            "stream": True,
                            "reasoning_chars": len(reasoning),
                            "empty_stream_retry": empty_stream_retry,
                        },
                    },
                ),
            ],
        )
        self._record_runtime_prompt(
            context,
            phase=phase,
            bundle=bundle,
            skill_name=skill_name,
            assembly=assembly,
            messages=messages,
            llm_response=reply,
            reasoning=reasoning,
        )
        return reply, reasoning

    def _runtime_prompt_skill_catalog(self, state: SessionState):
        """Expose the configured user-facing Skill directory to general_chat."""
        active_skill_id = canonical_skill_id(state.active_skill_id)
        if active_skill_id != GENERAL_CHAT_SKILL_ID:
            return ()
        return build_runtime_skill_catalog(self.runtime_registry)

    def _messages_from_assembly(
        self,
        state: SessionState,
        assembly: PromptAssembly,
        transient_messages: tuple[ChatMessage, ...],
    ) -> list[ChatMessage]:
        prompt_messages = self._conversation_messages_for_model(state)
        return [
            ChatMessage(role="system", content=assembly.final_prompt),
            *prompt_messages,
            *transient_messages,
        ]

    def _conversation_messages_for_model(self, state: SessionState) -> list[ChatMessage]:
        memory_recent = (
            state.conversation_memory.get("recent_messages")
            if isinstance(state.conversation_memory, dict)
            and isinstance(state.conversation_memory.get("recent_messages"), list)
            else []
        )
        uncovered = [
            ChatMessage(role=str(item.get("role")), content=str(item.get("content") or ""))
            for item in memory_recent
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"}
        ]
        runtime_messages = [item for item in state.messages if item.role != "system"]
        if uncovered:
            uncovered_keys = [(item.role, item.content) for item in uncovered]
            runtime_keys = [(item.role, item.content) for item in runtime_messages]
            matched_end: int | None = None
            width = len(uncovered_keys)
            for start in range(max(0, len(runtime_keys) - width) + 1):
                if runtime_keys[start : start + width] == uncovered_keys:
                    matched_end = start + width
            if matched_end is not None:
                prompt_messages = [*uncovered, *runtime_messages[matched_end:]]
            else:
                tail = runtime_messages[-self.runtime_bridge_config.active_window_messages :]
                tail_keys = [(item.role, item.content) for item in tail]
                overlap = 0
                for size in range(min(len(uncovered_keys), len(tail_keys)), 0, -1):
                    if uncovered_keys[-size:] == tail_keys[:size]:
                        overlap = size
                        break
                prompt_messages = [*uncovered, *tail[overlap:]]
        else:
            prompt_messages = runtime_messages[-self.runtime_bridge_config.active_window_messages :]
        return prompt_messages

    def _prepare_turn_long_context(
        self,
        context,
        state: SessionState,
        active_skill_id: str,
    ) -> None:
        bundle = self.runtime_registry.get(active_skill_id) or self.main_bundle
        state.soul_context = self._soul_context()
        telemetry = current_telemetry()
        memory_logger = RuntimeLogger(
            default_log_file(self._runtime_session_file(context.session_id)),
            str(context.session_id),
            user_id=str(getattr(context, "user_id", "") or ""),
            run_id=str(telemetry.run_id if telemetry else ""),
        )
        memory_result = self.memory_store.prepare_for_turn(
            user_id=str(getattr(context, "user_id", "") or "anonymous"),
            session_id=str(getattr(context, "session_id", "") or state.session_id),
            active_skill_id=active_skill_id or MAIN_PLANNER_ID,
            skill_dir=bundle.root_dir if bundle else None,
            llm_client=self._runtime_client_for_context(context),
            logger=memory_logger,
            defer_update=self.runtime_bridge_config.memory_async_update,
        )
        memory_context = supplement_questionnaire_evidence(
            memory_result.context,
            list(getattr(context, "messages", []) or []),
        )
        state.conversation_memory = memory_context
        memory_summary = str(memory_context.get("summary") or "")
        memory_facts = memory_context.get("facts") if isinstance(memory_context.get("facts"), dict) else {}
        memory_recent = (
            memory_context.get("recent_messages")
            if isinstance(memory_context.get("recent_messages"), list)
            else []
        )
        questionnaire_evidence = (
            memory_context.get("questionnaire_evidence_messages")
            if isinstance(memory_context.get("questionnaire_evidence_messages"), list)
            else []
        )
        memory_logger.log(
            "conversation_context.composition",
            active_skill_id=active_skill_id,
            summary_chars=len(memory_summary),
            facts_chars=len(json.dumps(memory_facts, ensure_ascii=False)),
            unsummarized_message_count=len(memory_recent),
            unsummarized_chars=sum(len(str(item.get("content") or "")) for item in memory_recent if isinstance(item, dict)),
            questionnaire_evidence_message_count=len(questionnaire_evidence),
            active_window_messages=self.runtime_bridge_config.active_window_messages,
        )
        context.skill_states.setdefault(MAIN_PLANNER_ID, {})["conversation_memory"] = memory_context.get(
            "status",
            {},
        )
        self._record_events(
            context,
            [
                make_event(
                    "conversation_memory",
                    {
                        "step": memory_result.step.name,
                        "status": memory_result.step.status,
                        "detail": memory_result.step.detail,
                        "payload": memory_result.step.payload,
                    },
                )
            ],
        )

    def _append_turn_memory(
        self,
        context,
        state: SessionState,
        user_message: str,
        assistant_message: str,
    ) -> None:
        active_skill_id = str(
            context.interaction_state.get("active_skill")
            or state.active_skill_id
            or MAIN_PLANNER_ID
        )
        memory = self.memory_store.append_turn(
            user_id=str(getattr(context, "user_id", "") or "anonymous"),
            session_id=str(getattr(context, "session_id", "") or state.session_id),
            active_skill_id=active_skill_id,
            user_message=user_message,
            assistant_message=assistant_message,
        )
        context.skill_states.setdefault(MAIN_PLANNER_ID, {})["conversation_memory"] = {
            "total_messages": len(memory.get("messages") or []),
            "memory_update_status": memory.get("memory_update_status") or "idle",
            "last_memory_updated_at": memory.get("last_memory_updated_at"),
            "runtime_contract_hash": memory.get("runtime_contract_hash"),
            "runtime_contract_available": bool(memory.get("runtime_contract_available")),
        }

    def _soul_context(self) -> dict[str, Any]:
        path = self.runtime_bridge_config.soul_path
        if not path.is_file():
            return {"content": "", "content_hash": None, "available": False, "path": str(path)}
        content = path.read_text(encoding="utf-8", errors="replace")
        if not content.strip():
            return {"content": "", "content_hash": None, "available": False, "path": str(path)}
        return {
            "content": content,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "available": True,
            "path": str(path),
        }

    def handle_message(self, user_message: str, context) -> SkillResult:
        """Run the default Expert, retaining the existing API/result contract."""
        return self.expert_runtime.handle_message(
            user_message,
            context,
            self._handle_message_legacy,
        )

    def _handle_message_legacy(self, user_message: str, context) -> SkillResult:
        self._normalize_planner_state_alias(context)
        turn_id = f"turn_{uuid4().hex[:12]}"
        context.session_meta["active_turn_id"] = turn_id
        context.session_meta.pop("skill_intro", None)
        if self.moderation_service is not None:
            try:
                moderation_result = self.moderation_service.check(
                    user_message,
                    stage="input",
                    trace_id=str(context.session_meta.get("trace_id") or ""),
                    session_id=str(context.session_id),
                    turn_id=turn_id,
                )
                context.session_meta["moderation_mode"] = moderation_result.mode
                context.session_meta["semantic_moderation_unavailable"] = moderation_result.mode == "local_fallback"
                from hailiang_skills.core.conversation_state import record_security_result
                record_security_result(context, stage="input", result=moderation_result)
                if moderation_result.mode == "local_fallback":
                    self._record_events(
                        context,
                        [make_event("security_degraded", {"stage": "input", **moderation_result.to_public_dict()})],
                    )
            except Exception as exc:
                from hailiang_skills.security.models import ModerationBlockedError

                if isinstance(exc, ModerationBlockedError):
                    context.session_meta["moderation_mode"] = exc.result.mode
                    context.session_meta["semantic_moderation_unavailable"] = exc.result.mode == "local_fallback"
                    from hailiang_skills.core.conversation_state import record_security_result
                    record_security_result(context, stage="input", result=exc.result, case_id=exc.case_id)
                    self._record_events(
                        context,
                        [make_event("moderation_blocked", {"stage": "input", "case_id": exc.case_id, **exc.result.to_public_dict()})],
                    )
                raise
        generation_by_thread = context.session_meta.get("stream_generation_by_thread")
        if isinstance(generation_by_thread, dict):
            stream_generation = generation_by_thread.get(str(threading.get_ident()))
            if stream_generation:
                turn_by_generation = context.session_meta.setdefault("turn_id_by_stream_generation", {})
                if isinstance(turn_by_generation, dict):
                    turn_by_generation[stream_generation] = turn_id
        user_metadata = {"turn_id": turn_id}
        if context.session_meta.pop("hide_next_user_message", False):
            user_metadata["hidden"] = True
            user_metadata["message_type"] = "skill_transition_command"
        visible_user_message = str(
            context.session_meta.pop("team_handoff_visible_user_message", "") or user_message
        )
        context.add_message("user", visible_user_message, metadata=user_metadata)
        context.session_meta["active_turn_index"] = sum(
            1 for item in context.messages if item.get("role") == "user"
        )
        # general_chat deliberately has no planning scenario.  Initialising the
        # legacy scenario engine here used to write admission_simulation before
        # the user had selected any Skill.
        persisted_runtime = context.skill_states.get(RUNTIME_STATE_KEY, {})
        persisted_skill = str(persisted_runtime.get("active_skill_id") or "") if isinstance(persisted_runtime, dict) else ""
        active_before_turn = str((context.interaction_state or {}).get("active_skill") or persisted_skill or GENERAL_CHAT_ID)
        if active_before_turn not in {GENERAL_CHAT_ID, EXPERT_DIRECT_EXECUTION_ID}:
            self._record_events(context, self.scenario_engine.ensure_context_initialized(context))

        runtime_state = self._load_runtime_state(context)
        runtime_state.messages = self._runtime_messages_from_context(context)
        sync_context_to_runtime_state(context, runtime_state)
        expert_direct = context.session_meta.pop("expert_direct_reply", None)
        if isinstance(expert_direct, dict) and str(expert_direct.get("reply") or "").strip():
            # AgentScope already produced the role-bounded final answer.  Do
            # not send the same turn through general_chat, whose legacy soul
            # prompt is intentionally broad and may contradict this expert.
            reply = str(expert_direct["reply"]).strip()
            expert_id = str(expert_direct.get("expert_id") or "")
            # This response was generated by the selected Expert Agent, not
            # by the legacy general_chat Skill. Preserve that distinction in
            # state, trace, memory, and the UI. ``expert_direct`` never takes
            # part in Skill routing; it is only an execution-source marker.
            runtime_state.active_skill_id = EXPERT_DIRECT_EXECUTION_ID
            context.add_message(
                "assistant",
                reply,
                metadata=self._assistant_message_metadata(context, EXPERT_DIRECT_EXECUTION_ID),
            )
            self._emit_reply_delta(context, reply)
            runtime_state.messages.append(ChatMessage(role="assistant", content=reply))
            self._persist_runtime_state(context, runtime_state)
            context.interaction_state["active_skill"] = EXPERT_DIRECT_EXECUTION_ID
            self._record_events(
                context,
                [
                    make_event(
                        "expert_direct_response",
                        {
                            "expert_id": expert_id,
                            "active_skill": EXPERT_DIRECT_EXECUTION_ID,
                            "execution_mode": "expert_direct",
                            "message_preview": reply[:200],
                        },
                    )
                ],
            )
            result = SkillResult(
                assistant_message=reply,
                state_patch={
                    "runtime_active_skill_id": EXPERT_DIRECT_EXECUTION_ID,
                    "expert_id": expert_id,
                    "execution_mode": "expert_direct",
                },
                events=[],
            )
            self._append_turn_memory(context, runtime_state, user_message, reply)
            return result
        active_bundle = self.runtime_registry.get(runtime_state.active_skill_id)
        if runtime_state.active_skill_id and active_bundle is None:
            runtime_state.active_skill_id = GENERAL_CHAT_ID
        if runtime_state.active_skill_id == "multi_path_planning":
            # A new user turn invalidates the previous round's path cards. The
            # native response below must repopulate matched_paths for this turn.
            runtime_state.skill_facts.setdefault("multi_path_planning", {})["matched_paths"] = []
        submitted_answer: dict[str, Any] | None = None
        if active_bundle is not None:
            submitted_answer = consume_pending_questionnaire_answer(
                runtime_state,
                context,
                active_bundle,
                user_message,
            )
            if submitted_answer:
                sync_context_to_runtime_state(context, runtime_state)
                self._record_events(
                    context,
                    [
                        make_event(
                            "native_questionnaire_answered",
                            {
                                "skill_id": runtime_state.active_skill_id,
                                "question_id": submitted_answer["question_id"],
                                "question_ids": submitted_answer["question_ids"],
                                "promotions": submitted_answer["promotions"],
                            },
                        )
                    ],
                )

        self._emit_runtime_status(context, "intent", "意图判断")
        if self.runtime_registry.is_enabled(MAIN_PLANNER_ID):
            active_skill_id = self._route_with_main_planner(runtime_state, context)
        else:
            # A disabled main Skill must not invoke keyword, embedding or LLM
            # routing. An already active enabled specialist may continue;
            # otherwise use the legacy general-chat fallback.
            active_skill_id = (
                runtime_state.active_skill_id
                if runtime_state.active_skill_id not in {"", MAIN_PLANNER_ID}
                and self.runtime_registry.is_enabled(runtime_state.active_skill_id)
                else GENERAL_CHAT_ID
            )
            runtime_state.active_skill_id = active_skill_id
        self._prepare_turn_long_context(context, runtime_state, active_skill_id)
        is_explicit_skill_entry = (
            canonical_skill_id(runtime_state.status_flags.get("entry_skill_id"))
            == canonical_skill_id(active_skill_id)
            and active_skill_id != GENERAL_CHAT_ID
        )
        if not is_explicit_skill_entry:
            self._emit_runtime_status(context, "planner", "推理规划")
        self._emit_skill_intro_if_needed(context, runtime_state, active_skill_id)
        if runtime_state.status_flags.get("awaiting_school_stage_for_multi_path"):
            result = self._respond_need_grade_for_multi_path(context, runtime_state)
            self._append_turn_memory(context, runtime_state, user_message, result.assistant_message)
            return result
        target = HAILIANG_TARGETS.get(active_skill_id)
        if target and active_skill_id in self.runtime_bridge_config.legacy_bridge_skill_ids:
            result = self._run_hailiang_target(user_message, context, target, turn_id)
            runtime_state.messages = self._runtime_messages_from_context(context)
            sync_context_to_runtime_state(context, runtime_state)
            self._persist_runtime_state(context, runtime_state)
            self._record_events(
                context,
                [
                    make_event(
                        "main_planner_bridge",
                        {
                            "runtime_active_skill_id": active_skill_id,
                            "hailiang_skill": target["skill"],
                            "scene": target["scene"],
                        },
                    )
                ],
            )
            self._append_turn_memory(context, runtime_state, user_message, result.assistant_message)
            return result

        context.skill_states["planner"] = {
            "target_skill": active_skill_id,
            "scenario_id": context.interaction_state.get("current_scenario"),
            "phase_id": runtime_state.stage,
            "response_mode": "answer",
            "missing_facts": [],
            "focus_points": [],
            "should_ask_question": False,
            "question_hint": "",
            "missing_fact_form": None,
        }
        if active_skill_id == GENERAL_CHAT_ID and not self.runtime_registry.is_enabled(MAIN_PLANNER_ID):
            result = self._run_hailiang_fallback(user_message, context, turn_id)
            self._persist_runtime_state(context, runtime_state)
            self._append_turn_memory(context, runtime_state, user_message, result.assistant_message)
            return result
        logger = RuntimeLogger(
            default_log_file(self._runtime_session_file(context.session_id)),
            context.session_id,
        )
        current_bundle = self.runtime_registry.get(runtime_state.active_skill_id) or self.main_bundle
        current_skill_state = runtime_state.skill_facts.get(runtime_state.active_skill_id, {})
        pending_questionnaire = (
            current_skill_state.get("_pending_questionnaire")
            if isinstance(current_skill_state, dict)
            else None
        )
        questionnaire_result: tuple[str, str] | None = None
        entry_result: tuple[str, str] | None = None
        if questionnaire_enabled(current_bundle) and (
            submitted_answer is not None or not isinstance(pending_questionnaire, dict)
        ):
            questionnaire_result = self._resolve_lightweight_questionnaire(
                current_bundle,
                runtime_state,
                logger,
                context,
                user_message,
            )
        elif is_explicit_skill_entry:
            entry_result = self._resolve_lightweight_skill_entry(
                current_bundle,
                runtime_state,
                logger,
                context,
            )
        runtime_client = self._runtime_client_for_context(context)
        if questionnaire_result is None and entry_result is None and runtime_client is None:
            if current_bundle.runtime_metadata.skill_type == "native":
                active_skill_name = current_bundle.contract.skill_id or current_bundle.root_name
                if not self.ms_agent_probe.available:
                    detail = self.ms_agent_probe.error or self.ms_agent_probe.status
                    reply = (
                        "当前 Runtime Skill 底层依赖 ms-agent，但运行环境尚不可用，"
                        f"因此无法执行 `{active_skill_name}`。请先安装并配置 ms-agent 后重试。"
                    )
                    status = self.ms_agent_probe.status
                    step = "runtime_health"
                else:
                    detail = "runtime LLM client is unavailable"
                    reply = (
                        f"已路由到 `{active_skill_name}`，说明海亮主 Agent 已经发现并命中该 Skill。"
                        "但当前未配置可用的模型客户端，无法继续生成该 Skill 的正式回复。"
                        "请配置 DASHSCOPE_API_KEY 后重启后端再测试完整 ms-agent 运行链路。"
                    )
                    status = "missing_llm_client"
                    step = "llm_config"
                context.add_message(
                    "assistant",
                    reply,
                    metadata=self._assistant_message_metadata(context, active_skill_name),
                )
                self._emit_reply_delta(context, reply)
                self._record_events(
                    context,
                    [
                        make_event(
                            "ms_agent_runtime",
                            {
                                "step": step,
                                "status": status,
                                "skill_id": active_skill_name,
                                "error": detail,
                            },
                        )
                    ],
                )
                self._persist_runtime_state(context, runtime_state)
                context.interaction_state["active_skill"] = runtime_state.active_skill_id or GENERAL_CHAT_ID
                result = SkillResult(
                    assistant_message=reply,
                    state_patch={"runtime_active_skill_id": runtime_state.active_skill_id},
                    events=[],
                )
                self._append_turn_memory(context, runtime_state, user_message, result.assistant_message)
                return result
            result = self._run_hailiang_fallback(user_message, context, turn_id)
            self._persist_runtime_state(context, runtime_state)
            self._append_turn_memory(context, runtime_state, user_message, result.assistant_message)
            return result

        if questionnaire_result is not None:
            reply, reasoning = questionnaire_result
        elif entry_result is not None:
            reply, reasoning = entry_result
        else:
            assert runtime_client is not None
            reply, reasoning = self._resolve_runtime_reply(
                self.main_bundle,
                runtime_state,
                runtime_client,
                logger,
                context,
            )
        native_path_items = resolve_native_path_options(
            runtime_state.active_skill_id,
            runtime_state.stage,
            reply,
            runtime_state.skill_facts.get(runtime_state.active_skill_id, {}).get("matched_paths", []),
        )
        if runtime_state.active_skill_id == "multi_path_planning":
            # The streaming/API layers build path_actions from the result, but
            # citations and the persisted snapshot read the context directly.
            context.candidate_paths = native_path_items
        if native_path_items:
            self._record_events(
                context,
                [
                    make_event(
                        "native_path_options_created",
                        {
                            "skill_id": runtime_state.active_skill_id,
                            "source": (
                                "state_patch"
                                if runtime_state.skill_facts.get(runtime_state.active_skill_id, {}).get("matched_paths")
                                else "reply_text_fallback"
                            ),
                            "path_ids": [item["path_id"] for item in native_path_items],
                            "path_names": [item["primary_category"] for item in native_path_items],
                        },
                    )
                ],
            )
        context.add_message(
            "assistant",
            reply,
            metadata=self._assistant_message_metadata(context, runtime_state.active_skill_id or GENERAL_CHAT_ID),
        )
        question_block = attach_staged_questionnaire_form(context, runtime_state)
        if question_block:
            context.skill_states.setdefault(AGENT_RUNTIME_STATE_KEY, {})["pending_form"] = {
                "skill_id": runtime_state.active_skill_id,
                "form_id": question_block.get("payload", {}).get("form_id"),
            }
            self._record_events(
                context,
                [
                    make_event(
                        "native_questionnaire_form_created",
                        {
                            "skill_id": runtime_state.active_skill_id,
                            "form_id": question_block.get("payload", {}).get("form_id"),
                        },
                    )
                ],
            )
        runtime_state.messages.append(ChatMessage(role="assistant", content=reply))
        current_bundle = self.runtime_registry.get(runtime_state.active_skill_id) or self.main_bundle
        run_status_hook_if_present(current_bundle, runtime_state, logger=logger)
        sync_runtime_state_to_context(context, runtime_state)
        self._persist_runtime_state(context, runtime_state)
        context.skill_states.setdefault(MAIN_PLANNER_ID, {}).update(
            {
                "thinking": self._runtime_thinking_options(context),
                "last_reasoning": reasoning,
            }
        )
        context.interaction_state["active_skill"] = runtime_state.active_skill_id or GENERAL_CHAT_ID
        self._record_events(
            context,
            [
                make_event(
                    "assistant_response",
                    {
                        "active_skill": context.interaction_state["active_skill"],
                        "current_scenario": context.interaction_state.get("current_scenario"),
                        "message_preview": reply[:200],
                    },
                )
            ],
        )
        result = SkillResult(
            assistant_message=reply,
            state_patch={
                "runtime_active_skill_id": runtime_state.active_skill_id,
                **(
                    {"matched_paths": [item["path_id"] for item in native_path_items]}
                    if runtime_state.active_skill_id == "multi_path_planning"
                    else {}
                ),
            },
            candidate_paths=(native_path_items if runtime_state.active_skill_id == "multi_path_planning" else None),
            suggested_paths=(
                [item["primary_category"] for item in native_path_items]
                if runtime_state.active_skill_id == "multi_path_planning"
                else []
            ),
            events=[],
        )
        self._append_turn_memory(context, runtime_state, user_message, result.assistant_message)
        return result

    def _emit_skill_intro_if_needed(self, context, state: SessionState, active_skill_id: str) -> None:
        entry_skill_id = str(state.status_flags.pop("entry_skill_id", "") or "").strip()
        if not entry_skill_id or entry_skill_id in {MAIN_PLANNER_ID, GENERAL_CHAT_ID}:
            return
        previous_skill_id = str(state.status_flags.pop("entry_from_skill_id", "") or "").strip()
        if previous_skill_id == entry_skill_id:
            return
        bundle = self.runtime_registry.get(active_skill_id or entry_skill_id)
        if bundle is None:
            return
        display = build_skill_display(
            context,
            active_skill=active_skill_id or entry_skill_id,
            runtime_registry=self.runtime_registry,
        )
        # Use the same normalized display metadata as the SSE context so
        # imported Skills with partial frontmatter still get a useful intro.
        brief = display.get("brief", "")
        info = display.get("info", "")
        description = display.get("description", "")
        intro = {
            "skill_id": active_skill_id or entry_skill_id,
            "skill_label": display["agent_label"],
            "brief": brief,
            "info": info,
            "description": description,
            "scene_name": display["scene_name"],
            "skill_theme": display["skill_theme"],
        }
        context.session_meta["skill_intro"] = intro
        context.add_message(
            "assistant",
            info,
            metadata={
                **self._assistant_message_metadata(context, active_skill_id or entry_skill_id),
                "message_type": "skill_intro",
                "skill_intro": intro,
            },
        )
        callback = (context.session_meta or {}).get("skill_intro_callback")
        if callable(callback):
            callback(intro)

    def _respond_need_grade_for_multi_path(self, context, runtime_state: SessionState) -> SkillResult:
        reply = (
            "先确认一下孩子现在处于哪个学段，我再接到对应的多元路径规划。"
            "可以直接告诉我几年级，比如 `初二`、`初三`、`高一`、`高二`。"
        )
        planner_state = {
            "target_skill": "multi_path_planning",
            "scenario_id": "multi_path_planning",
            "phase_id": "collect",
            "response_mode": "ask_followup",
            "missing_facts": ["grade"],
            "focus_points": ["先确认学段，再进入对应的多元路径 skill"],
            "should_ask_question": True,
            "question_hint": "请先告诉我孩子现在几年级或处于小学 / 初中 / 高中哪个学段。",
            "missing_fact_form": build_missing_fact_form_block(["grade"]),
        }
        context.skill_states["planner"] = planner_state
        active_skill_id = runtime_state.active_skill_id or "multi_path_planning"
        context.add_message("assistant", reply, metadata=self._assistant_message_metadata(context, active_skill_id))
        context.interaction_state["active_skill"] = active_skill_id
        if not self._has_streamed_reply(context):
            self._emit_reply_delta(context, reply)
        self._persist_runtime_state(context, runtime_state)
        self._record_events(
            context,
            [
                make_event(
                    "assistant_response",
                    {
                        "active_skill": active_skill_id,
                        "current_scenario": context.interaction_state.get("current_scenario"),
                        "message_preview": reply[:200],
                    },
                )
            ],
        )
        return SkillResult(
            assistant_message=reply,
            state_patch={"runtime_active_skill_id": runtime_state.active_skill_id},
            events=[],
        )

    def _run_hailiang_target(
        self,
        user_message: str,
        context,
        target: dict[str, str],
        turn_id: str,
    ) -> SkillResult:
        self._apply_legacy_llm_options(context)
        self._record_events(
            context,
            self.scenario_engine.apply_scenario_switch(
                context,
                target["scenario"],
                "skill_runtime_main_planner",
            ),
        )
        context.interaction_state["current_phase"] = target["phase"]
        context.skill_states.setdefault("router", {}).update(
            {
                "target_skill": target["skill"],
                "target_scenario": target["scenario"],
                "confidence": 0.92,
                "reason": "skill-runtime main_planner routed to hailiang scene",
            }
        )

        facts_extractor = self.registry.get("facts_extractor")
        facts_result = facts_extractor.run(user_message, context)
        self._record_prompt_assembly_from_skill(context, facts_extractor)
        self._record_events(context, facts_result.events)
        if facts_result.state_patch:
            context.skill_states.setdefault("facts_extractor", {}).update(facts_result.state_patch)
            for key, value in facts_result.state_patch.get("fact_updates", {}).items():
                if value not in (None, "", [], {}):
                    context.update_fact(
                        key,
                        value,
                        source_skill="facts_extractor",
                        confidence=facts_result.state_patch.get("confidence", 0.8),
                        source_turn_id=turn_id,
                    )

        planner = self.registry.get("planner")
        planner_result = planner.run(user_message, context)
        self._record_prompt_assembly_from_skill(context, planner)
        self._record_events(context, planner_result.events)
        planner_state = dict(planner_result.state_patch or {})
        planner_state["target_skill"] = target["skill"]
        planner_state["scenario_id"] = target["scenario"]
        planner_state["phase_id"] = target["phase"]
        context.skill_states["planner"] = planner_state

        self._record_events(context, self.scenario_engine.apply_skill(context, target["skill"]))
        context.interaction_state["current_phase"] = target["phase"]
        self._emit_runtime_status(context, "response", "正在生成回复")
        skill = self.registry.get(target["skill"])
        result = skill.run(user_message, context)
        result.assistant_message = self._with_fact_summary(result.assistant_message, context)
        if not self._has_streamed_reply(context):
            self._emit_reply_delta(context, result.assistant_message)
        self._record_prompt_assembly_from_skill(context, skill)
        context.interaction_state["active_skill"] = skill.skill_name
        context.add_message(
            "assistant",
            result.assistant_message,
            metadata=self._assistant_message_metadata(context, skill.skill_name),
        )
        context.interaction_state["last_non_terminal_skill"] = skill.skill_name
        self._record_events(context, result.events)
        self._record_events(
            context,
            [
                make_event(
                    "assistant_response",
                    {
                        "active_skill": skill.skill_name,
                        "current_scenario": context.interaction_state.get("current_scenario"),
                        "current_phase": context.interaction_state.get("current_phase"),
                        "message_preview": result.assistant_message[:200],
                    },
                )
            ],
        )
        if result.state_patch:
            context.skill_states.setdefault(skill.skill_name, {}).update(result.state_patch)
        if result.candidate_paths is not None:
            context.candidate_paths = result.candidate_paths
        if result.risk_alerts:
            context.risk_signals = result.risk_alerts
        return result

    def _with_fact_summary(self, assistant_message: str, context) -> str:
        province = context.known_facts.get_value("student_province")
        subject_group = context.known_facts.get_value("subject_group")
        score = context.known_facts.get_value("score_total")
        summary_parts = []
        if province not in (None, "", [], {}):
            summary_parts.append(str(province))
        if subject_group not in (None, "", [], {}):
            summary_parts.append(str(subject_group))
        if score not in (None, "", [], {}):
            summary_parts.append(f"{score} 分")
        if not summary_parts:
            return assistant_message
        summary = f"已基于：{' / '.join(summary_parts)}。\n\n"
        if all(part in assistant_message for part in [str(province or ""), str(score or "")]):
            return assistant_message
        return f"{summary}{assistant_message}"

    def _run_hailiang_fallback(self, user_message: str, context, turn_id: str) -> SkillResult:
        del turn_id
        self._apply_legacy_llm_options(context)
        fallback_skill = self.registry.get("chat")
        result = fallback_skill.run(user_message, context)
        if not self._has_streamed_reply(context):
            self._emit_reply_delta(context, result.assistant_message)
        self._record_prompt_assembly_from_skill(context, fallback_skill)
        context.interaction_state["active_skill"] = fallback_skill.skill_name
        context.add_message(
            "assistant",
            result.assistant_message,
            metadata=self._assistant_message_metadata(context, fallback_skill.skill_name),
        )
        self._record_events(context, result.events)
        return result

    def _route_with_main_planner(self, state: SessionState, context) -> str:
        ensure_runtime_state(state, self.main_bundle)
        # AgentScope's execute_skill tool only writes an authorized selection
        # hint.  Hailiang still owns state, fact_form and native Skill
        # execution; therefore a stale or unauthorized hint can never route a
        # request outside the active Expert Bundle.
        expert_state = context.skill_states.get(AGENT_RUNTIME_STATE_KEY, {})
        expert_selected = str(context.session_meta.pop("expert_requested_skill_id", "") or "")
        expert_id = str(expert_state.get("expert_id") or DEFAULT_EXPERT_ID)
        expert_definition = self.expert_registry.get(expert_id)
        authorized = bool(expert_definition) and expert_selected in set(expert_definition.authorized_skill_ids)
        if authorized and self.runtime_registry.is_enabled(expert_selected):
            state.active_skill_id = expert_selected
            state.status_flags["expert_selected_skill_id"] = expert_selected
            context.skill_states.setdefault(MAIN_PLANNER_ID, {})["intent_route"] = {
                "route_mode": "expert",
                "target_skill_id": expert_selected,
                "reason": "AgentScope 专家已在授权范围内选择该 Skill。",
                "source": "agentscope",
            }
            self._record_events(
                context,
                [make_event("expert_skill_selected", {"expert_id": expert_id, "skill_id": expert_selected})],
            )
            self._split_multi_path_skill_by_stage(state, context)
            return state.active_skill_id or GENERAL_CHAT_ID
        entry_skill_id = state.active_skill_id or GENERAL_CHAT_ID
        is_entry_skill = entry_skill_id in {MAIN_PLANNER_ID, GENERAL_CHAT_ID}
        # ``career_plan_entity`` is both the public career-consultant Skill
        # and the runtime's historical main-planner bundle.  A user who got
        # here by explicitly choosing its route-suggestion card has entered a
        # specialist consultation, so generic follow-ups (for example
        # "你好") must remain in that Skill.  Without this distinction the
        # main-planner fallback below treats the follow-up as a fresh general
        # chat request and overwrites the selected Skill with general_chat.
        entered_from_skill = canonical_skill_id(
            state.status_flags.get("explicitly_selected_from_skill_id")
        )
        is_explicit_career_consultation = (
            entry_skill_id == CAREER_PLAN_SKILL_ID
            and canonical_skill_id(state.status_flags.get("explicitly_selected_skill_id"))
            == CAREER_PLAN_SKILL_ID
            and bool(entered_from_skill)
            and entered_from_skill != CAREER_PLAN_SKILL_ID
        )
        intent_update = track_user_intent(self.main_bundle, state)
        apply_intent_update(state, intent_update)
        requested_skill_id = self._apply_requested_target_skill_if_present(state, context)
        if requested_skill_id:
            self._split_multi_path_skill_by_stage(state, context)
            return requested_skill_id
        latest_user = next((item.content for item in reversed(state.messages) if item.role == "user"), "")
        if entry_skill_id != GENERAL_CHAT_ID and state.status_flags.get("awaiting_school_stage_for_multi_path"):
            state.active_skill_id = "multi_path_planning"
            state.status_flags["pending_route_scene"] = ""
            self._split_multi_path_skill_by_stage(state, context)
            return state.active_skill_id or MAIN_PLANNER_ID
        # The classifier is advisory while the user is in general_chat.  It
        # may provide ranked candidates to the general-chat model later in the
        # turn, but it must not mutate the session's active Skill or scenario.
        routing_state = replace(state, active_skill_id=MAIN_PLANNER_ID) if entry_skill_id == GENERAL_CHAT_ID else state
        route_decision = self.intent_router.route(latest_user, routing_state)
        self._emit_router_error_if_present(context)
        if entry_skill_id == GENERAL_CHAT_ID:
            candidates = () if is_short_contextual_reply(latest_user) else route_decision.candidate_skills
            if candidates:
                first_candidate = candidates[0]
                route_decision = replace(
                    route_decision,
                    route_mode="recommend_switch",
                    target_skill_id=str(first_candidate.get("target_skill_id") or ""),
                    confidence=float(first_candidate.get("confidence") or route_decision.confidence),
                    scene_name=str(first_candidate.get("scene_name") or ""),
                    intent_clear=False,
                    requires_user_choice=True,
                    reason="路由候选仅供 general_chat 生成建议卡片时参考，等待用户明确点击。",
                    candidate_skills=tuple(candidates),
                )
            else:
                route_decision = replace(
                    route_decision,
                    route_mode="stay",
                    target_skill_id=GENERAL_CHAT_ID,
                    intent_clear=False,
                    requires_user_choice=False,
                    reason="general_chat 保持当前场景；文本路由不会自动切换 Skill。",
                    candidate_skills=(),
                )
            context.skill_states.setdefault(MAIN_PLANNER_ID, {})["intent_route"] = route_decision.as_dict()
            state.active_skill_id = GENERAL_CHAT_ID
            return GENERAL_CHAT_ID
        if not is_entry_skill and route_decision.target_skill_id != state.active_skill_id:
            if (
                route_decision.target_skill_id not in {MAIN_PLANNER_ID, GENERAL_CHAT_ID}
                and route_decision.confidence >= CROSS_SKILL_SUGGESTION_MIN_CONFIDENCE
                and not is_short_contextual_reply(latest_user)
            ):
                route_decision = replace(
                    route_decision,
                    route_mode="recommend_switch",
                    intent_clear=False,
                    requires_user_choice=True,
                    reason="检测到可能适合其他专项 Skill，请点击本轮回复下方按钮确认进入。",
                )
            else:
                route_decision = replace(
                    route_decision,
                    route_mode="stay",
                    target_skill_id=state.active_skill_id or MAIN_PLANNER_ID,
                    intent_clear=False,
                    requires_user_choice=False,
                    reason="当前专项 Skill 场景锁生效，文本不会自动切换场景。",
                )
        if is_explicit_career_consultation and route_decision.target_skill_id == GENERAL_CHAT_ID:
            # An explicitly selected consultation can only be exited by the
            # dedicated exit action. Never let an uncertain router fallback
            # silently turn an in-skill conversation back into general_chat.
            route_decision = replace(
                route_decision,
                route_mode="stay",
                target_skill_id=CAREER_PLAN_SKILL_ID,
                intent_clear=False,
                requires_user_choice=False,
                reason="用户已显式进入升学规划顾问，保持当前咨询领域。",
            )
        if is_entry_skill:
            if (
                entry_skill_id == MAIN_PLANNER_ID
                and not is_explicit_career_consultation
                and route_decision.target_skill_id == MAIN_PLANNER_ID
                and not _looks_like_planning_request(latest_user)
            ):
                route_decision = replace(
                    route_decision,
                    route_mode="switch",
                    target_skill_id=GENERAL_CHAT_ID,
                    intent_clear=False,
                    reason="用户问题未体现明确的升学规划诉求，交由自由问答处理。",
                )
            if route_decision.target_skill_id not in {MAIN_PLANNER_ID, GENERAL_CHAT_ID}:
                route_decision = replace(
                    route_decision,
                    route_mode="recommend_switch",
                    requires_user_choice=True,
                    reason=f"识别到用户可能需要「{route_decision.scene_name or route_decision.target_skill_id}」，等待用户点击进入按钮。",
                )
        context.skill_states.setdefault(MAIN_PLANNER_ID, {})["intent_route"] = route_decision.as_dict()
        self._apply_intent_route_decision(state, context, route_decision)
        self._split_multi_path_skill_by_stage(state, context)
        return state.active_skill_id or GENERAL_CHAT_ID

    def _apply_explicit_scene_route_if_present(
        self,
        state: SessionState,
        context,
        user_message: str,
    ) -> str:
        scene = self._match_explicit_scene_name_from_registry(user_message, state=state, context=context)
        if not scene:
            return ""
        target_skill_id = self._target_skill_for_scene(scene, state, context)
        if not target_skill_id or not self.runtime_registry.get(target_skill_id):
            return ""
        display = build_skill_display(context, active_skill=target_skill_id, runtime_registry=self.runtime_registry)
        route_decision = IntentRouteDecision(
            route_mode="direct" if (state.active_skill_id or MAIN_PLANNER_ID) == MAIN_PLANNER_ID else "switch",
            target_skill_id=target_skill_id,
            confidence=0.98,
            intent_clear=True,
            reason=f"用户明确提到已注册子 Skill 场景：{scene}。",
            matched_examples=(scene,),
            requires_user_choice=False,
            scene_name=display.get("scene_name") or scene,
        )
        context.skill_states.setdefault(MAIN_PLANNER_ID, {})["intent_route"] = route_decision.as_dict()
        self._apply_intent_route_decision(state, context, route_decision)
        return state.active_skill_id or MAIN_PLANNER_ID

    def _match_explicit_scene_name_from_registry(self, user_message: str, *, state: SessionState, context) -> str:
        normalized = str(user_message or "").strip()
        if not normalized:
            return ""
        stage_label = self._profile_stage_label(user_message, state, context)
        candidates: list[tuple[str, str]] = []
        for route in self.main_bundle.contract.routes:
            candidates.append((route.scene, route.target_skill_id))
        for skill_id, bundle in self.runtime_registry.enabled_bundles().items():
            if skill_id == MAIN_PLANNER_ID:
                continue
            for scene in (*bundle.contract.accepts_scenes, *bundle.runtime_metadata.accepts_scenes):
                candidates.append((str(scene), skill_id))
        best_scene = ""
        best_skill_id = ""
        for scene, skill_id in candidates:
            scene = str(scene or "").strip()
            if not scene or scene not in normalized:
                continue
            # Short generic scene words such as "提分" are valid router signals, but too broad for
            # unconditional pre-router switching inside profile collection turns.
            if len(scene) < 4 and scene not in {"命理"}:
                continue
            if len(scene) > len(best_scene):
                best_scene = scene
                best_skill_id = skill_id
        if not best_scene or not best_skill_id:
            return ""
        return self._canonical_scene_for_skill(best_skill_id, stage_label=stage_label) or best_scene

    def _apply_pending_route_scene_if_selected(
        self,
        state: SessionState,
        context,
        user_message: str,
        pending_scene: str,
    ) -> str:
        pending_scene = str(pending_scene or "").strip()
        if not pending_scene or not self._looks_like_pending_scene_selection(user_message, pending_scene):
            return ""
        target_skill_id = self._target_skill_for_scene(pending_scene, state, context)
        if not target_skill_id or not self.runtime_registry.get(target_skill_id):
            return ""
        display = build_skill_display(context, active_skill=target_skill_id, runtime_registry=self.runtime_registry)
        route_decision = IntentRouteDecision(
            route_mode="direct" if (state.active_skill_id or MAIN_PLANNER_ID) == MAIN_PLANNER_ID else "switch",
            target_skill_id=target_skill_id,
            confidence=0.98,
            intent_clear=True,
            reason=f"用户确认 main_planner 推荐方向：{pending_scene}。",
            matched_examples=(pending_scene,),
            requires_user_choice=False,
            scene_name=display.get("scene_name") or pending_scene,
        )
        context.skill_states.setdefault(MAIN_PLANNER_ID, {})["intent_route"] = route_decision.as_dict()
        self._apply_intent_route_decision(state, context, route_decision)
        return state.active_skill_id or MAIN_PLANNER_ID

    def _looks_like_pending_scene_selection(self, user_message: str, pending_scene: str) -> bool:
        normalized = re.sub(r"[\s，。！？、,.!?：:；;\"“”'‘’（）()【】\\[\\]]+", "", str(user_message or ""))
        if not normalized:
            return False
        scene = re.sub(r"[\s，。！？、,.!?：:；;\"“”'‘’（）()【】\\[\\]]+", "", pending_scene)
        if scene and scene in normalized:
            return True
        if _is_multi_path_scene(pending_scene):
            return any(
                keyword in normalized
                for keyword in (
                    "初中多元路径",
                    "初中多元",
                    "多元路径",
                    "多元升学",
                    "升学路径",
                    "其他升学路径",
                    "别的升学路",
                )
            )
        return False

    def _target_skill_for_scene(self, scene: str, state: SessionState, context) -> str:
        if _is_multi_path_scene(scene):
            inferred_stage = self._infer_school_stage(context, state)
            if inferred_stage == "junior" or "初中" in scene:
                return JUNIOR_MULTI_PATH_SKILL_ID
            if inferred_stage == "senior":
                return "multi_path_planning"
            return "multi_path_planning"
        for route in self.main_bundle.contract.routes:
            if route.scene == scene:
                return route.target_skill_id
        matched_scene = self._match_scene_from_registry(scene, state=state, context=context)
        if matched_scene and matched_scene != scene:
            return self._target_skill_for_scene(matched_scene, state, context)
        return ""

    def _apply_requested_target_skill_if_present(self, state: SessionState, context) -> str:
        requested = canonical_skill_id((context.session_meta or {}).pop("requested_target_skill_id", ""))
        handoff_context = (context.session_meta or {}).pop("handoff_context", {}) or {}
        handoff_payload = (
            handoff_context.get("handoff_context")
            if isinstance(handoff_context, dict) and isinstance(handoff_context.get("handoff_context"), dict)
            else handoff_context
        )
        preactivated = canonical_skill_id(
            (context.session_meta or {}).pop("preactivated_requested_target_skill_id", "")
        )
        preactivated_from = canonical_skill_id(
            (context.session_meta or {}).pop("preactivated_previous_skill_id", "")
        )
        if not requested:
            return ""
        if requested == state.active_skill_id:
            if preactivated == requested and preactivated_from != requested:
                state.status_flags["entry_skill_id"] = requested
                state.status_flags["entry_from_skill_id"] = preactivated_from or MAIN_PLANNER_ID
                state.status_flags["explicitly_selected_skill_id"] = requested
                state.status_flags["explicitly_selected_from_skill_id"] = (
                    preactivated_from or MAIN_PLANNER_ID
                )
            state.status_flags["last_handoff_context"] = handoff_payload
            self._record_events(
                context,
                [
                    make_event(
                        "route_suggestion_selected",
                        {
                            "requested_target_skill_id": requested,
                            "handoff_context": handoff_payload,
                            "resolved_target_skill_id": state.active_skill_id,
                            "already_active": True,
                        },
                    )
                ],
            )
            return state.active_skill_id or MAIN_PLANNER_ID
        target_bundle = self.runtime_registry.get(requested)
        if target_bundle is None:
            self._record_events(
                context,
                [
                    make_event(
                        "route_suggestion_rejected",
                        {
                            "requested_target_skill_id": requested,
                            "reason": "target skill not found",
                        },
                    )
                ],
            )
            return ""
        display = build_skill_display(context, active_skill=requested, runtime_registry=self.runtime_registry)
        route_decision = IntentRouteDecision(
            route_mode="switch" if (state.active_skill_id or MAIN_PLANNER_ID) != MAIN_PLANNER_ID else "direct",
            target_skill_id=requested,
            confidence=1.0,
            intent_clear=True,
            reason="用户从 finalized 单选建议中选择继续该 agent。",
            matched_examples=(),
            requires_user_choice=False,
            scene_name=display.get("scene_name", ""),
        )
        state.status_flags["last_handoff_context"] = handoff_payload
        state.status_flags["entry_skill_id"] = requested
        state.status_flags["entry_from_skill_id"] = state.active_skill_id or MAIN_PLANNER_ID
        state.status_flags["explicitly_selected_skill_id"] = requested
        state.status_flags["explicitly_selected_from_skill_id"] = (
            state.active_skill_id or MAIN_PLANNER_ID
        )
        target_facts = state.skill_facts.setdefault(requested, {})
        if isinstance(target_facts, dict) and isinstance(handoff_payload, dict):
            if handoff_payload.get("handoff_notes"):
                target_facts["handoff_notes"] = handoff_payload.get("handoff_notes")
            if handoff_payload.get("context_compression"):
                target_facts["handoff_context_summary"] = handoff_payload.get("context_compression")
        context.skill_states.setdefault(MAIN_PLANNER_ID, {})["intent_route"] = route_decision.as_dict()
        self._apply_intent_route_decision(state, context, route_decision)
        self._record_events(
            context,
            [
                make_event(
                    "route_suggestion_selected",
                    {
                        "requested_target_skill_id": requested,
                        "handoff_context": handoff_payload,
                        "resolved_target_skill_id": state.active_skill_id,
                    },
                )
            ],
        )
        return state.active_skill_id or MAIN_PLANNER_ID

    def _apply_intent_route_decision(
        self,
        state: SessionState,
        context,
        route_decision: IntentRouteDecision,
    ) -> None:
        target_skill_id = route_decision.target_skill_id or MAIN_PLANNER_ID
        previous_skill_id = state.active_skill_id or MAIN_PLANNER_ID
        if route_decision.route_mode == "resume":
            state.status_flags["resume_to_skill_id"] = target_skill_id
            self._record_intent_route_event(context, route_decision, from_skill=previous_skill_id, to_skill=target_skill_id)
            return
        if route_decision.route_mode == "main_planner":
            self._lock_main_planner(state)
            self._record_intent_route_event(
                context,
                route_decision,
                from_skill=previous_skill_id,
                to_skill=state.active_skill_id or MAIN_PLANNER_ID,
            )
            return
        if route_decision.route_mode in {"stay", "clarify", "recommend_switch"}:
            self._refresh_scene_lock(state)
            self._record_intent_route_event(
                context,
                route_decision,
                from_skill=previous_skill_id,
                to_skill=state.active_skill_id or previous_skill_id,
            )
            return
        if target_skill_id == state.active_skill_id:
            self._refresh_scene_lock(state)
            self._record_intent_route_event(
                context,
                route_decision,
                from_skill=previous_skill_id,
                to_skill=state.active_skill_id or previous_skill_id,
            )
            return
        if not self.runtime_registry.get(target_skill_id):
            self._refresh_scene_lock(state)
            self._record_intent_route_event(
                context,
                route_decision,
                from_skill=previous_skill_id,
                to_skill=state.active_skill_id or previous_skill_id,
            )
            return

        if previous_skill_id != MAIN_PLANNER_ID and target_skill_id != previous_skill_id:
            mark_route_interruption(state, target_skill_id=target_skill_id)
        state.route_history.append(
            {
                "from": previous_skill_id,
                "to": target_skill_id,
                "scene": route_decision.scene_name,
                "reason": route_decision.reason,
                "route_mode": route_decision.route_mode,
                "confidence": route_decision.confidence,
            }
        )
        state.active_skill_id = target_skill_id
        target_bundle = self.runtime_registry.get(target_skill_id)
        if target_bundle and target_bundle.contract.stages:
            state.stage = target_bundle.contract.stages[0].id
        state.status_flags["pending_route_scene"] = ""
        state.status_flags["pending_route_scene_source"] = ""
        self._refresh_scene_lock(state)
        self._record_intent_route_event(
            context,
            route_decision,
            from_skill=previous_skill_id,
            to_skill=state.active_skill_id or target_skill_id,
        )

    def _record_intent_route_event(
        self,
        context,
        route_decision: IntentRouteDecision,
        *,
        from_skill: str,
        to_skill: str,
    ) -> None:
        self._record_events(
            context,
            [
                make_event(
                    "main_planner_route",
                    {
                        "from_skill": from_skill,
                        "to_skill": to_skill,
                        "scene": route_decision.scene_name,
                        "reason": route_decision.reason,
                        "route_mode": route_decision.route_mode,
                        "confidence": route_decision.confidence,
                        "matched_examples": list(route_decision.matched_examples),
                        "debug_payload": route_decision.debug_payload,
                    },
                )
            ],
        )

    def _emit_router_error_if_present(self, context) -> None:
        error = getattr(self.intent_router, "last_llm_error", None)
        if error is None:
            return
        self._emit_model_error(context, error, terminal=False)
        self._record_events(
            context,
            [make_event("intent_router_model_error", {"error": f"{type(error).__name__}: {error}"})],
        )

    def _emit_model_error(self, context, error: BaseException, *, terminal: bool) -> None:
        callback = (context.session_meta or {}).get("model_error_callback")
        if callable(callback):
            callback(error, terminal=terminal)

    def _lock_main_planner(self, state: SessionState) -> None:
        state.active_skill_id = MAIN_PLANNER_ID
        state.status_flags["scene_lock"] = "consultative"
        state.status_flags["consultative_lock"] = True
        state.status_flags["task_lock"] = False

    def _refresh_scene_lock(self, state: SessionState) -> None:
        if (state.active_skill_id or MAIN_PLANNER_ID) == MAIN_PLANNER_ID:
            state.status_flags.setdefault("scene_lock", "consultative")
            state.status_flags["consultative_lock"] = True
            state.status_flags["task_lock"] = False
            return
        state.status_flags["scene_lock"] = "task"
        state.status_flags["consultative_lock"] = False
        state.status_flags["task_lock"] = True

    def _resume_previous_skill_if_requested(self, state: SessionState, context) -> str:
        target_skill_id = str(state.status_flags.get("resume_to_skill_id") or "").strip()
        if not target_skill_id:
            return ""
        if not self.runtime_registry.get(target_skill_id):
            state.status_flags["resume_to_skill_id"] = ""
            return ""
        previous_skill_id = state.active_skill_id
        state.route_history.append(
            {
                "from": previous_skill_id,
                "to": target_skill_id,
                "scene": "resume",
                "reason": "用户要求回到之前的 skill",
            }
        )
        state.active_skill_id = target_skill_id
        state.status_flags["resume_to_skill_id"] = ""
        state.status_flags["interrupted_skill_id"] = ""
        state.status_flags["pending_route_scene"] = ""
        self._record_events(
            context,
            [
                make_event(
                    "main_planner_route",
                    {
                        "from_skill": previous_skill_id,
                        "to_skill": target_skill_id,
                        "scene": "resume",
                        "reason": "用户要求回到之前的 skill",
                    },
                )
            ],
        )
        return state.active_skill_id or MAIN_PLANNER_ID

    def _seed_scene_hint(self, user_message: str, state: SessionState, context) -> None:
        if state.status_flags.get("pending_route_scene"):
            return
        if looks_like_structured_fact_update(user_message):
            return
        if (
            (state.active_skill_id or MAIN_PLANNER_ID) == MAIN_PLANNER_ID
            and (
                state.status_flags.get("consultative_lock")
                or state.status_flags.get("scene_lock") == "consultative"
                or not bool(state.status_flags.get("collection_complete", False))
            )
            and looks_like_profile_slot_answer(user_message)
        ):
            return
        explicit_scene = self._match_scene_from_registry(user_message, state=state, context=context)
        if explicit_scene:
            state.status_flags["pending_route_scene"] = explicit_scene
            state.status_flags["pending_route_scene_source"] = "registry"
            return
        recommended_scene = self._recommend_scene_from_profile_matrix(user_message, state, context)
        if recommended_scene:
            state.status_flags["pending_route_scene"] = recommended_scene
            state.status_flags["pending_route_scene_source"] = "profile_matrix"
            return
        for scene, keywords in SCENE_HINTS:
            if any(keyword in user_message for keyword in keywords):
                state.status_flags["pending_route_scene"] = scene
                state.status_flags["pending_route_scene_source"] = "keyword_fallback"
                return

    def _match_scene_from_registry(self, user_message: str, *, state: SessionState, context) -> str:
        del context
        normalized = user_message.strip()
        if not normalized:
            return ""
        stage_label = self._profile_stage_label(user_message, state, context=None)
        best_skill_id = ""
        best_alias_length = 0
        for skill_id, bundle in self.runtime_registry.enabled_bundles().items():
            if skill_id == MAIN_PLANNER_ID:
                continue
            aliases = self._skill_aliases(skill_id, bundle)
            for alias in aliases:
                if not alias or alias not in normalized:
                    continue
                alias_length = len(alias)
                if alias_length > best_alias_length:
                    best_alias_length = alias_length
                    best_skill_id = skill_id
        if not best_skill_id:
            return ""
        return self._canonical_scene_for_skill(best_skill_id, stage_label=stage_label)

    def _skill_aliases(self, skill_id: str, bundle) -> tuple[str, ...]:
        aliases: list[str] = []
        for route in self.main_bundle.contract.routes:
            if route.target_skill_id == skill_id:
                aliases.append(route.scene)
        aliases.extend(bundle.contract.accepts_scenes)
        aliases.extend(bundle.runtime_metadata.accepts_scenes)
        aliases.extend(bundle.runtime_metadata.triggers)
        if skill_id == "multi_path_planning":
            aliases.extend(("除了普通高考", "还有什么路", "还有哪些路", "其他升学路径", "别的升学路"))
        if skill_id == "mock_admission":
            aliases.extend(("能上什么大学", "分数能上", "学校层次"))
        seen: list[str] = []
        for alias in aliases:
            normalized = str(alias).strip()
            if normalized and normalized not in seen:
                seen.append(normalized)
        return tuple(seen)

    def _canonical_scene_for_skill(self, skill_id: str, *, stage_label: str = "") -> str:
        if skill_id == JUNIOR_MULTI_PATH_SKILL_ID:
            return "初中多元路径规划"
        if skill_id == "multi_path_planning" and stage_label == "初中":
            return "初中多元路径规划"
        for route in self.main_bundle.contract.routes:
            if route.target_skill_id != skill_id:
                continue
            if stage_label == "初中" and _is_multi_path_scene(route.scene):
                return "初中多元路径规划"
            return route.scene
        bundle = self.runtime_registry.get(skill_id)
        if bundle and bundle.contract.accepts_scenes:
            return bundle.contract.accepts_scenes[0]
        return ""

    def _recommend_scene_from_profile_matrix(self, user_message: str, state: SessionState, context) -> str:
        config = self.main_bundle.runtime_metadata.planner.scene_selection
        if config.mode != "profile_matrix" or not config.enable_implicit_routing:
            return ""
        if not any(keyword in user_message for keyword in PLANNING_AMBIGUITY_KEYWORDS):
            return ""
        recommendation = recommend_scenes_from_profile_matrix(
            self.main_bundle,
            stage=self._profile_stage_label(user_message, state, context),
            score_band=self._profile_score_band(user_message, state, context),
            talent_flag=self._profile_talent_flag(user_message, state, context),
        )
        if recommendation is None:
            return ""
        normalized_scenes = [
            self._normalize_profile_matrix_scene_name(scene, recommendation)
            for scene in recommendation.recommended_scenes
        ]
        resolved_scene = next((scene for scene in normalized_scenes if scene), "")
        if not resolved_scene:
            return ""
        profile_state = context.skill_states.setdefault(MAIN_PLANNER_ID, {})
        profile_state["profile_matrix"] = {
            "stage": recommendation.stage,
            "score_band": recommendation.score_band,
            "talent_flag": recommendation.talent_flag,
            "recommended_scenes": list(normalized_scenes),
            "matched_rows": [entry.source_row for entry in recommendation.matched_entries],
            "matrix_reference": config.matrix_reference,
        }
        self._record_events(
            context,
            [
                make_event(
                    "main_planner_profile_matrix",
                    {
                        "stage": recommendation.stage,
                        "score_band": recommendation.score_band,
                        "talent_flag": recommendation.talent_flag,
                        "recommended_scenes": [scene for scene in normalized_scenes if scene],
                        "matched_rows": [entry.source_row for entry in recommendation.matched_entries],
                        "matrix_reference": config.matrix_reference,
                    },
                )
            ],
        )
        return resolved_scene

    def _normalize_profile_matrix_scene_name(
        self,
        scene_name: str,
        recommendation: ProfileMatrixRecommendation,
    ) -> str:
        raw_scene = scene_name.strip()
        if not raw_scene:
            return ""
        direct_match = self._match_scene_from_registry(
            raw_scene,
            state=SessionState(session_id="matrix_normalize"),
            context=None,
        )
        if direct_match:
            return direct_match
        simplified = re.split(r"[（(]", raw_scene, maxsplit=1)[0].strip()
        direct_match = self._match_scene_from_registry(
            simplified,
            state=SessionState(session_id="matrix_normalize"),
            context=None,
        )
        if direct_match:
            return direct_match
        if "多元路径" in raw_scene or "特长生" in raw_scene or "综评" in raw_scene or "职教" in raw_scene:
            return "初中多元路径规划" if recommendation.stage == "初中" else "多元路径规划"
        if "模拟升学" in raw_scene:
            return "模拟升学"
        if "选科" in raw_scene:
            return "选科参谋"
        if "前景" in raw_scene or "职业" in raw_scene or "专业" in raw_scene:
            return "前景探路"
        if any(keyword in raw_scene for keyword in ("学习方法", "学习习惯", "学习自信", "基础学习", "提分")):
            return "提分"
        if any(keyword in raw_scene for keyword in ("培养建议", "兴趣探索", "兴趣", "潜在优势")):
            return "兴趣探索"
        return ""

    def _profile_stage_label(self, user_message: str, state: SessionState, context) -> str:
        if context is not None:
            grade_fact = context.known_facts.get_value("grade")
            if isinstance(grade_fact, str):
                inferred = _infer_profile_stage_label_from_text(grade_fact)
                if inferred:
                    return inferred
        grade_value = str(state.global_facts.get("grade") or "").strip()
        inferred = _infer_profile_stage_label_from_text(grade_value)
        if inferred:
            return inferred
        inferred = _infer_profile_stage_label_from_text(user_message)
        if inferred:
            return inferred
        return ""

    def _profile_score_band(self, user_message: str, state: SessionState, context) -> str:
        for candidate in (
            context.known_facts.get_value("score_level") if context is not None else "",
            state.global_facts.get("score_level"),
            user_message,
        ):
            normalized = self._normalize_score_band(str(candidate or ""))
            if normalized:
                return normalized
        return ""

    def _normalize_score_band(self, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        if any(keyword in normalized for keyword in TOP_SCORE_KEYWORDS):
            return "上游"
        if any(keyword in normalized for keyword in LOW_SCORE_KEYWORDS):
            return "下游"
        if any(keyword in normalized for keyword in MID_SCORE_KEYWORDS):
            return "中游"
        return normalized if normalized in {"上游", "中游", "下游"} else ""

    def _profile_talent_flag(self, user_message: str, state: SessionState, context) -> str:
        for candidate in (
            context.known_facts.get_value("talent") if context is not None else "",
            state.global_facts.get("talent"),
            user_message,
        ):
            normalized = self._normalize_talent_flag(str(candidate or ""))
            if normalized:
                return normalized
        return ""

    def _normalize_talent_flag(self, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        if any(keyword in normalized for keyword in TALENT_NEGATIVE_KEYWORDS):
            return "无"
        if any(keyword in normalized for keyword in TALENT_POSITIVE_KEYWORDS):
            return "有"
        return normalized if normalized in {"有", "无"} else ""

    def _infer_school_stage(self, context, state: SessionState) -> str:
        grade_fact = context.known_facts.get_value("grade")
        if isinstance(grade_fact, str):
            inferred = _infer_school_stage_from_text(grade_fact)
            if inferred:
                return inferred
        global_grade = state.global_facts.get("grade")
        if isinstance(global_grade, str):
            inferred = _infer_school_stage_from_text(global_grade)
            if inferred:
                return inferred
        for item in reversed(context.messages):
            if item.get("role") != "user":
                continue
            inferred = _infer_school_stage_from_text(str(item.get("content") or ""))
            if inferred:
                return inferred
        return ""

    def _split_multi_path_skill_by_stage(self, state: SessionState, context) -> None:
        if state.active_skill_id not in MULTI_PATH_SKILL_IDS:
            state.status_flags["awaiting_school_stage_for_multi_path"] = False
            state.status_flags["pending_multi_path_scene"] = ""
            return
        latest_user = next(
            (item.get("content") for item in reversed(context.messages) if item.get("role") == "user"),
            "",
        )
        explicit_grade = _extract_grade_value_from_text(str(latest_user or ""))
        if explicit_grade and not context.known_facts.get_value("grade"):
            context.update_fact("grade", explicit_grade, source_skill=MAIN_PLANNER_ID, confidence=0.85)
            state.global_facts["grade"] = explicit_grade
        inferred_stage = self._infer_school_stage(context, state)
        if inferred_stage == "junior":
            target_skill_id = JUNIOR_MULTI_PATH_SKILL_ID
        elif inferred_stage == "senior":
            target_skill_id = "multi_path_planning"
        else:
            pending_scene = ""
            if state.route_history and _is_multi_path_scene(str(state.route_history[-1].get("scene") or "")):
                pending_scene = str(state.route_history[-1].get("scene") or "")
            pending_scene = pending_scene or "多元路径规划"
            # Toolbar/card entry is authoritative.  Missing grade is collected
            # inside multi-path planning instead of silently returning to the
            # main advisor.
            state.status_flags["pending_route_scene"] = ""
            state.status_flags["awaiting_school_stage_for_multi_path"] = True
            state.status_flags["pending_multi_path_scene"] = pending_scene
            self._record_events(
                context,
                [
                    make_event(
                        "main_planner_stage_gate",
                        {
                            "blocked_skill": state.active_skill_id,
                            "reason": "学段未知，在多元路径 Skill 内追问年级",
                            "pending_scene": pending_scene,
                        },
                    )
                ],
            )
            return
        if state.active_skill_id == target_skill_id:
            state.status_flags["awaiting_school_stage_for_multi_path"] = False
            state.status_flags["pending_multi_path_scene"] = ""
            return
        previous_skill_id = state.active_skill_id
        target_bundle = self.runtime_registry.get(target_skill_id)
        if target_bundle is None:
            return
        state.active_skill_id = target_skill_id
        if target_bundle.contract.stages:
            state.stage = target_bundle.contract.stages[0].id
        ensure_runtime_state(state, target_bundle)
        state.status_flags["awaiting_school_stage_for_multi_path"] = False
        state.status_flags["pending_multi_path_scene"] = ""
        if state.route_history:
            last_route = state.route_history[-1]
            if last_route.get("to") in MULTI_PATH_SKILL_IDS:
                last_route["to"] = target_skill_id
                last_route["reason"] = (
                    f"{last_route.get('reason', '场景路由匹配。')}；根据学段识别切换为"
                    f" {target_skill_id}"
                )
        self._record_events(
            context,
            [
                make_event(
                    "main_planner_stage_split",
                    {
                        "from_skill": previous_skill_id,
                        "to_skill": target_skill_id,
                        "inferred_stage": inferred_stage,
                    },
                )
            ],
        )

    def _load_runtime_state(self, context) -> SessionState:
        payload = context.skill_states.get(RUNTIME_STATE_KEY, {})
        persisted_active_skill = canonical_skill_id(payload.get("active_skill_id"))
        # The interaction state is updated atomically by Skill transitions and
        # is the durable session-level source of truth.  Prefer it over an
        # older runtime payload so restoring a specialist conversation cannot
        # fall back to main_planner/general_chat and emit fresh route buttons.
        interaction_active_skill = canonical_skill_id((context.interaction_state or {}).get("active_skill"))
        active_skill = interaction_active_skill or persisted_active_skill or GENERAL_CHAT_ID
        state = SessionState(
            session_id=context.session_id,
            stage=str(payload.get("stage") or "init"),
            collected_info=dict(payload.get("collected_info") or {}),
            active_skill_id=active_skill,
            global_facts=dict(payload.get("global_facts") or {}),
            skill_facts={
                canonical_skill_id(key): dict(value)
                for key, value in (payload.get("skill_facts") or {}).items()
                if isinstance(value, dict)
            },
            stage_facts={
                canonical_skill_id(skill_id): {
                    str(stage_id): dict(stage_value)
                    for stage_id, stage_value in skill_value.items()
                    if isinstance(stage_value, dict)
                }
                for skill_id, skill_value in (payload.get("stage_facts") or {}).items()
                if isinstance(skill_value, dict)
            },
            status_flags=dict(payload.get("status_flags") or {}),
            route_history=list(payload.get("route_history") or []),
            messages=self._runtime_messages_from_context(context),
            soul_context=dict(payload.get("soul_context") or {}),
            conversation_memory=dict(payload.get("conversation_memory") or {}),
        )
        ensure_runtime_state(state, self.runtime_registry.get(active_skill) or self.main_bundle)
        return state

    def _persist_runtime_state(self, context, state: SessionState) -> None:
        context.skill_states[RUNTIME_STATE_KEY] = runtime_state_payload(state)
        context.skill_states[RUNTIME_STATE_KEY]["soul_context"] = state.soul_context
        context.skill_states[RUNTIME_STATE_KEY]["conversation_memory"] = state.conversation_memory
        context.skill_states.setdefault(MAIN_PLANNER_ID, {}).update(
            {
                "target_skill": state.active_skill_id or MAIN_PLANNER_ID,
                "stage": state.stage,
                "status_flags": dict(state.status_flags),
                "route_history_count": len(state.route_history),
            }
        )
        self._normalize_planner_state_alias(context)

    @staticmethod
    def _normalize_planner_state_alias(context) -> None:
        """Migrate old snapshots while keeping one compatibility read key."""
        states = context.skill_states
        legacy = states.get("main_planner") if isinstance(states, dict) else None
        canonical = states.get(MAIN_PLANNER_ID) if isinstance(states, dict) else None
        if not isinstance(canonical, dict) and isinstance(legacy, dict):
            states[MAIN_PLANNER_ID] = legacy
            canonical = legacy
        if isinstance(canonical, dict):
            # A read-only alias keeps old integrations and historical tests
            # functional; all new public IDs use career_plan_entity.
            states["main_planner"] = canonical

    def _runtime_messages_from_context(self, context) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        transition_context_ids = {
            str(item)
            for item in ((context.session_meta or {}).get("transition_context_message_ids") or [])
            if str(item)
        }
        include_internal_transition_turn = bool(
            (context.session_meta or {}).get("include_internal_transition_turn")
        )
        start_message_id = str((context.session_meta or {}).get("runtime_context_start_message_id") or "")
        include_message = not start_message_id
        for item in context.messages:
            item_id = str(item.get("message_id") or "")
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if transition_context_ids and item_id not in transition_context_ids:
                # Keep the current internal transition turn below; all other
                # historic messages are intentionally excluded for a
                # message_context handoff.
                if not (
                    include_internal_transition_turn
                    and metadata.get("message_type") == "skill_transition_command"
                ):
                    continue
            if start_message_id and str(item.get("message_id") or "") == start_message_id:
                include_message = True
                continue
            if not include_message:
                continue
            if metadata.get("hidden") or metadata.get("message_type") == "skill_transition":
                if not (
                    include_internal_transition_turn
                    and metadata.get("message_type") == "skill_transition_command"
                ):
                    continue
            role = str(item.get("role") or "")
            if role not in {"system", "user", "assistant", "tool"}:
                continue
            messages.append(ChatMessage(role=role, content=str(item.get("content") or "")))
        return messages

    def _runtime_session_file(self, session_id: str) -> Path:
        return self.runtime_bridge_config.runtime_dir / "sessions" / f"{session_id}.json"
