from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.responses import JSONResponse

from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.session_opening_config import (
    build_historical_session_opening_message,
    build_session_opening_message,
)
from hailiang_skills.core.request_logging import append_http_request_record
from hailiang_skills.core.session_logging import SseRecordingConfig, append_sse_record
from hailiang_skills.core.skill_lifecycle import build_finalized_payload
from hailiang_skills.core.status_labels import redact_skill_file_names
from hailiang_skills.core.telemetry import bind_telemetry, reset_telemetry
from hailiang_skills.core.streaming_runner import (
    StreamingRunner,
    _append_message_blocks_to_latest_assistant,
    _preactivate_requested_target_skill,
)
from hailiang_skills.core.registry import SkillRegistry
from hailiang_skills.llm.client import LLMClient
from hailiang_skills.llm.config import load_llm_config
from hailiang_skills.runtime_bridge import MainPlannerOrchestrator
from hailiang_skills.runtime_bridge.main_planner import (
    PROJECT_RUNTIME_SKILLS_ROOT,
    _IncrementalAssistantMessageExtractor,
    _QuestionnaireContinuationExtractor,
    _RuntimePlannerLLM,
    _authorize_requested_tool_specs,
    _tool_intent_label,
)
from hailiang_skills.runtime_bridge.facts import (
    sync_context_to_runtime_state,
    sync_runtime_state_to_context,
)
from hailiang_skills.skills.admission import AdmissionSkill
from hailiang_skills.skills.base import SkillResult
from hailiang_skills.skills.chat import ChatSkill
from hailiang_skills.skills.convergence import ConvergenceSkill
from hailiang_skills.skills.facts_extractor import FactsExtractorSkill
from hailiang_skills.skills.logging_skill import LoggingSkill
from hailiang_skills.skills.path_drilldown import PathDrillDownSkill
from hailiang_skills.skills.planner import PlannerSkill
from hailiang_skills.skills.router import RouterSkill
from hailiang_skills.skills.school_intro import SchoolIntroSkill
from hailiang_skills.skills.terminate_or_recommend import TerminateOrRecommendSkill
from hailiang_skills.skill_runtime.embedding_client import EmbeddingClient
from hailiang_skills.skill_runtime.intent_router import IntentRouter, RouteExample
from hailiang_skills.skill_runtime.models import (
    AssistantTurnResult,
    ChatMessage,
    ChatStreamChunk,
    PromptAssembly,
    RoutingDecision,
    SessionState,
    ToolCallRequest,
    ToolCallResult,
    ToolRoutingCandidate,
    ToolSpec,
)
from hailiang_skills.skill_runtime.runtime_router import parse_ms_agent_tool_routing
from hailiang_skills.skill_runtime.runtime_logger import RuntimeLogger
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry
from hailiang_skills.schemas.facts import normalize_fact_value
from hailiang_skills.api.main import _extract_request_context, _response_error_metadata

from agent_skill_runtime_core import CoreTraceStep, LoadedSkillContext


class FakeRuntimeClient:
    def complete(self, messages, *, logger=None) -> str:
        latest = str(getattr(messages[-1], "content", "") if messages else "")
        if "SkillAnalyzer" in latest or "plan_summary_short" in latest:
            return (
                '{"can_handle": true, "plan_summary": "测试环境按需加载并执行 native skill",'
                '"plan_summary_short": "测试Skill运行",'
                '"steps": [{"step": 1, "action": "测试环境确认 ms-agent plan 后进入回复生成", "type": "plan"}],'
                '"required_scripts": [], "required_references": [], "required_resources": [],'
                '"required_packages": [], "parameters": {}, "reasoning": "unit test",'
                '"tool_routing": {"required": false, "candidates": [], "allow_web_search": false,'
                '"candidate_domains": [], "query_focus": "", "reason": "unit test"}}'
            )
        return '{"allow_web_search": false, "candidate_domains": [], "query_focus": "", "reason": "unit test"}'

    def complete_with_tools(self, messages, tool_specs, *, preferred_mode="native", logger=None):
        return AssistantTurnResult(final_text="runtime 原生 Skill 回复", tool_mode="none")


class FakeRouteSuggestionClient:
    def __init__(self, response: str | None = None, *, fail: bool = False) -> None:
        self.response = response or '{"suggestions":[]}'
        self.fail = fail
        self.calls: list[str] = []

    def complete(self, messages, *, logger=None) -> str:
        latest = str(getattr(messages[-1], "content", "") if messages else "")
        self.calls.append(latest)
        if self.fail:
            raise RuntimeError("route suggestion llm unavailable")
        return self.response


class FakeEmbeddingClient:
    def __init__(
        self,
        *,
        fail: bool = False,
        model: str = "text-embedding-v4",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        max_batch_size: int = 10,
    ) -> None:
        self.fail = fail
        self.model = model
        self.base_url = base_url
        self.max_batch_size = max_batch_size
        self.last_batch_count = 0
        self.calls: list[list[str]] = []

    def embed(self, inputs):
        if self.fail:
            raise RuntimeError("embedding unavailable")
        if isinstance(inputs, str):
            self.last_batch_count = 1
            self.calls.append([inputs])
            return [self._vector(inputs)]
        normalized = [str(item) for item in inputs]
        self.last_batch_count = max((len(normalized) + self.max_batch_size - 1) // self.max_batch_size, 1)
        self.calls.append(normalized)
        return [self._vector(str(item)) for item in normalized]

    def _vector(self, text: str) -> list[float]:
        if "选科" in text or "物化" in text:
            return [1.0, 0.0, 0.0]
        if "能上" in text or "院校" in text or "大学" in text or "学校" in text:
            return [0.0, 1.0, 0.0]
        if "路径" in text or "高考之外" in text:
            return [0.0, 0.0, 1.0]
        return [0.2, 0.2, 0.2]


class FakeReasoningOnlyStreamClient:
    def __init__(self) -> None:
        self.retry_calls = 0

    def stream_complete(self, messages, *, logger=None):
        del messages, logger
        yield ChatStreamChunk(reasoning_delta="我已经想好了，但是没有输出正文。")

    def complete_with_tools(self, messages, tool_specs, *, preferred_mode="none", logger=None):
        del messages, tool_specs, preferred_mode, logger
        self.retry_calls += 1
        return AssistantTurnResult(final_text="这是非流式兜底生成的可见回复。", tool_mode="none")


class FakeStreamingRepository:
    def __init__(self, context: SessionContext) -> None:
        self.context = context

    def get(self, session_id: str) -> SessionContext:
        self.context.session_id = session_id
        return self.context

    def save(self, context: SessionContext) -> SessionContext:
        self.context = context
        return context


class FakeStreamingFactService:
    profile_repo = SimpleNamespace()

    def hydrate_context(self, context: SessionContext) -> SessionContext:
        context.load_effective_facts(
            shared_facts=context.shared_facts,
            profile_facts=context.profile_facts,
            session_facts=context.session_facts,
        )
        return context

    def persist_context(self, context: SessionContext) -> None:
        del context


class FakeStreamingOrchestrator:
    runtime_registry = None
    route_suggestion_client = None
    route_suggestion_monitor_every_turn = False

    def handle_message(self, content: str, context: SessionContext) -> SkillResult:
        del content
        context.interaction_state["active_skill"] = "main_planner"
        return SkillResult(assistant_message="好的，我们先了解孩子情况。")

    def _record_events(self, context: SessionContext, events: list[dict]) -> None:
        del context, events


class FakeStreamingSuggestionOrchestrator(FakeStreamingOrchestrator):
    def __init__(self, runtime_registry) -> None:
        self.runtime_registry = runtime_registry
        self.route_suggestion_client = FakeRouteSuggestionClient(
            '{"suggestions":['
            '{"target_skill_id":"interest_explore","agent_label":"兴趣探索","reason":"主回复提供继续探索兴趣方向。","confidence":0.95}'
            ']}'
        )
        self.route_suggestion_monitor_every_turn = True

    def handle_message(self, content: str, context: SessionContext) -> SkillResult:
        del content
        context.interaction_state["active_skill"] = "main_planner"
        return SkillResult(
            assistant_message=(
                "孩子理科能力不错，可以先从信息学试探兴趣。"
                "下一步想继续聊怎么探索这个方向，还是先看特长生升学路径？"
            )
        )


class FakeInterruptibleStreamingOrchestrator(FakeStreamingOrchestrator):
    def handle_message(self, content: str, context: SessionContext) -> SkillResult:
        generation_by_thread = context.session_meta.get("stream_generation_by_thread")
        stream_generation = (
            generation_by_thread.get(str(threading.get_ident()))
            if isinstance(generation_by_thread, dict)
            else ""
        )
        turn_id = f"turn_{content}"
        context.session_meta["active_turn_id"] = turn_id
        turn_by_generation = context.session_meta.setdefault("turn_id_by_stream_generation", {})
        if isinstance(turn_by_generation, dict) and stream_generation:
            turn_by_generation[stream_generation] = turn_id
        context.add_message("user", content, metadata={"turn_id": turn_id})
        if content == "old":
            time.sleep(0.08)
        reply = f"{content} reply"
        context.add_message("assistant", reply, metadata={"turn_id": turn_id})
        return SkillResult(assistant_message=reply)


def parse_sse_events(chunks: list[str]) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for chunk in chunks:
        event_name = ""
        data = {}
        for line in chunk.strip().splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        if event_name:
            events.append({"event": event_name, "data": data})
    return events


class FakeMSAgentRuntime:
    def __init__(self, skill_dir: Path) -> None:
        self.skill_dir = skill_dir
        self.run_kwargs = {}
        self.execute_kwargs = {}

    def run_single_skill_turn(self, **kwargs):
        self.run_kwargs = kwargs
        plan = {
            "planner": "fake",
            "can_handle": True,
            "plan_summary": "fake plan",
            "steps": [],
            "required_scripts": ["profile_op.py"],
            "required_references": [],
            "required_resources": [],
            "required_packages": [],
            "parameters": {"query": "给孩子做规划"},
            "reasoning": "unit test",
        }
        context = LoadedSkillContext(
            skill_key="main_planner@fake",
            skill_path=self.skill_dir,
            skill_md="fake skill",
            tools_yaml="",
            references=[],
            scripts=[
                {
                    "name": "profile_op.py",
                    "path": "scripts/profile_op.py",
                    "abs_path": str(self.skill_dir / "scripts" / "profile_op.py"),
                    "content": "",
                }
            ],
            resources=[],
            plan=plan,
            execution_outputs=[],
            raw_trace={
                "runtime": "ms_agent_single_skill",
                "skill_key": "main_planner@fake",
                "turn": 1,
                "planner": {"name": "fake", "error": None, "raw_response": "{}"},
                "plan": plan,
                "loaded": {"references": [], "scripts": [{"name": "profile_op.py", "path": "scripts/profile_op.py"}], "resources": []},
                "previous_lazy_load": {},
                "lazy_load_diff": {},
                "execution_outputs": [],
            },
        )
        return context, {"references": [], "scripts": ["profile_op.py"], "resources": []}, [
            CoreTraceStep(name="ms_agent_health", status="success", detail="ok"),
            CoreTraceStep(name="script_execution", status="skipped", detail="script execution disabled by request"),
        ]

    def execute_scripts_in_sandbox(self, **kwargs):
        self.execute_kwargs = kwargs
        return [
            {
                "script": "profile_op.py",
                "args": ["--query", "给孩子做规划", "--user-id", "u1"],
                "stdin_payload": kwargs["script_inputs"]["profile_op.py"],
                "exit_code": 0,
                "stdout": '{"ok": true, "profile": {}}',
                "stderr": "",
                "duration_ms": 1,
            }
        ], [
            CoreTraceStep(
                name="script_review",
                status="success",
                detail="static script safety review passed",
                payload={"script_review_duration_ms": 1},
            ),
            CoreTraceStep(
                name="sandbox_startup",
                status="success",
                detail="Docker sandbox is available",
                payload={"docker_check_duration_ms": 1},
            ),
            CoreTraceStep(
                name="script_execution",
                status="success",
                detail="MS-Agent SkillContainer completed script execution",
                payload={"outputs": []},
            )
        ]


class FailingMSAgentRuntime:
    runtime_probe = SimpleNamespace(available=True, status="available", error=None)

    def run_single_skill_turn(self, **kwargs):
        raise RuntimeError("MS-Agent SkillAnalyzer did not produce an executable plan")


def build_orchestrator() -> MainPlannerOrchestrator:
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
    return MainPlannerOrchestrator(registry, llm_config)


def build_orchestrator_with_config(llm_config) -> MainPlannerOrchestrator:
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
    return MainPlannerOrchestrator(registry, llm_config)


class RuntimeBridgeTest(unittest.TestCase):
    def test_status_label_redacts_current_skill_file_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill_root = Path(directory)
            (skill_root / "references").mkdir()
            (skill_root / "references" / "path_catalog.json").write_text("{}", encoding="utf-8")

            self.assertEqual(
                redact_skill_file_names("读取 path_catalog.json 后进行分拣", skill_root),
                "读取 内部文件 后进行分拣",
            )
            self.assertEqual(
                redact_skill_file_names("整理 path_catalog 资料", skill_root),
                "整理 内部文件 资料",
            )

    def test_ms_agent_status_callback_redacts_active_skill_file_names(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.skill_states["skill_runtime"] = {"active_skill_id": "multi_path_planning"}
        statuses: list[dict[str, str]] = []
        context.session_meta["status_callback"] = statuses.append

        orchestrator._emit_ms_agent_plan_statuses(
            context,
            {"steps": [{"action": "读取 runtime_contract.json 后进行分拣"}]},
        )

        self.assertEqual(statuses[0]["label"], "读取内部文件后进行分拣")

    def test_main_planner_declares_profile_matrix_scene_selection(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("main_planner")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        scene_selection = bundle.runtime_metadata.planner.scene_selection
        self.assertEqual(scene_selection.mode, "profile_matrix")
        self.assertEqual(
            scene_selection.matrix_reference,
            "references/06_画像_说明_五型与选择.md",
        )
        self.assertEqual(scene_selection.match_fields, ("grade", "score_level", "talent", "subject_preference"))
        self.assertTrue(scene_selection.enable_implicit_routing)
        self.assertEqual(bundle.runtime_metadata.response_policy.citation_visibility, "hidden")
        self.assertFalse(bundle.runtime_metadata.response_policy.allow_file_name_mentions)
        self.assertFalse(bundle.runtime_metadata.response_policy.allow_reference_id_mentions)
        self.assertIn(
            "不知道从哪里开始",
            bundle.runtime_metadata.planner.intent_router.unclear_intent_patterns,
        )
        self.assertEqual(bundle.runtime_metadata.planner.intent_router.embedding_model, "text-embedding-v4")
        self.assertEqual(bundle.runtime_metadata.planner.intent_router.long_profile_message_min_chars, 80)
        self.assertEqual(bundle.runtime_metadata.planner.intent_router.long_profile_signal_threshold, 3)

    def test_child_skill_declares_routing_examples(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("subject_advisor")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        self.assertEqual(bundle.runtime_metadata.routing.scene_name, "选科参谋")
        self.assertIn("高一怎么选科", bundle.runtime_metadata.routing.routing_examples)
        self.assertIn("grade", bundle.runtime_metadata.routing.slot_facts)
        self.assertEqual(bundle.runtime_metadata.routing.school_stage_scope, "junior_senior")

    def test_llm_config_controls_embedding_runtime_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "llm_config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "provider": "dashscope_compatible",
                        "base_url": "https://example.com/compatible-mode/v1",
                        "model": "deepseek-v4-flash",
                        "api_key_env": "DASHSCOPE_API_KEY",
                        "embedding": {
                            "enabled": False,
                            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                            "model": "text-embedding-v4",
                            "api_key_env": "DASHSCOPE_API_KEY",
                            "timeout_s": 8,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            llm_config = load_llm_config(config_path)
            orchestrator = build_orchestrator_with_config(llm_config)
        self.assertFalse(llm_config.embedding.enabled)
        self.assertEqual(llm_config.embedding.model, "text-embedding-v4")
        self.assertIsNone(orchestrator.embedding_client)

    def test_runtime_registry_uses_progressive_disclosure(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        bundle = registry.get("main_planner")
        self.assertIsNotNone(bundle)
        assert bundle is not None

        self.assertEqual(bundle.contract.skill_id, "main_planner")
        self.assertIsNone(bundle._skill_markdown)
        self.assertIsNone(bundle._references)
        self.assertIsNone(bundle._asset_domains)

        self.assertIn("升学规划顾问", bundle.skill_markdown)
        self.assertIsNotNone(bundle._skill_markdown)

    def test_ms_agent_native_turn_uses_hailiang_sandbox_payload(self) -> None:
        orchestrator = build_orchestrator()
        bundle = orchestrator.runtime_registry.get("main_planner")
        self.assertIsNotNone(bundle)
        assert bundle is not None
        fake_runtime = FakeMSAgentRuntime(bundle.root_dir)
        orchestrator.ms_agent_probe = SimpleNamespace(available=True, status="available", error=None)
        orchestrator.ms_agent_runtime = fake_runtime
        context = SessionContext(user_id="u1")
        state = SessionState(
            session_id=context.session_id,
            active_skill_id="main_planner",
            messages=[ChatMessage(role="user", content="给孩子做规划")],
        )
        logger = RuntimeLogger(Path(tempfile.gettempdir()) / "hailiang_test_runtime.jsonl", context.session_id)

        reply = orchestrator._prepare_ms_agent_native_turn(
            bundle,
            state,
            FakeRuntimeClient(),
            logger,
            context,
        )

        self.assertIsNone(reply)
        self.assertIs(fake_runtime.run_kwargs["execute_scripts"], False)
        self.assertIs(fake_runtime.execute_kwargs["execute_scripts"], True)
        profile_payload = fake_runtime.execute_kwargs["script_inputs"]["profile_op.py"]
        self.assertEqual(profile_payload["action"], "read")
        self.assertEqual(profile_payload["user_id"], "u1")
        self.assertEqual(profile_payload["session_id"], context.session_id)
        self.assertEqual(profile_payload["active_skill_id"], "main_planner")
        self.assertEqual(profile_payload["query"], "给孩子做规划")
        outputs = state.status_flags["ms_agent_runtime"]["execution_outputs"]
        self.assertEqual(outputs[0]["script"], "profile_op.py")
        self.assertEqual(outputs[0]["exit_code"], 0)
        self.assertEqual(outputs[0]["stdin_payload"]["action"], "read")
        runtime_events = [event for event in context.event_trace if event.get("event_type") == "ms_agent_runtime"]
        runtime_steps = [event.get("payload", {}).get("step") for event in runtime_events]
        self.assertIn("script_review", runtime_steps)
        self.assertIn("sandbox_startup", runtime_steps)
        self.assertIn("script_execution", runtime_steps)

    def test_runtime_logger_emits_shanghai_timestamp_and_ids(self) -> None:
        log_path = Path(tempfile.gettempdir()) / "hailiang_test_runtime_context.jsonl"
        log_path.unlink(missing_ok=True)
        _context, token = bind_telemetry(user_id="user_1", session_id="sess_1", run_id="run_1")
        try:
            logger = RuntimeLogger(log_path, "sess_1")
            logger.log("turn.resolve.start", detail="ok")
        finally:
            reset_telemetry(token)

        record = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(record["session_id"], "sess_1")
        self.assertEqual(record["user_id"], "user_1")
        self.assertEqual(record["run_id"], "run_1")
        self.assertTrue(str(record["timestamp"]).endswith("+08:00"))
        self.assertTrue(str(record["ts"]).endswith("+00:00"))
        self.assertEqual(record["payload"]["telemetry"]["run_id"], "run_1")

    def test_append_sse_record_emits_shanghai_timestamp_and_ids(self) -> None:
        log_root = Path(tempfile.gettempdir()) / "hailiang_test_sse_logs"
        if log_root.exists():
            import shutil

            shutil.rmtree(log_root)
        config = SseRecordingConfig(enabled=True, root_dir=log_root, format="jsonl")
        with patch("hailiang_skills.core.session_logging.load_sse_recording_config", return_value=config):
            _context, token = bind_telemetry(user_id="user_2", session_id="sess_2", run_id="run_2")
            try:
                append_sse_record("sess_2", "run_2", {"event": "unit_test"})
            finally:
                reset_telemetry(token)

        record_path = log_root / "sess_2" / "sse" / "run_2.jsonl"
        record = json.loads(record_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(record["session_id"], "sess_2")
        self.assertEqual(record["user_id"], "user_2")
        self.assertEqual(record["run_id"], "run_2")
        self.assertTrue(str(record["timestamp"]).endswith("+08:00"))
        self.assertTrue(str(record["timestamp_utc"]).endswith("+00:00"))

    def test_api_request_context_prefers_stream_body_identity(self) -> None:
        request = SimpleNamespace(
            path_params={},
            query_params={"profile_id": "profile_from_query"},
        )
        payload = {
            "session_id": "sess_from_body",
            "run_id": "run_from_body",
            "context_data": {
                "user_id": "user_from_context",
                "profile_id": "profile_from_context",
            },
        }

        context = _extract_request_context(request, body_payload=payload)

        self.assertEqual(context["session_id"], "sess_from_body")
        self.assertEqual(context["user_id"], "user_from_context")
        self.assertEqual(context["profile_id"], "profile_from_query")
        self.assertEqual(context["run_id"], "run_from_body")

    def test_append_http_request_record_emits_shanghai_timestamp_and_ids(self) -> None:
        log_path = Path(tempfile.gettempdir()) / "hailiang_test_http_requests.jsonl"
        log_path.unlink(missing_ok=True)
        _context, token = bind_telemetry(user_id="user_http", session_id="sess_http", run_id="run_http")
        try:
            append_http_request_record(
                method="POST",
                route="/api/v1/sessions/chat/stream",
                status_code=200,
                duration_ms=12.345,
                request_id="req_http",
                file_path=log_path,
            )
        finally:
            reset_telemetry(token)

        record = json.loads(log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(record["request_id"], "req_http")
        self.assertEqual(record["session_id"], "sess_http")
        self.assertEqual(record["user_id"], "user_http")
        self.assertEqual(record["run_id"], "run_http")
        self.assertEqual(record["route"], "/api/v1/sessions/chat/stream")
        self.assertTrue(str(record["timestamp"]).endswith("+08:00"))
        self.assertTrue(str(record["timestamp_utc"]).endswith("+00:00"))

    def test_response_error_metadata_keeps_json_detail_for_http_logs(self) -> None:
        response = JSONResponse(
            status_code=422,
            content={
                "code": "REQUEST_VALIDATION_ERROR",
                "message": "请求字段校验失败。",
                "detail": [
                    {"loc": ["body", "run_id"], "msg": "Field required", "type": "missing"}
                ],
            },
        )

        self.assertEqual(
            _response_error_metadata(response),
            {
                "response_error_code": "REQUEST_VALIDATION_ERROR",
                "response_error_message": "请求字段校验失败。",
                "response_error_detail": [
                    {"loc": ["body", "run_id"], "msg": "Field required", "type": "missing"}
                ],
            },
        )

    def test_runtime_planner_llm_normalizes_nonstandard_json_plan(self) -> None:
        class NonstandardPlannerClient(FakeRuntimeClient):
            def complete(self, messages, *, logger=None) -> str:
                return (
                    "```json\n"
                    '{"can_handle": true, "contribution": "执行初中多元路径规划 skill",'
                    '"plan_summary": "执行初中多元路径规划 skill",'
                    '"steps": [{"action": "生成多元路径建议"}],'
                    '"required_scripts": [], "required_references": [], "required_resources": [],'
                    '"required_packages": []}'
                    "\n```"
                )

        planner = _RuntimePlannerLLM(NonstandardPlannerClient())
        message = planner.generate([SimpleNamespace(content="plan this turn")])
        payload = json.loads(message.content)

        self.assertTrue(payload["can_handle"])
        self.assertEqual(payload["required_scripts"], [])
        self.assertEqual(payload["steps"][0]["step"], 1)
        self.assertEqual(payload["steps"][0]["action"], "生成多元路径建议")
        self.assertEqual(payload["plan_summary_short"], "继续规划")

    def test_runtime_planner_llm_carries_combined_response_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            skill_dir = Path(directory)
            references_dir = skill_dir / "references"
            references_dir.mkdir()
            (references_dir / "rule.md").write_text("本地规则资料", encoding="utf-8")

            class CombinedPlannerClient:
                def __init__(self) -> None:
                    self.prompts: list[str] = []

                def complete(self, messages, *, logger=None) -> str:
                    del logger
                    self.prompts.append(str(messages[-1].content))
                    return json.dumps(
                        {
                            "can_handle": True,
                            "plan_summary": "读取本地规则并直接回复",
                            "plan_summary_short": "直接生成回复",
                            "steps": [],
                            "required_scripts": [],
                            "required_references": ["rule.md"],
                            "required_resources": [],
                            "required_packages": [],
                            "parameters": {},
                            "reasoning": "unit test",
                            "assistant_message": "这是同一次规划调用生成的正文。",
                        },
                        ensure_ascii=False,
                    )

            client = CombinedPlannerClient()
            planner = _RuntimePlannerLLM(client, skill_dir=skill_dir)
            message = planner.generate([SimpleNamespace(content="回答本轮问题")])

            payload = json.loads(message.content)
            self.assertTrue(payload["can_handle"])
            self.assertEqual(planner.last_combined_response, "这是同一次规划调用生成的正文。")
            self.assertIn("references/rule.md", client.prompts[0])
            self.assertNotIn("本地规则资料", client.prompts[0])

    def test_incremental_assistant_message_extractor_handles_fragmented_json_escapes(self) -> None:
        extractor = _IncrementalAssistantMessageExtractor()
        chunks = [
            '{"required_scripts": [], "assistant_mes',
            'sage": "第一行\\n第',
            '二行，代号\\u0031',
            '。", "questionnaire_response": null}',
        ]

        visible = "".join(extractor.feed(chunk) for chunk in chunks)

        self.assertEqual(visible, "第一行\n第二行，代号1。")
        self.assertTrue(extractor.complete)

    def test_incremental_assistant_message_extractor_buffers_script_dependent_reply(self) -> None:
        extractor = _IncrementalAssistantMessageExtractor()
        response = (
            '{"required_scripts":["lookup.py"],'
            '"assistant_message":"这段正文必须等待脚本结果。"}'
        )

        self.assertEqual(extractor.feed(response), "")
        self.assertFalse(extractor.complete)

    def test_incremental_assistant_message_extractor_buffers_reference_dependent_reply(self) -> None:
        extractor = _IncrementalAssistantMessageExtractor()
        response = (
            '{"required_scripts":[],"required_references":["references/rule.md"],'
            '"required_resources":[],"required_packages":[],'
            '"assistant_message":"这段正文必须等待 reference 加载。"}'
        )

        self.assertEqual(extractor.feed(response), "")
        self.assertFalse(extractor.complete)

    def test_incremental_assistant_message_extractor_buffers_tool_dependent_reply(self) -> None:
        extractor = _IncrementalAssistantMessageExtractor(require_tool_routing_gate=True)
        response = json.dumps(
            {
                "required_scripts": [],
                "tool_routing": {
                    "required": True,
                    "candidates": [
                        {
                            "kind": "tool",
                            "name": "web_search",
                            "intent_label": "查询实时天气",
                            "reason": "需要实时数据",
                        }
                    ],
                    "allow_web_search": True,
                    "candidate_domains": [],
                    "query_focus": "今天的天气",
                    "reason": "需要实时数据",
                },
                "assistant_message": "这段正文不能在工具执行前展示。",
            },
            ensure_ascii=False,
        )

        self.assertEqual(extractor.feed(response), "")
        self.assertFalse(extractor.complete)

    def test_questionnaire_continuation_streams_message_before_question_selection_finishes(self) -> None:
        extractor = _QuestionnaireContinuationExtractor({"方向（艺术）", "能力基础"})
        chunks = [
            '{"assistant_mes',
            'sage":"已经了解你的艺术方向，接下来确认训练基础。",',
            '"question_ids":["能力基础"],"collection_complete":false}',
        ]
        first = extractor.feed(chunks[0])
        second = extractor.feed(chunks[1])
        third = extractor.feed(chunks[2])

        self.assertEqual(first, "")
        self.assertEqual(second, "已经了解你的艺术方向，接下来确认训练基础。")
        self.assertEqual(third, "")

    def test_explicit_skill_entry_uses_small_streaming_response(self) -> None:
        orchestrator = build_orchestrator()
        bundle = orchestrator.runtime_registry.get("interest_explore")
        self.assertIsNotNone(bundle)
        assert bundle is not None
        streamed: list[str] = []

        class EntryClient:
            def __init__(self) -> None:
                self.request_purpose = ""
                self.messages: list[ChatMessage] = []

            def stream_complete(self, messages, *, logger=None, request_purpose="", cancel_check=None):
                del logger, cancel_check
                self.request_purpose = request_purpose
                self.messages = list(messages)
                yield ChatStreamChunk(content_delta="我会接着孩子的兴趣情况往下梳理，")
                yield ChatStreamChunk(content_delta="先确认目前最愿意长期投入的活动。")

        client = EntryClient()
        orchestrator.questionnaire_client = client
        context = SessionContext(session_id="sess_fast_entry", user_id="u1")
        context.session_meta["stream_final_reply"] = True
        context.session_meta["reply_delta_callback"] = streamed.append
        state = SessionState(
            session_id=context.session_id,
            active_skill_id="interest_explore",
            global_facts={"grade": "初二"},
            status_flags={
                "last_handoff_context": {
                    "context_compression": "孩子在探索兴趣方向",
                    "raw_history": "冗长历史" * 20_000,
                }
            },
            conversation_memory={"summary": "家长正在了解培养方向", "facts": {"grade": "初二"}},
        )
        logger = RuntimeLogger(
            Path(tempfile.gettempdir()) / "hailiang_test_skill_entry.jsonl",
            context.session_id,
        )

        reply, reasoning = orchestrator._resolve_lightweight_skill_entry(
            bundle,
            state,
            logger,
            context,
        )

        self.assertEqual(client.request_purpose, "skill_entry_response")
        self.assertEqual(reply, "".join(streamed))
        self.assertEqual(reasoning, "")
        prompt = "\n".join(message.content for message in client.messages)
        self.assertIn("特长培养规划", prompt)
        self.assertIn("孩子在探索兴趣方向", prompt)
        self.assertNotIn("required_references", prompt)
        self.assertNotIn("MS-Agent", prompt)
        self.assertLess(len(prompt), 10_000)

    def test_ms_agent_tool_routing_parser_rejects_unknown_or_inconsistent_candidates(self) -> None:
        unknown = {
            "required": True,
            "candidates": [{"kind": "tool", "name": "shell", "intent_label": "执行系统命令"}],
            "allow_web_search": False,
            "candidate_domains": [],
            "query_focus": "",
            "reason": "invalid",
        }
        inconsistent = {
            "required": False,
            "candidates": [{"kind": "tool", "name": "rag", "intent_label": "检索本地资料"}],
            "allow_web_search": False,
            "candidate_domains": [],
            "query_focus": "",
            "reason": "invalid",
        }

        self.assertIsNone(parse_ms_agent_tool_routing(unknown))
        self.assertIsNone(parse_ms_agent_tool_routing(inconsistent))

    def test_ms_agent_tool_candidates_can_only_reduce_server_authorized_specs(self) -> None:
        decision = parse_ms_agent_tool_routing(
            {
                "required": True,
                "candidates": [
                    {
                        "kind": "tool",
                        "name": "web_search",
                        "intent_label": "查询实时天气",
                        "reason": "实时问题",
                    }
                ],
                "allow_web_search": True,
                "candidate_domains": [],
                "query_focus": "天气",
                "reason": "实时问题",
            }
        )
        assert decision is not None
        specs = (
            ToolSpec(name="web_search", description="web", parameters_schema={}, enabled=True),
            ToolSpec(name="mcp", description="mcp", parameters_schema={}, enabled=True),
            ToolSpec(name="rag", description="rag", parameters_schema={}, enabled=False),
        )

        authorized = _authorize_requested_tool_specs(specs, decision)

        self.assertEqual([item.name for item in authorized if item.enabled], ["web_search"])
        self.assertEqual(_tool_intent_label(decision, "web_search"), "查询实时天气")

    def test_runtime_planner_streams_combined_assistant_message_before_response_finishes(self) -> None:
        full_response = json.dumps(
            {
                "can_handle": True,
                "required_scripts": [],
                "required_references": [],
                "required_resources": [],
                "required_packages": [],
                "tool_routing": {
                    "required": False,
                    "candidates": [],
                    "allow_web_search": False,
                    "candidate_domains": [],
                    "query_focus": "",
                    "reason": "unit test",
                },
                "assistant_message": "正文首字无需等待完整响应。",
                "plan_summary_short": "正在生成正文",
                "plan_summary": "规划并实时生成正文",
                "steps": [],
                "parameters": {},
                "reasoning": "unit test",
                "questionnaire_response": None,
            },
            ensure_ascii=False,
        )
        split_at = full_response.index("无需等待")
        streamed: list[str] = []

        class StreamingPlannerClient:
            def stream_complete(self, messages, *, logger=None, cancel_check=None):
                del messages, logger, cancel_check
                yield ChatStreamChunk(content_delta=full_response[:split_at])
                self.first_chunk_observed = "".join(streamed)
                yield ChatStreamChunk(content_delta=full_response[split_at:])

            def complete(self, messages, *, logger=None):
                raise AssertionError("streamed planner response must not retry non-streaming")

        client = StreamingPlannerClient()
        planner = _RuntimePlannerLLM(
            client,
            stream_reply_callback=streamed.append,
            require_tool_routing_gate=True,
        )
        message = planner.generate([SimpleNamespace(content="回答本轮问题")])

        self.assertIn("正文首字", client.first_chunk_observed)
        self.assertEqual("".join(streamed), "正文首字无需等待完整响应。")
        self.assertTrue(planner.streamed_combined_response)
        self.assertIsNotNone(planner.first_visible_delta_ms)
        self.assertEqual(planner.last_combined_response, "正文首字无需等待完整响应。")
        self.assertTrue(json.loads(message.content)["can_handle"])

    def test_native_turn_uses_combined_response_without_second_body_call(self) -> None:
        class CombinedRuntime:
            runtime_probe = SimpleNamespace(available=True, status="available", error=None)

            def run_single_skill_turn(self, **kwargs):
                return (
                    LoadedSkillContext(
                        skill_key="interest_explore@combined",
                        skill_path=Path("/tmp/interest_explore"),
                        skill_md="native skill",
                        tools_yaml="",
                        references=[],
                        scripts=[],
                        resources=[],
                        plan={
                            "can_handle": True,
                            "plan_summary": "直接生成回复",
                            "steps": [],
                            "required_scripts": [],
                            "required_references": [],
                            "required_resources": [],
                            "required_packages": [],
                            "parameters": {},
                            "reasoning": "unit test",
                        },
                        execution_outputs=[],
                        raw_trace={
                            "runtime": "ms_agent_single_skill",
                            "skill_key": "interest_explore@combined",
                            "turn": 1,
                            "planner": {"name": "fake", "error": None},
                            "plan": {},
                            "loaded": {"references": [], "scripts": [], "resources": []},
                        },
                        combined_response="这是合并后的原生 Skill 正文。",
                    ),
                    {"references": [], "scripts": [], "resources": []},
                    [],
                )

        class NoBodyCallClient(FakeRuntimeClient):
            def complete_with_tools(self, *args, **kwargs):
                raise AssertionError("combined response should skip the independent body call")

        orchestrator = build_orchestrator()
        orchestrator.ms_agent_runtime = CombinedRuntime()
        orchestrator.ms_agent_probe = CombinedRuntime.runtime_probe
        context = SessionContext(user_id="u1")
        state = SessionState(
            session_id=context.session_id,
            active_skill_id="interest_explore",
            messages=[ChatMessage(role="user", content="我喜欢演讲和辩论")],
        )
        logger = RuntimeLogger(Path(tempfile.gettempdir()) / "hailiang_test_combined.jsonl", context.session_id)

        with patch(
            "hailiang_skills.runtime_bridge.main_planner.build_tool_specs",
            return_value=(),
        ):
            reply, _ = orchestrator._resolve_runtime_reply(
                orchestrator.main_bundle,
                state,
                NoBodyCallClient(),
                logger,
                context,
            )

        self.assertEqual(reply, "这是合并后的原生 Skill 正文。")
        combined_event = next(
            event
            for event in context.event_trace
            if event.get("event_type") == "ms_agent_runtime"
            and event.get("payload", {}).get("step") == "combined_response"
        )
        self.assertEqual(combined_event["payload"]["status"], "success")

    def test_streamed_combined_response_is_not_emitted_twice_after_tool_routing(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        emitted: list[str] = []
        context.session_meta.update(
            {
                "stream_final_reply": True,
                "reply_delta_callback": emitted.append,
            }
        )
        state = SessionState(
            session_id=context.session_id,
            active_skill_id="interest_explore",
            messages=[ChatMessage(role="user", content="我喜欢演讲和辩论")],
        )
        logger = RuntimeLogger(Path(tempfile.gettempdir()) / "hailiang_test_stream_once.jsonl", context.session_id)

        def prepare_combined(*args, **kwargs):
            del args, kwargs
            state.status_flags["ms_agent_combined_response"] = "已经实时发送的正文。"
            state.status_flags["ms_agent_combined_response_streamed"] = True
            return None

        with (
            patch.object(orchestrator, "_prepare_ms_agent_native_turn", side_effect=prepare_combined),
            patch(
                "hailiang_skills.runtime_bridge.main_planner.build_tool_specs",
                return_value=(),
            ),
        ):
            reply, _ = orchestrator._resolve_runtime_reply(
                orchestrator.main_bundle,
                state,
                FakeRuntimeClient(),
                logger,
                context,
            )

        self.assertEqual(reply, "已经实时发送的正文。")
        self.assertEqual(emitted, [])

    def test_valid_merged_tool_routing_skips_standalone_classifier(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        state = SessionState(
            session_id=context.session_id,
            active_skill_id="interest_explore",
            messages=[ChatMessage(role="user", content="普通规划建议")],
        )
        logger = RuntimeLogger(Path(tempfile.gettempdir()) / "hailiang_test_merged_route.jsonl", context.session_id)

        def prepare_merged(*args, **kwargs):
            del args, kwargs
            state.status_flags["ms_agent_tool_routing"] = {
                "required": False,
                "candidates": [],
                "allow_web_search": False,
                "candidate_domains": [],
                "query_focus": "",
                "reason": "无需工具",
            }
            state.status_flags["ms_agent_combined_response"] = "直接使用合并正文。"
            return None

        with (
            patch.object(orchestrator, "_prepare_ms_agent_native_turn", side_effect=prepare_merged),
            patch(
                "hailiang_skills.runtime_bridge.main_planner.classify_tool_routing",
                side_effect=AssertionError("valid merged routing must skip standalone classifier"),
            ),
            patch(
                "hailiang_skills.runtime_bridge.main_planner.build_tool_specs",
                return_value=(),
            ),
        ):
            reply, _ = orchestrator._resolve_runtime_reply(
                orchestrator.main_bundle,
                state,
                FakeRuntimeClient(),
                logger,
                context,
            )

        self.assertEqual(reply, "直接使用合并正文。")

    def test_invalid_merged_tool_routing_uses_standalone_classifier_once(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        state = SessionState(
            session_id=context.session_id,
            active_skill_id="interest_explore",
            messages=[ChatMessage(role="user", content="普通规划建议")],
        )
        logger = RuntimeLogger(Path(tempfile.gettempdir()) / "hailiang_test_route_fallback.jsonl", context.session_id)

        def prepare_invalid(*args, **kwargs):
            del args, kwargs
            state.status_flags["ms_agent_combined_response"] = "异常路由后的安全正文。"
            return None

        fallback = RoutingDecision(source="standalone_classifier")
        with (
            patch.object(orchestrator, "_prepare_ms_agent_native_turn", side_effect=prepare_invalid),
            patch(
                "hailiang_skills.runtime_bridge.main_planner.classify_tool_routing",
                return_value=fallback,
            ) as classifier,
            patch(
                "hailiang_skills.runtime_bridge.main_planner.build_tool_specs",
                return_value=(),
            ),
        ):
            reply, _ = orchestrator._resolve_runtime_reply(
                orchestrator.main_bundle,
                state,
                FakeRuntimeClient(),
                logger,
                context,
            )

        self.assertEqual(reply, "异常路由后的安全正文。")
        self.assertEqual(classifier.call_count, 1)
        self.assertTrue(
            any(event.get("event_type") == "tool_routing_fallback" for event in context.event_trace)
        )

    def test_tool_intent_label_is_emitted_only_before_authorized_execution(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        statuses: list[dict[str, object]] = []
        context.session_meta["status_callback"] = statuses.append
        state = SessionState(
            session_id=context.session_id,
            active_skill_id="interest_explore",
            messages=[ChatMessage(role="user", content="请查询外部资料")],
        )
        logger = RuntimeLogger(Path(tempfile.gettempdir()) / "hailiang_test_tool_intent.jsonl", context.session_id)

        def prepare_tool(*args, **kwargs):
            del args, kwargs
            state.status_flags["ms_agent_tool_routing"] = {
                "required": True,
                "candidates": [
                    {
                        "kind": "tool",
                        "name": "mcp",
                        "intent_label": "查询外部资料",
                        "reason": "需要外部信息",
                    }
                ],
                "allow_web_search": False,
                "candidate_domains": [],
                "query_focus": "外部资料",
                "reason": "需要外部信息",
            }
            return None

        class ToolClient(FakeRuntimeClient):
            def complete_with_tools(self, messages, tool_specs, *, preferred_mode="native", logger=None):
                del messages, tool_specs, preferred_mode, logger
                return AssistantTurnResult(
                    tool_mode="native",
                    tool_calls=(
                        ToolCallRequest(
                            id="call-1",
                            name="mcp",
                            arguments={"server_name": "allowed", "tool_name": "search"},
                        ),
                    ),
                )

        enabled_mcp = ToolSpec(name="mcp", description="mcp", parameters_schema={}, enabled=True)
        with (
            patch.object(orchestrator, "_prepare_ms_agent_native_turn", side_effect=prepare_tool),
            patch.object(orchestrator, "_stream_runtime_final_text", return_value=("工具结果正文", "")),
            patch(
                "hailiang_skills.runtime_bridge.main_planner.build_tool_specs",
                return_value=(enabled_mcp,),
            ),
            patch(
                "hailiang_skills.runtime_bridge.main_planner.execute_tool_call",
                return_value=ToolCallResult(id="call-1", name="mcp", ok=True, content="result"),
            ) as execute,
        ):
            reply, _ = orchestrator._resolve_runtime_reply(
                orchestrator.main_bundle,
                state,
                ToolClient(),
                logger,
                context,
            )

        self.assertEqual(reply, "工具结果正文")
        self.assertEqual(execute.call_count, 1)
        self.assertTrue(
            any(item.get("stage") == "tool_mcp" and item.get("label") == "查询外部资料" for item in statuses)
        )

    def test_unapproved_tool_call_is_rejected_without_intent_label(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        statuses: list[dict[str, object]] = []
        context.session_meta["status_callback"] = statuses.append
        state = SessionState(
            session_id=context.session_id,
            active_skill_id="interest_explore",
            messages=[ChatMessage(role="user", content="请查询实时资料")],
        )
        logger = RuntimeLogger(
            Path(tempfile.gettempdir()) / "hailiang_test_tool_rejected.jsonl",
            context.session_id,
        )

        def prepare_web(*args, **kwargs):
            del args, kwargs
            state.status_flags["ms_agent_tool_routing"] = {
                "required": True,
                "candidates": [
                    {
                        "kind": "tool",
                        "name": "web_search",
                        "intent_label": "查询实时资料",
                        "reason": "需要实时信息",
                    }
                ],
                "allow_web_search": True,
                "candidate_domains": ["school_intro"],
                "query_focus": "实时资料",
                "reason": "需要实时信息",
            }
            return None

        class UnexpectedToolClient(FakeRuntimeClient):
            def complete_with_tools(self, messages, tool_specs, *, preferred_mode="native", logger=None):
                del messages, tool_specs, preferred_mode, logger
                return AssistantTurnResult(
                    tool_mode="native",
                    tool_calls=(
                        ToolCallRequest(id="call-1", name="mcp", arguments={"query": "x"}),
                    ),
                )

        enabled_web = ToolSpec(
            name="web_search",
            description="web",
            parameters_schema={},
            enabled=True,
        )
        with (
            patch.object(orchestrator, "_prepare_ms_agent_native_turn", side_effect=prepare_web),
            patch.object(orchestrator, "_stream_runtime_final_text", return_value=("安全回复", "")),
            patch(
                "hailiang_skills.runtime_bridge.main_planner.build_tool_specs",
                return_value=(enabled_web,),
            ),
            patch(
                "hailiang_skills.runtime_bridge.main_planner.execute_tool_call",
            ) as execute,
        ):
            reply, _ = orchestrator._resolve_runtime_reply(
                orchestrator.main_bundle,
                state,
                UnexpectedToolClient(),
                logger,
                context,
            )

        self.assertEqual(reply, "安全回复")
        execute.assert_not_called()
        self.assertFalse(any(item.get("stage") == "tool_mcp" for item in statuses))

    def test_merged_tool_routing_enables_incremental_gate_before_planner(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        state = SessionState(
            session_id=context.session_id,
            active_skill_id="interest_explore",
            messages=[ChatMessage(role="user", content="请调用工具查询")],
        )
        logger = RuntimeLogger(Path(tempfile.gettempdir()) / "hailiang_test_tool_gate.jsonl", context.session_id)
        streaming_flags: list[bool] = []
        routing_gate_flags: list[bool] = []

        def prepare_with_tool(*args, **kwargs):
            del args, kwargs
            streaming_flags.append(
                bool(state.status_flags.get("ms_agent_stream_combined_response"))
            )
            routing_gate_flags.append(
                bool(state.status_flags.get("ms_agent_require_tool_routing_gate"))
            )
            return "工具场景保持缓冲。"

        with (
            patch.object(orchestrator, "_prepare_ms_agent_native_turn", side_effect=prepare_with_tool),
            patch(
                "hailiang_skills.runtime_bridge.main_planner.build_tool_specs",
                return_value=(ToolSpec(name="mcp", description="test", parameters_schema={}, enabled=True),),
            ),
        ):
            reply, _ = orchestrator._resolve_runtime_reply(
                orchestrator.main_bundle,
                state,
                FakeRuntimeClient(),
                logger,
                context,
            )

        self.assertEqual(reply, "工具场景保持缓冲。")
        self.assertEqual(streaming_flags, [True])
        self.assertEqual(routing_gate_flags, [True])

    def test_native_turn_accepts_legacy_core_context_without_combined_field(self) -> None:
        class CombinedPlannerClient(FakeRuntimeClient):
            def complete(self, messages, *, logger=None) -> str:
                latest = str(getattr(messages[-1], "content", "") if messages else "")
                if "SkillAnalyzer" in latest or "plan_summary_short" in latest:
                    return json.dumps(
                        {
                            "can_handle": True,
                            "plan_summary": "合并生成正文",
                            "plan_summary_short": "合并生成正文",
                            "steps": [],
                            "required_scripts": [],
                            "required_references": [],
                            "required_resources": [],
                            "required_packages": [],
                            "parameters": {},
                            "reasoning": "unit test",
                            "assistant_message": "旧版 core 也能读取的合并正文。",
                        },
                        ensure_ascii=False,
                    )
                return super().complete(messages, logger=logger)

        class LegacyCoreRuntime:
            def run_single_skill_turn(self, **kwargs):
                planner = kwargs["planner_llm"]
                planner.generate([SimpleNamespace(content="plan this turn")])
                plan = json.loads(planner.last_raw_response)
                # Simulate a deployed debug/runtime core from before the
                # optional LoadedSkillContext.combined_response field existed.
                context = SimpleNamespace(
                    references=[],
                    scripts=[],
                    resources=[],
                    execution_outputs=[],
                    plan=plan,
                    raw_trace={
                        "runtime": "ms_agent_single_skill",
                        "skill_key": "interest_explore@legacy",
                        "turn": 1,
                        "planner": {"name": "fake", "error": None},
                        "plan": plan,
                        "loaded": {"references": [], "scripts": [], "resources": []},
                    },
                )
                return context, {"references": [], "scripts": [], "resources": []}, []

        orchestrator = build_orchestrator()
        orchestrator.ms_agent_probe = SimpleNamespace(available=True, status="available", error=None)
        orchestrator.ms_agent_runtime = LegacyCoreRuntime()
        bundle = orchestrator.runtime_registry.get("interest_explore")
        assert bundle is not None
        context = SessionContext(user_id="u1")
        state = SessionState(
            session_id=context.session_id,
            active_skill_id="interest_explore",
            messages=[ChatMessage(role="user", content="我喜欢演讲")],
        )
        logger = RuntimeLogger(Path(tempfile.gettempdir()) / "hailiang_test_legacy_core.jsonl", context.session_id)

        reply = orchestrator._prepare_ms_agent_native_turn(
            bundle,
            state,
            CombinedPlannerClient(),
            logger,
            context,
        )

        self.assertIsNone(reply)
        self.assertEqual(
            state.status_flags["ms_agent_combined_response"],
            "旧版 core 也能读取的合并正文。",
        )

    def test_ms_agent_plan_failure_falls_back_to_hailiang_prompt_reply(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.ms_agent_runtime = FailingMSAgentRuntime()
        orchestrator.ms_agent_probe = FailingMSAgentRuntime.runtime_probe
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初二", source_skill="test")
        state = SessionState(
            session_id=context.session_id,
            active_skill_id="junior_multi_path_planning",
            global_facts={"grade": "初二", "score_level": "年级上游"},
            messages=[
                ChatMessage(role="user", content="孩子初二，成绩年级上游"),
                ChatMessage(role="assistant", content="可以了解初中多元路径规划。"),
                ChatMessage(role="user", content="好，那看看初中多元路径规划"),
            ],
        )
        logger = RuntimeLogger(Path(tempfile.gettempdir()) / "hailiang_test_runtime_fallback.jsonl", context.session_id)
        model_errors: list[tuple[str, bool]] = []
        context.session_meta["model_error_callback"] = (
            lambda exc, terminal: model_errors.append((str(exc), terminal))
        )

        reply, _ = orchestrator._resolve_runtime_reply(
            orchestrator.runtime_registry.get("main_planner"),
            state,
            FakeRuntimeClient(),
            logger,
            context,
        )

        self.assertEqual(reply, "runtime 原生 Skill 回复")
        self.assertNotIn("MS-Agent SkillAnalyzer did not produce an executable plan", reply)
        self.assertEqual(model_errors, [])
        fallback_event = next(
            event
            for event in context.event_trace
            if event.get("event_type") == "ms_agent_runtime"
            and event.get("payload", {}).get("step") == "fallback_runtime"
        )
        self.assertEqual(fallback_event["payload"]["status"], "warning")

    def test_profile_op_empty_stdin_returns_structured_error(self) -> None:
        script_path = PROJECT_RUNTIME_SKILLS_ROOT / "main_planner" / "scripts" / "profile_op.py"
        result = subprocess.run(
            [sys.executable, str(script_path)],
            input="",
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn('"ok": false', result.stdout)
        self.assertIn("empty stdin payload", result.stdout)

    def test_runtime_registry_includes_runtime_and_hailiang_scenes(self) -> None:
        orchestrator = build_orchestrator()
        self.assertIn("main_planner", orchestrator.runtime_registry.bundles)
        self.assertIn("future_explore", orchestrator.runtime_registry.bundles)
        self.assertIn("junior_multi_path_planning", orchestrator.runtime_registry.bundles)
        self.assertIn("mock_admission", orchestrator.runtime_registry.bundles)
        self.assertIn("multi_path_planning", orchestrator.runtime_registry.bundles)
        routes = {
            (route.scene, route.target_skill_id, route.required_global_facts)
            for route in orchestrator.main_bundle.contract.routes
        }
        self.assertIn(("模拟升学", "mock_admission", ()), routes)
        self.assertIn(("多元路径规划", "multi_path_planning", ()), routes)
        self.assertIn(("初中多元路径规划", "junior_multi_path_planning", ()), routes)

    def test_opening_message_comes_from_main_planner_skill_markdown(self) -> None:
        orchestrator = build_orchestrator()

        opening = orchestrator.build_opening_message(parent_name="张女士", profile_name="孩子 1")

        self.assertTrue(opening.startswith("张女士 你好，"))
        self.assertFalse(opening.startswith("孩子 1 你好，"))
        self.assertIn("专属升学规划顾问", opening)
        self.assertIn("先告诉我，孩子是男孩女孩、现在几年级", opening)
        self.assertNotIn("海亮升学助手", opening)

    def test_opening_message_keeps_default_when_profile_name_missing(self) -> None:
        orchestrator = build_orchestrator()

        opening = orchestrator.build_opening_message()

        self.assertTrue(opening.startswith("你好，我是"))
        self.assertIn("专属升学规划顾问", opening)

    def test_config_opening_message_does_not_assume_parent_identity(self) -> None:
        opening = build_session_opening_message(profile_name="孩子 1", parent_name="张女士")

        self.assertTrue(opening.startswith("你好！"))
        self.assertIn("无论你是学生还是家长", opening)
        self.assertNotIn("张女士", opening)
        self.assertNotIn("孩子 1", opening)

    def test_historical_opening_uses_previous_completed_session_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            previous_dir = Path(tmpdir) / "sess_previous"
            previous_dir.mkdir(parents=True)
            (previous_dir / "snapshot.json").write_text(
                json.dumps(
                    {
                        "user_id": "u1",
                        "profile_id": "child_1",
                        "profile_name": "孩子 1",
                        "messages": [
                            {
                                "role": "user",
                                "content": "我想了解美术特长生",
                                "created_at": "2026-07-06T08:55:00",
                            },
                            {
                                "role": "assistant",
                                "content": "可以继续准备美术特长生材料。",
                                "conclusion_summary": "美术特长生准备与材料时间安排",
                                "created_at": "2026-07-06T09:00:00",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            # The most recent session has started but its summary has not yet
            # been generated.  The opening should use the penultimate one.
            latest_dir = Path(tmpdir) / "sess_latest"
            latest_dir.mkdir(parents=True)
            (latest_dir / "snapshot.json").write_text(
                json.dumps(
                    {
                        "user_id": "u1",
                        "profile_id": "child_1",
                        "messages": [
                            {
                                "role": "user",
                                "content": "我还有一个问题",
                                "created_at": "2026-07-06T10:00:00",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("hailiang_skills.core.session_opening_config.SESSION_LOG_ROOT", Path(tmpdir)):
                opening = build_historical_session_opening_message(
                    user_id="u1",
                    profile_id="child_1",
                    profile_name="孩子 1",
                    parent_name="张女士",
                )

        self.assertEqual(
            opening,
            "你好，我们上次聊到了「美术特长生准备与材料时间安排」。这次想聊聊什么？",
        )
        self.assertNotIn("张女士", opening)
        self.assertNotIn("孩子 1", opening)

    def test_streaming_runner_emits_skill_context_before_main_content(self) -> None:
        context = SessionContext(user_id="u1", session_id="sess_stream")
        context.interaction_state["active_skill"] = "main_planner"
        runner = StreamingRunner(
            FakeStreamingRepository(context),
            FakeStreamingFactService(),
            FakeStreamingOrchestrator(),
        )

        events = parse_sse_events(
            list(runner.stream_message("sess_stream", "u1", "给孩子做规划"))
        )
        states = [event["data"] for event in events if event["event"] == "state"]

        self.assertTrue(states)
        self.assertTrue(all(state["protocol"] == "hailiang.sse.v2" for state in states))
        skill_state_index = next(
            index
            for index, state in enumerate(states)
            if state["session"]["active_skill"].get("skill_id") == "main_planner"
        )
        text_state_index = next(
            index for index, state in enumerate(states) if state["assistant"]["content"]
        )
        self.assertLess(skill_state_index, text_state_index)
        self.assertEqual(states[skill_state_index]["session"]["active_skill"]["title"], "升学顾问")

    def test_streaming_runner_emits_lifecycle_when_non_final_turn_has_route_suggestions(self) -> None:
        context = SessionContext(user_id="u1", session_id="sess_stream_suggestions")
        context.interaction_state["active_skill"] = "main_planner"
        orchestrator = build_orchestrator()
        runner = StreamingRunner(
            FakeStreamingRepository(context),
            FakeStreamingFactService(),
            FakeStreamingSuggestionOrchestrator(orchestrator.runtime_registry),
        )

        events = parse_sse_events(
            list(runner.stream_message("sess_stream_suggestions", "u1", "想看看孩子适合什么特长"))
        )
        states = [event["data"] for event in events if event["event"] == "state"]

        self.assertTrue(states)
        self.assertEqual(
            [item["skill_id"] for item in states[-1]["skill_rooms"]],
            ["interest_explore"],
        )

    def test_runtime_stream_empty_content_retries_non_stream_reply(self) -> None:
        context = SessionContext(user_id="u1", session_id="sess_empty_stream")
        streamed_reply_parts: list[str] = []
        context.session_meta["stream_final_reply"] = True
        context.session_meta["reply_delta_callback"] = streamed_reply_parts.append
        orchestrator = build_orchestrator()
        bundle = orchestrator.runtime_registry.bundles["interest_explore"]
        client = FakeReasoningOnlyStreamClient()
        assembly = PromptAssembly(
            core_prompt="core",
            retrieval_prompt="",
            tool_results_prompt="",
            final_prompt="final",
            reference_strategy="progressive",
            assembled_from=("unit-test",),
        )
        logger = RuntimeLogger(Path(tempfile.gettempdir()) / "hailiang_test_empty_stream.jsonl", context.session_id)

        reply, reasoning = orchestrator._stream_runtime_final_text(
            bundle,
            "interest_explore",
            assembly,
            [ChatMessage(role="user", content="有辩论演讲的特长生吗")],
            client,
            logger,
            context,
            phase="runtime_final_response_after_tools",
        )

        self.assertEqual(reply, "这是非流式兜底生成的可见回复。")
        self.assertEqual(reasoning, "我已经想好了，但是没有输出正文。")
        self.assertEqual(client.retry_calls, 1)
        self.assertIn("这是非流式兜底生成的可见回复。", "".join(streamed_reply_parts))
        empty_event = next(
            event
            for event in context.event_trace
            if event.get("event_type") == "ms_agent_runtime"
            and event.get("payload", {}).get("step") == "llm_output_empty"
        )
        self.assertEqual(empty_event["payload"]["status"], "warning")

    def test_streaming_runner_prunes_stale_assistant_when_new_stream_supersedes_old(self) -> None:
        context = SessionContext(user_id="u1", session_id="sess_interrupt")
        runner = StreamingRunner(
            FakeStreamingRepository(context),
            FakeStreamingFactService(),
            FakeInterruptibleStreamingOrchestrator(),
        )
        old_events: list[dict[str, object]] = []

        def consume_old() -> None:
            old_events.extend(parse_sse_events(list(runner.stream_message("sess_interrupt", "u1", "old"))))

        old_thread = threading.Thread(target=consume_old)
        old_thread.start()
        time.sleep(0.02)
        new_events = parse_sse_events(list(runner.stream_message("sess_interrupt", "u1", "new")))
        old_thread.join(timeout=1)

        assistant_contents = [
            message.get("content")
            for message in context.messages
            if message.get("role") == "assistant"
        ]
        self.assertEqual(assistant_contents, ["new reply"])
        old_states = [event["data"] for event in old_events if event["event"] == "state"]
        new_states = [event["data"] for event in new_events if event["event"] == "state"]
        self.assertFalse(any(state["assistant"]["content"] == "old reply" for state in old_states))
        self.assertEqual(new_states[-1]["assistant"]["content"], "new reply")
        self.assertEqual(new_states[-1]["status"], "completed")

    def test_requested_target_skill_is_active_before_stream_finishes(self) -> None:
        context = SessionContext(user_id="u1", session_id="sess_requested_target")
        context.skill_states["skill_runtime"] = {"active_skill_id": "main_planner"}
        context.interaction_state["active_skill"] = "main_planner"
        orchestrator = build_orchestrator()

        _preactivate_requested_target_skill(
            context,
            "junior_multi_path_planning",
            runtime_registry=orchestrator.runtime_registry,
        )

        self.assertEqual(
            context.skill_states["skill_runtime"]["active_skill_id"],
            "junior_multi_path_planning",
        )
        self.assertEqual(context.interaction_state["active_skill"], "junior_multi_path_planning")
        self.assertEqual(context.interaction_state["current_scenario"], "初中多元路径规划")

    def test_facts_sync_round_trips_between_context_and_runtime_state(self) -> None:
        context = SessionContext(user_id="u1")
        context.update_fact("student_province", "浙江", source_skill="test")
        context.update_fact("score_total", 512, source_skill="test")
        state = SessionState(session_id=context.session_id, active_skill_id="main_planner")

        sync_context_to_runtime_state(context, state)
        self.assertEqual(state.global_facts["student_province"], "浙江")
        self.assertEqual(state.global_facts["score_total"], 512)

        state.global_facts["grade"] = "高三"
        sync_runtime_state_to_context(context, state)
        self.assertEqual(context.known_facts.get_value("grade"), "高三")

    def test_main_planner_routes_to_hailiang_mock_admission(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("我想做模拟升学，浙江物理512分", context)

        self.assertIn("浙江", result.assistant_message)
        self.assertIn("512", result.assistant_message)
        self.assertIn("院校层次定位", result.assistant_message)
        self.assertIn("民办本科", result.assistant_message)
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "mock_admission")
        self.assertEqual(context.interaction_state["active_skill"], "admission")
        self.assertEqual(context.interaction_state["current_scenario"], "admission_simulation")
        self.assertEqual(context.interaction_state["current_phase"], "admission_analysis")
        self.assertEqual(context.known_facts.get_value("student_province"), "浙江")
        self.assertEqual(context.known_facts.get_value("subject_group"), "物理")
        self.assertEqual(context.known_facts.get_value("score_total"), 512)
        runtime_facts = context.skill_states["skill_runtime"]["global_facts"]
        self.assertEqual(runtime_facts["student_province"], "浙江")
        self.assertEqual(runtime_facts["subject_group"], "物理")
        self.assertEqual(runtime_facts["score_total"], 512)
        tier_summary = context.skill_states["admission"]["institution_tier_summary"]
        self.assertEqual(tier_summary["tier_name"], "民办本科")
        self.assertEqual(tier_summary["score_range"]["min_score"], 490)
        self.assertEqual(tier_summary["score_range"]["max_score"], 529)

    def test_main_planner_routes_to_hailiang_multi_path_planning(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("帮我看看多元路径规划，浙江物理512分", context)

        self.assertIn("浙江", result.assistant_message)
        self.assertIn("512", result.assistant_message)
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "multi_path_planning")
        self.assertEqual(context.interaction_state["active_skill"], "convergence")
        self.assertEqual(context.interaction_state["current_scenario"], "multi_path_planning")
        self.assertEqual(context.interaction_state["current_phase"], "match_paths")
        runtime_facts = context.skill_states["skill_runtime"]["global_facts"]
        self.assertEqual(runtime_facts["student_province"], "浙江")
        self.assertEqual(runtime_facts["subject_group"], "物理")
        self.assertEqual(runtime_facts["score_total"], 512)

    def test_main_planner_routes_natural_multi_path_query_to_multi_path_planning(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("我想看看除了普通高考还有什么路，浙江物理512分", context)

        self.assertIn("浙江", result.assistant_message)
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "multi_path_planning")
        self.assertEqual(context.interaction_state["active_skill"], "convergence")
        self.assertEqual(context.interaction_state["current_scenario"], "multi_path_planning")
        self.assertEqual(context.interaction_state["current_phase"], "match_paths")

    def test_main_planner_routes_junior_multi_path_query_to_dedicated_skill(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("孩子初二，有美术特长，想看看除了普通中考还有什么多元路径", context)

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "junior_multi_path_planning")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "junior_multi_path_planning")
        self.assertEqual(context.interaction_state["active_skill"], "junior_multi_path_planning")

    def test_explicit_junior_multi_path_scene_name_routes_directly_to_child_skill(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("我想了解初中多元路径规划", context)

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "junior_multi_path_planning")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "junior_multi_path_planning")
        self.assertEqual(context.interaction_state["active_skill"], "junior_multi_path_planning")
        intent_route = context.skill_states["main_planner"]["intent_route"]
        self.assertEqual(intent_route["route_mode"], "direct")
        self.assertEqual(intent_route["scene_name"], "初中多元路径规划")

    def test_explicit_subject_advisor_query_routes_directly_to_child_skill(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("高一怎么选科？", context)

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "subject_advisor")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "subject_advisor")
        self.assertEqual(context.skill_states["skill_runtime"]["status_flags"]["scene_lock"], "task")
        self.assertEqual(
            context.skill_states["main_planner"]["intent_route"]["route_mode"],
            "direct",
        )

    def test_junior_subject_advisor_query_routes_to_subject_advisor_for_preparation(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("初三提前了解高中怎么选科", context)

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "subject_advisor")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "subject_advisor")
        intent_route = context.skill_states["main_planner"]["intent_route"]
        self.assertEqual(intent_route["route_mode"], "direct")
        self.assertIn("初三提前了解高中怎么选科", intent_route["matched_examples"])

    def test_explicit_score_improve_query_routes_directly_to_child_skill(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("孩子成绩一直上不去，怎么提分？", context)

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "score_improve")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "score_improve")

    def test_explicit_mock_admission_query_routes_directly_to_bridge_skill(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("浙江物理512分能上什么学校？", context)

        self.assertIn("院校层次定位", result.assistant_message)
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "mock_admission")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "mock_admission")
        self.assertEqual(context.interaction_state["active_skill"], "admission")

    def test_score_subject_school_query_routes_by_mock_admission_example(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("浙江物理类 580分，看看可以上哪些学校", context)

        self.assertIn("院校层次定位", result.assistant_message)
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "mock_admission")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "mock_admission")
        self.assertEqual(context.interaction_state["active_skill"], "admission")
        intent_route = context.skill_states["main_planner"]["intent_route"]
        self.assertEqual(intent_route["route_mode"], "direct")
        self.assertIn("浙江物理类580分可以上哪些学校", intent_route["matched_examples"])

    def test_unclear_profile_only_query_enters_main_planner(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("孩子初二，成绩中等，有点喜欢画画", context)

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "main_planner")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "main_planner")
        self.assertEqual(context.skill_states["skill_runtime"]["status_flags"]["scene_lock"], "consultative")

    def test_profile_slot_answer_during_main_planner_collection_does_not_jump_to_child_skill(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        orchestrator.handle_message("给孩子做一份生涯规划", context)
        orchestrator.handle_message("小学五年级，女孩，成绩在班级的前 10%", context)
        result = orchestrator.handle_message("孩子喜欢画画", context)

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "main_planner")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "main_planner")
        status_flags = context.skill_states["skill_runtime"]["status_flags"]
        self.assertEqual(status_flags["scene_lock"], "consultative")
        self.assertTrue(status_flags["consultative_lock"])
        intent_route = context.skill_states["main_planner"]["intent_route"]
        self.assertEqual(intent_route["route_mode"], "main_planner")
        self.assertIn("画像补充", intent_route["reason"])

    def test_pending_main_planner_scene_selection_routes_to_junior_multi_path(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初一", source_skill="test")
        context.update_fact("talent", "绘画", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "collect",
            "global_facts": {"grade": "初一", "talent": "绘画"},
            "skill_facts": {"main_planner": {}},
            "stage_facts": {"main_planner": {"collect": {}}},
            "status_flags": {
                "scene_lock": "consultative",
                "consultative_lock": True,
                "collection_complete": False,
                "pending_route_scene": "多元路径规划",
            },
            "route_history": [],
        }

        result = orchestrator.handle_message("初中多元路径吧", context)

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "junior_multi_path_planning")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "junior_multi_path_planning")
        self.assertEqual(context.interaction_state["active_skill"], "junior_multi_path_planning")
        self.assertEqual(context.skill_states["skill_runtime"]["status_flags"]["scene_lock"], "task")
        intent_route = context.skill_states["main_planner"]["intent_route"]
        self.assertEqual(intent_route["route_mode"], "direct")
        self.assertEqual(intent_route["target_skill_id"], "junior_multi_path_planning")

    def test_explicit_interest_explore_request_still_routes_to_child_skill(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("我想做兴趣探索", context)

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "interest_explore")
        self.assertEqual(context.skill_states["skill_runtime"]["status_flags"]["scene_lock"], "task")

    def test_long_multi_info_profile_query_enters_main_planner_before_child_skill(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message(
            "孩子现在高一，成绩大概中等偏上，数学还可以但英语比较弱，平时喜欢画画，"
            "性格有点慢热，最近学习压力比较大，我们也在纠结选科、提分和以后专业方向，"
            "不知道应该先从哪里规划比较合适。",
            context,
        )

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "main_planner")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "main_planner")
        intent_route = context.skill_states["main_planner"]["intent_route"]
        self.assertEqual(intent_route["route_mode"], "main_planner")
        self.assertFalse(intent_route["intent_clear"])
        self.assertIn("画像", intent_route["reason"])
        short_circuit = intent_route["debug_payload"]["routing_short_circuit"]
        self.assertEqual(short_circuit["rule"], "long_profile_message")
        self.assertEqual(short_circuit["stage"], "precheck")
        self.assertTrue(short_circuit["details"]["matched"])
        self.assertGreaterEqual(short_circuit["details"]["char_count"], 80)
        self.assertGreaterEqual(short_circuit["details"]["signal_count"], 3)
        route_event = next(event for event in context.event_trace if event.get("event_type") == "main_planner_route")
        self.assertEqual(route_event["payload"]["route_mode"], "main_planner")
        self.assertEqual(route_event["payload"]["debug_payload"]["routing_short_circuit"]["rule"], "long_profile_message")

    def test_unclear_junior_profile_query_stays_in_main_planner_lock(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("孩子初二，有美术特长，我有点迷糊，不知道该怎么规划", context)

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "main_planner")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "main_planner")
        status_flags = context.skill_states["skill_runtime"]["status_flags"]
        self.assertEqual(status_flags["scene_lock"], "consultative")
        self.assertTrue(status_flags["consultative_lock"])
        intent_route = context.skill_states["main_planner"]["intent_route"]
        self.assertEqual(intent_route["route_mode"], "main_planner")
        self.assertFalse(intent_route["intent_clear"])

    def test_profile_slot_answer_route_event_includes_short_circuit_reason(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        orchestrator.handle_message("给孩子做一份生涯规划", context)
        orchestrator.handle_message("小学五年级，女孩，成绩在班级的前 10%", context)
        orchestrator.handle_message("孩子喜欢画画", context)

        route_events = [event for event in context.event_trace if event.get("event_type") == "main_planner_route"]
        self.assertTrue(route_events)
        last_route_event = route_events[-1]
        self.assertEqual(last_route_event["payload"]["route_mode"], "main_planner")
        self.assertEqual(last_route_event["payload"]["from_skill"], "main_planner")
        self.assertEqual(last_route_event["payload"]["to_skill"], "main_planner")
        short_circuit = last_route_event["payload"]["debug_payload"]["routing_short_circuit"]
        self.assertEqual(short_circuit["rule"], "consultative_profile_slot_answer")
        self.assertEqual(short_circuit["stage"], "precheck")
        self.assertTrue(short_circuit["details"]["consultative_lock"])

    def test_main_planner_asks_grade_before_entering_multi_path_when_stage_unknown(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("想看看除了普通高考还有什么路", context)

        self.assertIn("几年级", result.assistant_message)
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "main_planner")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "main_planner")
        self.assertEqual(context.interaction_state["active_skill"], "main_planner")
        self.assertEqual(context.skill_states["planner"].get("missing_facts"), ["grade"])
        self.assertIsNotNone(context.skill_states["planner"].get("missing_fact_form"))

    def test_main_planner_resumes_multi_path_after_user_supplies_grade(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        first = orchestrator.handle_message("想看看除了普通高考还有什么路", context)
        second = orchestrator.handle_message("初二", context)

        self.assertIn("几年级", first.assistant_message)
        self.assertEqual(second.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.known_facts.get_value("grade"), "初二")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "junior_multi_path_planning")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "junior_multi_path_planning")

    def test_planner_missing_facts_are_replaced_after_user_supplies_form_values(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")

        orchestrator.handle_message("高一，想看看除了普通高考还有什么路", context)
        self.assertEqual(
            context.skill_states["planner"].get("missing_facts"),
            ["score_total", "student_province", "subject_group"],
        )

        orchestrator.handle_message("当前分数：580；高考省份：浙江；选科组合：物理", context)

        next_missing_facts = context.skill_states["planner"].get("missing_facts") or []
        self.assertNotIn("score_total", next_missing_facts)
        self.assertNotIn("student_province", next_missing_facts)
        self.assertNotIn("subject_group", next_missing_facts)
        self.assertNotEqual(
            context.skill_states["planner"].get("question_hint"),
            "请告诉我您所在的省份、选科组合和最近一次模拟考试的总分，这样我可以为您精准筛选除普通高考外的升学路径。",
        )

    def test_structured_fact_update_keeps_multi_path_context_and_marks_unknown_score_avg(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")

        orchestrator.handle_message(
            "高一，想看看除了普通高考还有什么路",
            context,
        )
        orchestrator.handle_message(
            "最近三次大考均分：目前未知；高考省份：浙江；选科组合：物理",
            context,
        )

        self.assertEqual(context.known_facts.get_value("student_province"), "浙江")
        self.assertEqual(context.known_facts.get_value("subject_group"), "物理")
        self.assertIsNone(context.known_facts.get_value("score_recent_avg"))
        self.assertIn(
            "score_recent_avg",
            context.skill_states.get("facts_extractor", {}).get("unknown_fact_keys", []),
        )
        self.assertNotEqual(context.interaction_state["active_skill"], "subject_advisor")
        self.assertNotEqual(context.skill_states["main_planner"]["target_skill"], "subject_advisor")

    def test_normalize_fact_value_treats_explicit_unknown_as_missing(self) -> None:
        self.assertIsNone(normalize_fact_value("score_recent_avg", "目前未知"))
        self.assertIsNone(normalize_fact_value("student_province", "未知"))

    def test_normalize_fact_value_aligns_form_values_for_budget_exam_and_career(self) -> None:
        self.assertEqual(normalize_fact_value("budget_level", "大于5万/年"), ">5万")
        self.assertEqual(normalize_fact_value("budget_level", "5万元以下"), "<5万")
        self.assertEqual(normalize_fact_value("exam_qualification_status", "未通过"), "不合格")
        self.assertEqual(
            normalize_fact_value("career_orientation", ["军校方向", "飞行类"]),
            ["军警类", "飞行员"],
        )

    def test_convergence_filters_out_other_career_paths_when_orientation_selected(self) -> None:
        context = SessionContext(user_id="u1")
        context.update_fact("student_province", "浙江", source_skill="test")
        context.update_fact("subject_group", "物理", source_skill="test")
        context.update_fact("score_total", 600, source_skill="test")
        context.update_fact("career_orientation", ["军校方向"], source_skill="test")
        context.update_fact(
            "focus_primary_categories",
            ["军警招生", "三大招飞"],
            source_skill="test",
        )

        result = ConvergenceSkill(None).run("帮我看下军警方向的多元路径", context)
        categories = [item.get("primary_category") for item in result.candidate_paths]

        self.assertIn("军警招生", categories)
        self.assertNotIn("三大招飞", categories)

    def test_convergence_filters_high_budget_paths_when_budget_is_below_threshold(self) -> None:
        context = SessionContext(user_id="u1")
        context.update_fact("student_province", "浙江", source_skill="test")
        context.update_fact("subject_group", "物理", source_skill="test")
        context.update_fact("score_total", 620, source_skill="test")
        context.update_fact("budget_level", "5万元以下", source_skill="test")
        context.update_fact(
            "focus_primary_categories",
            ["中外合作办学", "港澳升学", "海外升学"],
            source_skill="test",
        )

        result = ConvergenceSkill(None).run("帮我看下国际方向路径", context)
        categories = {item.get("primary_category") for item in result.candidate_paths}

        self.assertFalse({"中外合作办学", "港澳升学", "海外升学"} & categories)

    def test_main_planner_dispatches_to_runtime_native_child_skill(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        result = orchestrator.handle_message("我想做前景探路，高一中等", context)

        self.assertEqual(result.assistant_message, "runtime 原生 Skill 回复")
        self.assertEqual(context.skill_states["main_planner"]["target_skill"], "future_explore")
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "future_explore")
        self.assertEqual(context.interaction_state["active_skill"], "future_explore")

    def test_explicit_career_consultation_keeps_generic_followup_in_selected_skill(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        _preactivate_requested_target_skill(
            context,
            "career_plan_entity",
            runtime_registry=orchestrator.runtime_registry,
            previous_skill_id_override="general_chat",
        )
        context.session_meta["requested_target_skill_id"] = "career_plan_entity"
        orchestrator.handle_message("进入 career_plan_entity", context)
        for message in ("你好", "今天天气怎么样？", "帮我写一个 Python 函数"):
            orchestrator.handle_message(message, context)
            self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "career_plan_entity")
            self.assertEqual(context.interaction_state["active_skill"], "career_plan_entity")

        self.assertEqual(context.skill_states["main_planner"]["intent_route"]["target_skill_id"], "career_plan_entity")

    def test_cross_skill_text_waits_for_requested_target_before_switching(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        context.update_fact("grade", "高一", source_skill="test")
        _preactivate_requested_target_skill(
            context,
            "future_explore",
            runtime_registry=orchestrator.runtime_registry,
        )
        context.session_meta["requested_target_skill_id"] = "future_explore"
        orchestrator.handle_message("进入 future_explore", context)
        orchestrator.handle_message("先岔开一下，我想做兴趣探索", context)

        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "future_explore")
        intent_route = context.skill_states["main_planner"]["intent_route"]
        self.assertEqual(intent_route["route_mode"], "recommend_switch")
        self.assertEqual(intent_route["target_skill_id"], "interest_explore")
        self.assertTrue(intent_route["requires_user_choice"])

        context.session_meta["requested_target_skill_id"] = "interest_explore"
        orchestrator.handle_message("进入 interest_explore", context)

        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "interest_explore")
        self.assertEqual(
            context.skill_states["skill_runtime"]["status_flags"]["resume_to_skill_id"],
            "future_explore",
        )

    def test_child_task_lock_turns_explicit_cross_skill_text_into_recommendation(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        context.update_fact("grade", "高一", source_skill="test")
        _preactivate_requested_target_skill(
            context,
            "subject_advisor",
            runtime_registry=orchestrator.runtime_registry,
        )
        context.session_meta["requested_target_skill_id"] = "subject_advisor"
        orchestrator.handle_message("进入 subject_advisor", context)
        orchestrator.handle_message("孩子成绩中等，也有点迷茫", context)
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "subject_advisor")

        orchestrator.handle_message("先看一下模拟升学，浙江物理512分能上什么学校", context)

        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "subject_advisor")
        self.assertEqual(context.interaction_state["active_skill"], "subject_advisor")
        intent_route = context.skill_states["main_planner"]["intent_route"]
        self.assertEqual(intent_route["route_mode"], "recommend_switch")
        self.assertEqual(intent_route["target_skill_id"], "mock_admission")
        self.assertTrue(intent_route["requires_user_choice"])

    def test_intent_router_uses_embedding_and_degrades_when_embedding_fails(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        router = IntentRouter(
            bundles=registry.bundles,
            main_skill_id="main_planner",
            embedding_client=FakeEmbeddingClient(),
        )
        state = SessionState(session_id="s1", active_skill_id="main_planner")

        decision = router.route("物化生和物化地哪个更适合孩子", state)

        self.assertEqual(decision.target_skill_id, "subject_advisor")
        self.assertEqual(decision.route_mode, "direct")
        self.assertTrue(decision.intent_clear)

        fallback_router = IntentRouter(
            bundles=registry.bundles,
            main_skill_id="main_planner",
            embedding_client=FakeEmbeddingClient(fail=True),
        )
        fallback_decision = fallback_router.route("高一怎么选科？", state)

        self.assertEqual(fallback_decision.target_skill_id, "subject_advisor")
        self.assertEqual(fallback_decision.route_mode, "direct")
        self.assertTrue(fallback_router.embedding_error)

    def test_short_reply_and_cross_skill_text_never_switch_specialist_skill(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        router = IntentRouter(bundles=registry.bundles, main_skill_id="main_planner")
        specialist = SessionState(session_id="s1", active_skill_id="interest_explore")

        short_reply = router.route("第二个", specialist)
        self.assertEqual(short_reply.route_mode, "stay")
        self.assertEqual(short_reply.target_skill_id, "interest_explore")

        router._examples = (
            RouteExample(skill_id="score_improve", scene_name="提分", text="数学提分规划", source="test"),
        )
        recommendation = router.route("数学提分规划", specialist)
        self.assertEqual(recommendation.route_mode, "recommend_switch")
        self.assertEqual(recommendation.target_skill_id, "score_improve")
        self.assertTrue(recommendation.requires_user_choice)

    def test_explicit_multi_path_entry_stays_active_when_grade_is_unknown(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        _preactivate_requested_target_skill(
            context,
            "multi_path_planning",
            runtime_registry=orchestrator.runtime_registry,
        )
        context.session_meta["requested_target_skill_id"] = "multi_path_planning"

        result = orchestrator.handle_message("进入multi_path_planning", context)

        self.assertIn("几年级", result.assistant_message)
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "multi_path_planning")
        self.assertEqual(context.interaction_state["active_skill"], "multi_path_planning")

    def test_explicit_multi_path_entry_uses_known_grade_without_falling_back(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "高一", source_skill="test")
        _preactivate_requested_target_skill(
            context,
            "multi_path_planning",
            runtime_registry=orchestrator.runtime_registry,
        )
        context.session_meta["requested_target_skill_id"] = "multi_path_planning"

        orchestrator.handle_message("进入multi_path_planning", context)

        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "multi_path_planning")
        self.assertNotEqual(context.interaction_state["active_skill"], "main_planner")

    def test_general_chat_candidate_choices_are_ranked_and_config_limited(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        router = IntentRouter(bundles=registry.bundles, main_skill_id="main_planner")

        choices = router._general_chat_candidate_skills(
            {
                "per_skill_scores": [
                    {"skill_id": "subject_advisor", "scene_name": "选科参谋", "final_score": 0.91, "best_reason": "选科问题"},
                    {"skill_id": "future_explore", "scene_name": "前景探路", "final_score": 0.83, "best_reason": "职业问题"},
                    {"skill_id": "interest_explore", "scene_name": "兴趣探索", "final_score": 0.78, "best_reason": "兴趣问题"},
                    {"skill_id": "mock_admission", "scene_name": "模拟升学", "final_score": 0.71, "best_reason": "低于阈值"},
                ]
            }
        )

        self.assertEqual(
            [item["target_skill_id"] for item in choices],
            ["subject_advisor", "future_explore", "interest_explore"],
        )

    def test_general_chat_finalization_persists_multiple_intent_route_buttons(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.interaction_state["active_skill"] = "general_chat"
        context.skill_states = {
            "skill_runtime": {"active_skill_id": "general_chat", "skill_facts": {"general_chat": {}}},
            "main_planner": {
                "intent_route": {
                    "candidate_skills": [
                        {"target_skill_id": "subject_advisor", "scene_name": "选科参谋", "confidence": 0.91, "reason": "选科问题"},
                        {"target_skill_id": "future_explore", "scene_name": "前景探路", "confidence": 0.83, "reason": "职业问题"},
                        {"target_skill_id": "interest_explore", "scene_name": "兴趣探索", "confidence": 0.78, "reason": "兴趣问题"},
                    ]
                }
            },
        }

        payload, _events = build_finalized_payload(
            context,
            assistant_message="我可以先回答当前问题，也为你找到了几个可进一步深入的方向。",
            runtime_registry=orchestrator.runtime_registry,
        )

        self.assertEqual(
            [item["target_skill_id"] for item in payload["route_suggestions"]],
            ["subject_advisor", "future_explore", "interest_explore"],
        )

    def test_general_chat_explicit_skill_invitation_survives_route_analyzer_rejection(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.interaction_state["active_skill"] = "general_chat"
        context.skill_states = {
            "skill_runtime": {"active_skill_id": "general_chat", "skill_facts": {"general_chat": {}}},
            "main_planner": {
                "intent_route": {
                    "candidate_skills": [
                        {
                            "target_skill_id": "multi_path_planning",
                            "scene_name": "多元路径推荐",
                            "confidence": 0.7228,
                            "reason": "除普通高考外的升学路径",
                        }
                    ]
                }
            },
        }

        payload, events = build_finalized_payload(
            context,
            assistant_message=(
                "系统里有专门的「多元路径推荐」顾问，可以帮你梳理适合的路径和时间节点，"
                "你可以点击进入那个板块。"
            ),
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=FakeRouteSuggestionClient(
                '{"analysis_reason":"候选置信度不足","suggestions":['
                '{"target_skill_id":"multi_path_planning","agent_label":"多元路径推荐",'
                '"reason":"除普通高考外的升学路径","confidence":0.7228}'
                ']}'
            ),
            general_chat_card_threshold=0.9,
        )

        self.assertEqual(
            [item["target_skill_id"] for item in payload["route_suggestions"]],
            ["multi_path_planning"],
        )
        self.assertEqual(payload["route_suggestions"][0]["suggestion_source"], "strong_format_fallback")
        analysis_event = next(event for event in events if event.get("event_type") == "route_suggestions_analyzed")
        self.assertTrue(analysis_event["payload"]["fallback_used"])
        self.assertEqual(analysis_event["payload"]["suggestion_source"], "strong_format_fallback")

    def test_embedding_client_batches_sequence_requests(self) -> None:
        requests: list[list[str]] = []

        class FakeHTTPResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb
                return None

        def fake_urlopen(request, timeout=None):
            del timeout
            payload = json.loads(request.data.decode("utf-8"))
            items = payload["input"] if isinstance(payload["input"], list) else [payload["input"]]
            requests.append(list(items))
            return FakeHTTPResponse(
                {
                    "data": [
                        {"embedding": [float(index), float(len(text))]}
                        for index, text in enumerate(items, start=1)
                    ]
                }
            )

        client = EmbeddingClient(
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key="test-key",
            model="text-embedding-v4",
            max_batch_size=10,
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            vectors = client.embed([f"样本-{index}" for index in range(23)])

        self.assertEqual([len(batch) for batch in requests], [10, 10, 3])
        self.assertEqual(client.last_batch_count, 3)
        self.assertEqual(len(vectors), 23)

    def test_intent_router_caches_route_examples_and_reuses_them(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        with tempfile.TemporaryDirectory() as tmpdir:
            first_client = FakeEmbeddingClient()
            first_router = IntentRouter(
                bundles=registry.bundles,
                main_skill_id="main_planner",
                embedding_client=first_client,
                embedding_cache_enabled=True,
                embedding_cache_dir=tmpdir,
            )
            self.assertTrue(first_client.calls)
            self.assertTrue((Path(tmpdir) / "examples.json").exists())
            self.assertEqual(first_router.embedding_error, "")

            second_client = FakeEmbeddingClient()
            second_router = IntentRouter(
                bundles=registry.bundles,
                main_skill_id="main_planner",
                embedding_client=second_client,
                embedding_cache_enabled=True,
                embedding_cache_dir=tmpdir,
            )
            state = SessionState(session_id="s-cache", active_skill_id="main_planner")
            self.assertEqual(second_client.calls, [])
            decision = second_router.route("高一怎么选科？", state)

            self.assertEqual(second_client.calls, [["高一怎么选科？"]])
            self.assertEqual(decision.target_skill_id, "subject_advisor")
            self.assertEqual(decision.debug_payload["embedding"]["cache_hit_count"], len(second_router._examples))
            self.assertEqual(decision.debug_payload["embedding"]["cache_miss_count"], 0)

    def test_intent_router_incrementally_updates_cache_and_removes_stale_entries(self) -> None:
        registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
        with tempfile.TemporaryDirectory() as tmpdir:
            seed_client = FakeEmbeddingClient()
            router = IntentRouter(
                bundles=registry.bundles,
                main_skill_id="main_planner",
                embedding_client=seed_client,
                embedding_cache_enabled=True,
                embedding_cache_dir=tmpdir,
            )
            original_examples = list(router._examples)
            modified_examples = [
                RouteExample(
                    skill_id=item.skill_id,
                    scene_name=item.scene_name,
                    text=f"{item.text}（新增变体）" if index == 0 else item.text,
                    source=item.source,
                )
                for index, item in enumerate(original_examples[:-1])
            ]
            incremental_client = FakeEmbeddingClient()
            router.embedding_client = incremental_client
            router._examples = tuple(modified_examples)
            router._embedding_error = ""
            router._embedding_cache_error = ""
            router._example_embedding_batch_count = 0
            router._embed_examples_if_available()

            self.assertEqual(len(incremental_client.calls), 1)
            self.assertEqual(len(incremental_client.calls[0]), 1)
            self.assertIn("新增变体", incremental_client.calls[0][0])
            state = SessionState(session_id="s-inc", active_skill_id="main_planner")
            decision = router.route("高一怎么选科？", state)
            self.assertEqual(decision.debug_payload["embedding"]["cache_miss_count"], 1)
            self.assertGreater(decision.debug_payload["embedding"]["cache_stale_removed_count"], 0)

    def test_runtime_native_skill_emits_legacy_progress_and_reply_deltas(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")
        statuses: list[dict] = []
        lifecycle_events: list[dict] = []
        deltas: list[str] = []
        context.session_meta["stream_final_reply"] = True
        context.session_meta["status_callback"] = statuses.append
        context.session_meta["lifecycle_callback"] = lifecycle_events.append
        context.session_meta["reply_delta_callback"] = deltas.append

        result = orchestrator.handle_message("我想做前景探路，高一中等", context)

        self.assertEqual(
            statuses,
            [
                {"stage": "intent", "label": "识别本轮需求"},
                {"stage": "planner", "label": "制定规划思路"},
            ],
        )
        self.assertEqual(lifecycle_events, [])
        self.assertEqual("".join(deltas), result.assistant_message)

    def test_message_blocks_are_persisted_on_latest_assistant_message(self) -> None:
        context = SessionContext(user_id="u1")
        context.add_message("user", "测试")
        context.add_message("assistant", "回复")
        blocks = [
            {
                "type": "status_timeline",
                "payload": {
                    "title": "推理进度",
                    "items": [{"stage": "intent", "label": "意图判断", "status": "completed"}],
                },
            },
            {
                "type": "citations",
                "payload": {"groups": [{"kind": "fact", "label": "Fact", "items": []}]},
            },
        ]

        _append_message_blocks_to_latest_assistant(context, blocks)

        assistant = context.messages[-1]
        self.assertEqual(assistant["blocks"], blocks)
        self.assertEqual(assistant["metadata"]["blocks"], blocks)

    def test_finalized_payload_preserves_facts_and_agent_suggestions(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "高一", source_skill="test")

        result = orchestrator.handle_message("我想做前景探路，高一中等", context)
        early_payload, _events = build_finalized_payload(
            context,
            assistant_message=result.assistant_message,
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
        )
        self.assertFalse(early_payload["is_final_summary"])
        self.assertEqual(early_payload["route_suggestions"], [])
        payload, events = build_finalized_payload(
            context,
            assistant_message=(
                "阶段结论：当前更适合先做生涯规划，再结合选科和学校录取结果校验。"
                "孩子目前处在高一中等水平，方向上需要先明确长期兴趣与职业画像，"
                "再把画像映射到选科组合、可报专业和目标学校。"
                "下一步可以继续看选科方案，也可以查看浙江物理类分数对应学校。"
            ),
            facts_delta=[{"key": "grade", "before": None, "after": "高一"}],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=FakeRouteSuggestionClient(
                '{"suggestions":['
                '{"target_skill_id":"subject_advisor","agent_label":"选科参谋","reason":"主回复建议继续看选科方案。","confidence":0.91},'
                '{"target_skill_id":"mock_admission","agent_label":"模拟升学","reason":"主回复建议查看分数对应学校。","confidence":0.89}'
                ']}'
            ),
        )

        self.assertEqual(payload["type"], "finalized")
        self.assertTrue(payload["is_final_summary"])
        self.assertEqual(payload["context_compression"]["source_skill_id"], payload["active_skill"])
        self.assertIn("grade", payload["context_compression"]["facts_snapshot"])
        self.assertEqual(payload["context_compression"]["facts_delta"][0]["key"], "grade")
        self.assertGreaterEqual(len(payload["route_suggestions"]), 2)
        self.assertNotIn(
            payload["active_skill"],
            {item["target_skill_id"] for item in payload["route_suggestions"]},
        )
        self.assertIn("conclusion_summary", context.skill_states["skill_runtime"]["skill_facts"][payload["active_skill"]])
        self.assertIn("skill_finalized", {event.get("event_type") for event in events})

    def test_main_planner_recommendation_summary_creates_junior_route_suggestions(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初三", source_skill="test")
        context.update_fact("talent", "舞蹈", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "analyze",
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        payload, _events = build_finalized_payload(
            context,
            assistant_message=(
                "感谢确认——孩子是初三男生，目标明确指向职高方向，且在舞蹈与绘画方面有扎实兴趣。"
                "结合当前阶段和特长基础，我们可聚焦两条主线："
                "- 主推方向：初中多元路径规划，梳理职高艺术类专业招生要求、文化课底线和面试要点。"
                "- 备选支持：兴趣探索，进一步验证艺术方向适配度。"
                "需要我先为你展开职高艺术类升学路径的具体流程与近期准备动作吗？"
            ),
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=FakeRouteSuggestionClient(
                '{"suggestions":['
                '{"target_skill_id":"junior_multi_path_planning","agent_label":"初中多元路径规划","reason":"主回复建议展开职高艺术类升学路径。","confidence":0.93},'
                '{"target_skill_id":"interest_explore","agent_label":"兴趣探索","reason":"主回复建议验证艺术方向适配度。","confidence":0.88}'
                ']}'
            ),
        )

        self.assertTrue(payload["is_final_summary"])
        targets = [item["target_skill_id"] for item in payload["route_suggestions"]]
        self.assertIn("junior_multi_path_planning", targets)
        self.assertIn("interest_explore", targets)
        self.assertNotIn("mock_admission", targets[:2])

    def test_strong_format_fallback_creates_route_suggestions_without_finalizing(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初一", source_skill="test")
        context.update_fact("talent", "绘画", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "analyze",
            "global_facts": {"grade": "初一", "talent": "绘画"},
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        payload, events = build_finalized_payload(
            context,
            assistant_message=(
                "接下来，为你聚焦三个可落地的方向："
                "🔹 **主推：提分** → 进入【提分】子场景，帮你梳理初一关键学科的提分抓手。"
                "🔸 **备选1：兴趣探索** → 进入【兴趣探索】，系统评估绘画兴趣的发展潜力。"
                "🔸 **备选2：初中多元路径规划** → 进入【初中多元路径规划】，了解艺术特长生准备节点。"
                "你更想优先深入哪一个？"
            ),
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
        )

        self.assertFalse(payload["is_final_summary"])
        targets = [item["target_skill_id"] for item in payload["route_suggestions"]]
        self.assertEqual(targets, ["score_improve", "interest_explore", "junior_multi_path_planning"])
        self.assertNotIn("multi_path_planning", targets)
        self.assertTrue(all(item["suggestion_source"] == "strong_format_fallback" for item in payload["route_suggestions"]))
        event_types = {event.get("event_type") for event in events}
        self.assertIn("route_suggestions_created", event_types)
        self.assertNotIn("skill_finalized", event_types)
        suggestion_event = next(event for event in events if event.get("event_type") == "route_suggestions_created")
        self.assertEqual(suggestion_event["payload"]["suggestion_source"], "strong_format_fallback")
        self.assertFalse(suggestion_event["payload"]["is_final_summary"])

    def test_route_suggestion_llm_keeps_all_valid_suggestions_in_order(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "高一", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "analyze",
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        payload, events = build_finalized_payload(
            context,
            assistant_message="你可以继续看选科、模拟升学、提分规划和兴趣探索四个方向。",
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=FakeRouteSuggestionClient(
                '{"suggestions":['
                '{"target_skill_id":"subject_advisor","agent_label":"选科参谋","reason":"明确建议选科。","confidence":0.91},'
                '{"target_skill_id":"mock_admission","agent_label":"模拟升学","reason":"明确建议模拟升学。","confidence":0.90},'
                '{"target_skill_id":"score_improve","agent_label":"提分规划","reason":"明确建议提分。","confidence":0.88},'
                '{"target_skill_id":"interest_explore","agent_label":"兴趣探索","reason":"明确建议兴趣探索。","confidence":0.86}'
                ']}'
            ),
        )

        self.assertEqual(
            [item["target_skill_id"] for item in payload["route_suggestions"]],
            ["subject_advisor", "mock_admission", "score_improve", "interest_explore"],
        )
        self.assertTrue(all(item["suggestion_source"] == "llm_reply_analysis" for item in payload["route_suggestions"]))
        analysis_event = next(event for event in events if event.get("event_type") == "route_suggestions_analyzed")
        self.assertEqual(analysis_event["payload"]["suggestion_count"], 4)
        self.assertEqual(analysis_event["payload"]["suggestion_source"], "llm_reply_analysis")

    def test_route_suggestion_llm_filters_invalid_targets_and_low_confidence(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "analyze",
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        payload, _events = build_finalized_payload(
            context,
            assistant_message="可进一步选择的规划主题：兴趣探索。你想先从哪个方向开始？",
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=FakeRouteSuggestionClient(
                '{"suggestions":['
                '{"target_skill_id":"main_planner","agent_label":"升学顾问","reason":"当前 skill 不应出现。","confidence":0.99},'
                '{"target_skill_id":"missing_skill","agent_label":"不存在","reason":"不存在 skill。","confidence":0.99},'
                '{"target_skill_id":"score_improve","agent_label":"提分规划","reason":"置信度过低。","confidence":0.70},'
                '{"target_skill_id":"interest_explore","agent_label":"兴趣探索","reason":"有效建议。","confidence":0.83},'
                '{"target_skill_id":"interest_explore","agent_label":"兴趣探索","reason":"重复建议。","confidence":0.82}'
                ']}'
            ),
        )

        self.assertEqual(
            [item["target_skill_id"] for item in payload["route_suggestions"]],
            ["interest_explore"],
        )

    def test_route_suggestion_llm_false_positive_is_blocked_without_choice_context(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初中 二", source_skill="test")
        context.update_fact("gender", "男孩", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "collect",
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        payload, events = build_finalized_payload(
            context,
            assistant_message=(
                "感谢确认——孩子是初中二年级男生。结合初中阶段特点，当前需聚焦学业稳基、"
                "兴趣收敛和目标校梯度意识建立。接下来我想快速确认两点："
                "一是孩子目前在班级或年级的大致位置；二是他平时对哪些领域比较投入。"
                "这两点确认后，我就能为你梳理出一条清晰、可落地的初二升学路径。"
            ),
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=FakeRouteSuggestionClient(
                '{"suggestions":['
                '{"target_skill_id":"interest_explore","agent_label":"兴趣探索","reason":"误判兴趣收敛。","confidence":0.9},'
                '{"target_skill_id":"junior_multi_path_planning","agent_label":"初中多元路径规划","reason":"误判目标校梯度。","confidence":0.85}'
                ']}'
            ),
        )

        self.assertEqual(payload["route_suggestions"], [])
        analysis_event = next(event for event in events if event.get("event_type") == "route_suggestions_analyzed")
        self.assertFalse(analysis_event["payload"]["route_choice_context"])

    def test_final_summary_accepts_high_confidence_llm_suggestions_without_regex_context(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初一", source_skill="test")
        context.update_fact("score_level", "中游", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "analyze",
            "global_facts": {"grade": "初一", "score_level": "中游"},
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        route_client = FakeRouteSuggestionClient(
            '{"suggestions":['
            '{"target_skill_id":"score_improve","agent_label":"提分","reason":"主回复明确建议先聊提分。","confidence":0.95},'
            '{"target_skill_id":"mock_admission","agent_label":"模拟升学","reason":"主回复明确建议先聊模拟升学。","confidence":0.94},'
            '{"target_skill_id":"interest_explore","agent_label":"兴趣探索","reason":"低置信候选应被过滤。","confidence":0.86}'
            ']}'
        )
        payload, events = build_finalized_payload(
            context,
            assistant_message=(
                "明白了，那咱们就专注在学业上，画画就当一个爱好。"
                "整体来看，初一男孩、年级中游，普高线是稳的，但想冲好一点的重点高中，"
                "需要再往上拉一拉。"
                "我建议两个方向你挑一个先聊：\n"
                "**① 提分（主推）**——先梳理孩子各科情况，定一个初一阶段的发力重点。\n"
                "**② 模拟升学**——看看以现在的成绩位置，大概能对应什么梯度的学校。\n"
                "你看先聊哪个？"
            ),
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=route_client,
        )

        self.assertTrue(payload["is_final_summary"])
        self.assertEqual(
            [item["target_skill_id"] for item in payload["route_suggestions"]],
            ["score_improve", "mock_admission"],
        )
        analysis_event = next(event for event in events if event.get("event_type") == "route_suggestions_analyzed")
        self.assertFalse(analysis_event["payload"]["route_choice_context"])
        self.assertEqual(analysis_event["payload"]["confidence_threshold"], 0.90)
        self.assertEqual(analysis_event["payload"]["suggestion_count"], 2)
        self.assertIn("route_choice_context_detected", route_client.calls[0])

    def test_monitor_every_turn_accepts_high_confidence_suggestions_without_summary(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初一", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "collect",
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        route_client = FakeRouteSuggestionClient(
            '{"suggestions":['
            '{"target_skill_id":"score_improve","agent_label":"提分","reason":"用户中途明确转向提分。","confidence":0.94},'
            '{"target_skill_id":"interest_explore","agent_label":"兴趣探索","reason":"低于无上下文阈值。","confidence":0.86}'
            ']}'
        )
        payload, events = build_finalized_payload(
            context,
            assistant_message=(
                "初一阶段我先记住孩子目前还在画像收集过程中。"
                "如果你现在最担心的是成绩拉不上去，我们也可以先顺着成绩问题往下聊。"
            ),
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=route_client,
            monitor_route_suggestions_every_turn=True,
        )

        self.assertFalse(payload["is_final_summary"])
        self.assertEqual(
            [item["target_skill_id"] for item in payload["route_suggestions"]],
            ["score_improve"],
        )
        analysis_event = next(event for event in events if event.get("event_type") == "route_suggestions_analyzed")
        self.assertFalse(analysis_event["payload"]["route_choice_context"])
        self.assertTrue(analysis_event["payload"]["monitor_every_turn"])
        self.assertTrue(analysis_event["payload"]["allow_llm_without_route_context"])
        self.assertEqual(analysis_event["payload"]["confidence_threshold"], 0.90)

    def test_monitor_every_turn_suggests_junior_path_for_talent_continuation_offer(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初二", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "analyze",
            "global_facts": {"grade": "初二", "score_level": "中等", "talent": "美术"},
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        route_client = FakeRouteSuggestionClient('{"suggestions":[]}')
        payload, events = build_finalized_payload(
            context,
            assistant_message=(
                "这个问题问得好，也是很多家长会想的。美术特长生确实文化课分数线更低，"
                "但文化课不是不考了，画画这边也要下真功夫。"
                "要不我接着跟你聊聊，走美术特长生这条路接下来具体要做什么准备？"
                "比如时间怎么安排、要关注哪些信息？"
            ),
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=route_client,
            monitor_route_suggestions_every_turn=True,
        )

        self.assertEqual(
            [item["target_skill_id"] for item in payload["route_suggestions"]],
            ["junior_multi_path_planning"],
        )
        self.assertEqual(payload["route_suggestions"][0]["confidence"], 0.91)
        analysis_event = next(event for event in events if event.get("event_type") == "route_suggestions_analyzed")
        self.assertFalse(analysis_event["payload"]["route_choice_context"])
        self.assertEqual(analysis_event["payload"]["suggestion_count"], 1)
        self.assertIn("美术/艺体特长生路径", payload["route_suggestions"][0]["reason"])

    def test_talent_background_analysis_without_continuation_offer_does_not_suggest_route(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初二", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "analyze",
            "global_facts": {"grade": "初二", "score_level": "中等", "talent": "美术"},
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        route_client = FakeRouteSuggestionClient(
            '{"analysis_reason":"回复只是在询问兴趣投入意向，没有明确提供可点击进入的下一步子场景。","suggestions":[]}'
        )
        payload, events = build_finalized_payload(
            context,
            assistant_message=(
                "从升学的角度看，画画在初中阶段是有实际价值的，"
                "很多好的高中有美术特长生招生通道。"
                "你们现在是怎么想的？是想把画画往升学方向走一走，还是就当个兴趣放松放松？"
            ),
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=route_client,
            monitor_route_suggestions_every_turn=True,
        )

        self.assertEqual(payload["route_suggestions"], [])
        analysis_event = next(event for event in events if event.get("event_type") == "route_suggestions_analyzed")
        self.assertFalse(analysis_event["payload"]["route_choice_context"])
        self.assertEqual(analysis_event["payload"]["suggestion_count"], 0)
        self.assertEqual(
            analysis_event["payload"]["analysis_reason"],
            "回复只是在询问兴趣投入意向，没有明确提供可点击进入的下一步子场景。",
        )

    def test_explicit_choice_options_supplement_missing_score_improve_suggestion(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初中 二", source_skill="test")
        context.update_fact("score_level", "中游", source_skill="test")
        context.update_fact("talent", "美术", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "analyze",
            "global_facts": {"grade": "初中 二", "score_level": "中游", "talent": "美术"},
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        route_client = FakeRouteSuggestionClient(
            '{"suggestions":['
            '{"target_skill_id":"interest_explore","agent_label":"兴趣探索","reason":"明确建议兴趣探索。","confidence":0.9},'
            '{"target_skill_id":"junior_multi_path_planning","agent_label":"初中多元路径规划","reason":"明确建议初中多元路径。","confidence":0.95}'
            ']}'
        )
        payload, events = build_finalized_payload(
            context,
            assistant_message=(
                "接下来，我为你梳理三条可落地的路径方向，供你选择主攻方向：\n"
                "1. **【主推】学业稳基+目标校梯度规划**：明确“保普高、冲优高、搏名校”的三层目标，匹配当前年级位置做学科提分策略；\n"
                "2. **【备选】兴趣收敛与艺术方向探索**：用结构化方式帮孩子判断绘画是否真热爱、能否持续投入，为初三特长路径做前置评估；\n"
                "3. **【备选】初中多元升学路径初筛**：了解本地美术特色高中、公办/民办艺体班、普职融通等选项，提前建立信息认知。\n"
                "你想先从哪一条开始深入？"
            ),
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=route_client,
        )

        self.assertEqual(
            [item["target_skill_id"] for item in payload["route_suggestions"]],
            ["score_improve", "interest_explore", "junior_multi_path_planning"],
        )
        self.assertTrue(all(item["suggestion_source"] == "llm_reply_analysis" for item in payload["route_suggestions"]))
        self.assertIn('"routing_facts"', route_client.calls[0])
        self.assertNotIn('"recent_history"', route_client.calls[0])
        self.assertNotIn('"facts_snapshot"', route_client.calls[0])
        analysis_event = next(event for event in events if event.get("event_type") == "route_suggestions_analyzed")
        self.assertTrue(analysis_event["payload"]["route_choice_context"])

    def test_route_suggestion_llm_failure_does_not_use_broad_keyword_fallback(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初一", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "collect",
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        route_client = FakeRouteSuggestionClient(fail=True)
        payload, events = build_finalized_payload(
            context,
            assistant_message=(
                "孩子现在需要先补充成绩和兴趣信息，后面可能再考虑成绩提升、兴趣探索或多元路径。"
                "请先告诉我目前成绩位置和坚持的活动。"
            ),
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=route_client,
        )

        self.assertEqual(payload["route_suggestions"], [])
        self.assertEqual(route_client.calls, [])
        analysis_event = next(event for event in events if event.get("event_type") == "route_suggestions_analyzed")
        self.assertEqual(analysis_event["payload"]["skipped_reason"], "no_route_choice_context")
        self.assertFalse(analysis_event["payload"]["fallback_used"])

    def test_route_suggestion_generation_does_not_modify_scene_lock_state(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初一", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "analyze",
            "status_flags": {
                "scene_lock": "consultative",
                "consultative_lock": True,
                "resume_to_skill_id": "subject_advisor",
            },
            "skill_facts": {"main_planner": {}},
        }

        build_finalized_payload(
            context,
            assistant_message="建议进入【兴趣探索】继续看看孩子的兴趣画像。",
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
            route_suggestion_client=FakeRouteSuggestionClient(fail=True),
        )

        status_flags = context.skill_states["skill_runtime"]["status_flags"]
        self.assertEqual(context.skill_states["skill_runtime"]["active_skill_id"], "main_planner")
        self.assertEqual(status_flags["scene_lock"], "consultative")
        self.assertTrue(status_flags["consultative_lock"])
        self.assertEqual(status_flags["resume_to_skill_id"], "subject_advisor")

    def test_main_planner_collection_prompt_does_not_create_route_suggestions(self) -> None:
        orchestrator = build_orchestrator()
        context = SessionContext(user_id="u1")
        context.update_fact("grade", "初一", source_skill="test")
        context.update_fact("gender", "男", source_skill="test")
        context.skill_states["skill_runtime"] = {
            "active_skill_id": "main_planner",
            "stage": "collect",
            "status_flags": {"scene_lock": "consultative", "consultative_lock": True},
            "skill_facts": {"main_planner": {}},
        }

        payload, events = build_finalized_payload(
            context,
            assistant_message=(
                "感谢确认——孩子是初中一年级男孩。这个阶段非常适合先把基础画像建立起来，"
                "再决定要往成绩提升、兴趣探索或多元路径规划哪边深入。"
                "接下来，我们需了解两个关键信息：一是目前成绩大概在班级或年级什么位置，"
                "二是平时有没有明显兴趣、特长或正在坚持的活动。"
                "这两点将帮我们判断当前更适合先稳基础，还是先做特长与升学路径的探索。"
                "你简单说说就好。"
            ),
            facts_delta=[],
            runtime_registry=orchestrator.runtime_registry,
        )

        self.assertFalse(payload["is_final_summary"])
        self.assertEqual(payload["route_suggestions"], [])
        self.assertNotIn("skill_finalized", {event.get("event_type") for event in events})

    def test_runtime_prompt_assembly_is_recorded_by_layer(self) -> None:
        orchestrator = build_orchestrator()
        orchestrator.runtime_client = FakeRuntimeClient()
        context = SessionContext(user_id="u1")

        orchestrator.handle_message("孩子初二，有美术特长，想看看除了普通中考还有什么多元路径", context)

        prompt_events = [event for event in context.event_trace if event.get("event_type") == "prompt_assembly"]
        layers = [event.get("payload", {}).get("layer") for event in prompt_events]
        self.assertIn("core", layers)
        self.assertIn("final", layers)
        for event in prompt_events:
            payload = event.get("payload", {})
            self.assertIn("reference_strategy", payload)
            self.assertIn("skill_type", payload)
            self.assertIn("retrieved_count", payload)


if __name__ == "__main__":
    unittest.main()
