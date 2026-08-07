import json
import threading
from pathlib import Path
from types import SimpleNamespace

from hailiang_skills.api.routes.chat import (
    MessageInteractionUpdateRequest,
    build_chat_router,
)
from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.message_interactions import ACTIVE, EXPIRED, SELECTED
from hailiang_skills.core.streaming_runner import StreamingRunner, _append_message_blocks_to_latest_assistant
from hailiang_skills.core import session_logging
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository
from hailiang_skills.runtime_bridge.main_planner import MainPlannerOrchestrator
from hailiang_skills.skills.base import SkillResult


def _state_payloads(events: list[str]) -> list[dict]:
    payloads: list[dict] = []
    for event in events:
        lines = event.strip().splitlines()
        assert lines[0] == "event: state"
        payloads.append(json.loads(lines[1][6:]))
    return payloads


class _FactService:
    profile_repo = None

    def hydrate_context(self, context):
        return context

    def persist_context(self, context):
        return context


class _RuntimeRegistry:
    def get(self, skill_id):
        return {"skill_id": skill_id} if skill_id in {"subject_advisor", "general_chat"} else None


class _Orchestrator:
    runtime_registry = _RuntimeRegistry()

    def _record_events(self, context, events):
        context.event_trace.extend(events)


class _ExitMemoryStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def finalize_for_skill_exit(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            context={
                "summary": "用户成绩年级前列，数学特别感兴趣。",
                "facts": {"global": {"score_level": "年级前列"}, "skill": {}, "stage": {}},
            },
            step=SimpleNamespace(status="success"),
        )


class _OrchestratorWithExitMemory(_Orchestrator):
    def __init__(self) -> None:
        self.memory_store = _ExitMemoryStore()


class _StreamingOrchestrator(_Orchestrator):
    def handle_message(self, user_message, context):
        context.add_message("user", user_message)
        callback = (context.session_meta or {}).get("status_callback")
        if callable(callback):
            callback({"stage": "intent", "label": "意图判断"})
        context.add_message("assistant", "流式测试回复")
        return SkillResult(assistant_message="流式测试回复")


class _RenamingIntentStreamingOrchestrator(_Orchestrator):
    def handle_message(self, user_message, context):
        context.add_message("user", user_message)
        callback = (context.session_meta or {}).get("status_callback")
        if callable(callback):
            callback(
                {
                    "stage": "intent",
                    "label": "直接回答用户关于浙江物理类580分可报考学校的建议",
                    "detail": "模型返回的完整计划动作",
                    "source": "ms_agent",
                }
            )
        context.add_message("assistant", "流式测试回复")
        return SkillResult(assistant_message="流式测试回复")


class _DeltaStreamingOrchestrator(_Orchestrator):
    runtime_bridge_config = SimpleNamespace(progress_simulation_enabled=False)

    def handle_message(self, user_message, context):
        context.add_message("user", user_message)
        callback = (context.session_meta or {}).get("reply_delta_callback")
        if callable(callback):
            for chunk in ("A", "AB", "ABC"):
                callback(chunk[-1:])
        context.add_message("assistant", "ABC")
        return SkillResult(assistant_message="ABC")


class _NativeFormStreamingOrchestrator(_Orchestrator):
    def handle_message(self, user_message, context):
        context.add_message("user", user_message)
        callback = (context.session_meta or {}).get("status_callback")
        if callable(callback):
            callback({"stage": "intent", "label": "收集信息"})
        context.add_message("assistant", "请填写下方表单")
        context.messages[-1]["blocks"] = [
            {
                "type": "fact_form",
                "payload": {
                    "form_id": "native_question:multi_path_planning:stream",
                    "title": "补充关键信息",
                    "fields": [
                        {
                            "fact_key": "native.subjects",
                            "label": "选科科目",
                            "input_type": "multi_select",
                            "scope": "skill_session",
                        }
                    ],
                },
            }
        ]
        context._ensure_message_ids()
        return SkillResult(assistant_message="请填写下方表单")


class _CancellableStreamingOrchestrator(_Orchestrator):
    def __init__(self) -> None:
        self.first_delta_sent = threading.Event()
        self.release = threading.Event()

    def handle_message(self, user_message, context):
        generation_by_thread = context.session_meta.get("stream_generation_by_thread")
        stream_generation = (
            generation_by_thread.get(str(threading.get_ident()))
            if isinstance(generation_by_thread, dict)
            else ""
        )
        turn_id = "turn_cancel"
        turn_by_generation = context.session_meta.setdefault("turn_id_by_stream_generation", {})
        if isinstance(turn_by_generation, dict) and stream_generation:
            turn_by_generation[stream_generation] = turn_id
        context.add_message("user", user_message, metadata={"turn_id": turn_id})
        callback = (context.session_meta or {}).get("reply_delta_callback")
        if callable(callback):
            callback("已收到的部分回复")
        self.first_delta_sent.set()
        self.release.wait(timeout=2)
        # Simulate a model that would otherwise finish after stop was clicked.
        context.add_message("assistant", "不应保存的完整回复", metadata={"turn_id": turn_id})
        return SkillResult(assistant_message="不应保存的完整回复")


def _route(router, path: str):
    return next(route.endpoint for route in router.routes if getattr(route, "path", "") == path)


def _form_block():
    return {
        "type": "fact_form",
        "payload": {"form_id": "missing_facts_form", "fields": [{"fact_key": "grade"}]},
    }


def test_stream_finalization_preserves_native_questionnaire_form():
    context = SessionContext(session_id="sess_native_form")
    context.add_message("assistant", "请填写下方表单")
    native_form = {
        "type": "fact_form",
        "payload": {
            "form_id": "native_question:multi_path_planning:abc",
            "fields": [{"fact_key": "native.subjects", "label": "选科科目"}],
        },
    }
    context.messages[-1]["blocks"] = [native_form]

    merged = _append_message_blocks_to_latest_assistant(
        context,
        [
            {"type": "status_timeline", "payload": {"items": []}},
            _form_block(),
            {"type": "citations", "payload": {"groups": []}},
        ],
    )

    assert [block["type"] for block in merged] == ["status_timeline", "fact_form", "citations"]
    assert merged[1]["payload"]["form_id"] == "native_question:multi_path_planning:abc"
    assert context.messages[-1]["metadata"]["blocks"] == merged
    interaction_id = "fact_form:native_question:multi_path_planning:abc"
    assert context.messages[-1]["interaction_states"][interaction_id]["status"] == ACTIVE


def test_streamed_state_and_persisted_message_keep_native_questionnaire_form():
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="sess_native_form_stream", user_id="user")
    repository.create(context)
    runner = StreamingRunner(repository, _FactService(), _NativeFormStreamingOrchestrator())

    payloads = _state_payloads(
        list(runner.stream_message(context.session_id, context.user_id, "开始推荐"))
    )

    assert payloads[-1]["form"]["form_id"] == "native_question:multi_path_planning:stream"
    restored = repository.load_from_snapshot(context.session_id)
    assistant = next(message for message in reversed(restored.messages) if message["role"] == "assistant")
    form = next(block for block in assistant["blocks"] if block["type"] == "fact_form")
    assert form["payload"]["form_id"] == "native_question:multi_path_planning:stream"


def test_streaming_intent_label_is_stable_in_sse_and_persisted_timeline():
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="sess_stable_intent", user_id="user")
    repository.create(context)
    orchestrator = _RenamingIntentStreamingOrchestrator()
    orchestrator.runtime_bridge_config = SimpleNamespace(
        progress_simulation_enabled=True,
        progress_simulation_interval_s=0.01,
        progress_simulation_jitter_s=0.0,
        progress_simulation_min_duration_s=0.3,
    )
    runner = StreamingRunner(
        repository,
        _FactService(),
        orchestrator,
    )

    payloads = _state_payloads(
        list(runner.stream_message(context.session_id, context.user_id, "浙江物理类580分"))
    )

    intent_step = next(
        item for item in payloads[-1]["intent"]["steps"] if item["id"] == "intent"
    )
    assert intent_step["label"] == "识别本轮需求"
    assert intent_step["status"] == "completed"
    assert intent_step["detail"] == "模型返回的完整计划动作"
    labels = [item["label"] for item in payloads[-1]["intent"]["steps"]]
    assert labels[0] == "识别本轮需求"
    assert len(labels) >= 3
    assert "正在总结信息" in labels

    restored = repository.load_from_snapshot(context.session_id)
    assistant = next(message for message in reversed(restored.messages) if message["role"] == "assistant")
    timeline = next(block for block in assistant["blocks"] if block["type"] == "status_timeline")
    persisted_step = next(
        item for item in timeline["payload"]["items"] if item["stage"] == "intent"
    )
    assert persisted_step["label"] == "识别本轮需求"
    assert persisted_step["status"] == "completed"
    assert persisted_step["detail"] == "模型返回的完整计划动作"


def test_streaming_assistant_content_is_monotonic_and_cumulative():
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="sess_monotonic_content", user_id="user")
    repository.create(context)
    runner = StreamingRunner(
        repository,
        _FactService(),
        _DeltaStreamingOrchestrator(),
    )

    payloads = _state_payloads(
        list(runner.stream_message(context.session_id, context.user_id, "开始"))
    )
    contents = [
        str(payload["assistant"]["content"])
        for payload in payloads
        if payload["assistant"]["content"]
    ]

    assert contents
    assert contents == sorted(contents, key=len)
    assert all(contents[index].startswith(contents[index - 1]) for index in range(1, len(contents)))
    assert contents[-1] == "ABC"
    for payload in payloads:
        if payload["assistant"]["content"]:
            assert payload["intent"]["status"] == "completed"


def test_new_user_message_expires_active_interactions():
    context = SessionContext(session_id="sess_interactions")
    context.add_message("assistant", "请补充信息")
    context.messages[-1]["blocks"] = [_form_block()]
    context._ensure_message_ids()
    assert context.messages[-1]["interaction_states"]["fact_form:missing_facts_form"]["status"] == ACTIVE

    context.add_message("user", "继续")

    assert context.messages[0]["interaction_states"]["fact_form:missing_facts_form"]["status"] == EXPIRED


def test_fact_form_interaction_is_persisted(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(session_logging, "SESSION_LOG_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session_logging, "USER_LOG_ROOT", tmp_path / "users")
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="sess_form", user_id="user")
    context.add_message("assistant", "请补充信息")
    context.messages[-1]["blocks"] = [_form_block()]
    context._ensure_message_ids()
    repository.create(context)
    endpoint = _route(
        build_chat_router(repository, _Orchestrator(), _FactService()),
        "/sessions/{session_id}/messages/{message_id}/interactions/{interaction_id}",
    )

    response = endpoint(
        context.session_id,
        context.messages[-1]["message_id"],
        "fact_form:missing_facts_form",
        MessageInteractionUpdateRequest(status="submitted", submitted_fact_keys=["grade"]),
    )

    assert response["state"]["status"] == "submitted"
    restored = repository.load_from_snapshot(context.session_id)
    assert restored.messages[-1]["interaction_states"]["fact_form:missing_facts_form"]["status"] == "submitted"


def test_route_transition_selects_current_suggestion():
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="sess_transition", user_id="user")
    context.add_message("assistant", "请选择下一步")
    context.messages[-1]["route_suggestions"] = [{"target_skill_id": "subject_advisor"}]
    context._ensure_message_ids()
    repository.create(context)
    runner = StreamingRunner(repository, _FactService(), _Orchestrator())

    transition = runner.prepare_skill_transition(
        context.session_id,
        "user",
        action="enter",
        target_skill_id="subject_advisor",
        source="route_suggestion",
        source_message_id=context.messages[0]["message_id"],
        source_interaction_id="route_suggestions",
    )

    assert transition["to_skill_id"] == "subject_advisor"
    assert context.messages[0]["interaction_states"]["route_suggestions"]["status"] == SELECTED
    assert context.messages[-1]["metadata"]["message_type"] == "skill_transition"


def test_exit_transition_keeps_history_but_resets_model_context_and_facts(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(session_logging, "SESSION_LOG_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session_logging, "USER_LOG_ROOT", tmp_path / "users")
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="sess_exit", user_id="user")
    context.add_message("user", "请继续帮我做选科规划")
    context.add_message("assistant", "这是退出前的专项建议")
    context.update_fact("student_province", "浙江", source_skill="test", scope="shared")
    context.update_fact("grade", "高一", source_skill="test", scope="profile")
    context.update_fact("score_total", 580, source_skill="test", scope="session")
    context.skill_states = {
        "skill_runtime": {
            "active_skill_id": "subject_advisor",
            "global_facts": {"stale": "should be cleared"},
            "skill_facts": {"subject_advisor": {"old": "value"}},
            "stage_facts": {"subject_advisor": {"analyze": {"old": "value"}}},
            "status_flags": {"pending_route_scene": "选科"},
            "route_history": [{"from": "main_planner", "to": "subject_advisor"}],
            "conversation_memory": {"summary": "old history"},
        },
        "router": {"old": "value"},
        "planner": {"old": "value"},
    }
    repository.create(context)
    runner = StreamingRunner(repository, _FactService(), _Orchestrator())

    transition = runner.prepare_skill_transition(
        context.session_id,
        "user",
        action="exit",
        target_skill_id=None,
        source="exit_button",
    )

    assert transition["context_reset"] is True
    assert context.messages[-2]["role"] == "user"
    assert context.messages[-2]["content"] == "退出AI咨询室"
    assert context.messages[-2]["metadata"]["synthetic"] is True
    assert len(context.messages) == 4
    assert context.skill_states["skill_runtime"]["active_skill_id"] == "general_chat"
    assert context.skill_states["skill_runtime"]["global_facts"] == {
        "student_province": "浙江",
        "grade": "高一",
        "score_total": 580,
    }
    assert "router" not in context.skill_states
    assert MainPlannerOrchestrator._runtime_messages_from_context(None, context) == []
    events = list(
        runner.stream_skill_transition(
            context.session_id,
            "user",
            action="exit",
            target_skill_id=None,
            source="exit_button",
            prepared_transition=transition,
        )
    )
    public_transition = {
        key: value
        for key, value in transition.items()
        if key not in {"facts_snapshot", "handoff_context", "context_message_ids"}
    }
    payloads = _state_payloads(events)
    assert [payload["status"] for payload in payloads] == ["streaming", "streaming", "completed"]
    assert payloads[-1]["protocol"] == "hailiang.sse.v2"
    assert payloads[-1]["skill_transition"] == {
        "action": public_transition["action"],
        "from_skill_id": public_transition["from_skill_id"],
        "to_skill_id": "general_chat",
        "source": public_transition["source"],
    }
    assert payloads[-1]["assistant"]["content"] == "已为你退出 AI 咨询室，如有问题可以继续提问。"
    context.add_message("user", "我想问一个新的问题")
    runtime_messages = MainPlannerOrchestrator._runtime_messages_from_context(None, context)
    assert [(message.role, message.content) for message in runtime_messages] == [("user", "我想问一个新的问题")]
    restored = repository.load_from_snapshot(context.session_id)
    assert len(restored.messages) == 4
    assert restored.session_meta["runtime_context_start_message_id"] == transition["message_id"]


def test_exit_transition_promotes_compacted_memory_facts_before_reset(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(session_logging, "SESSION_LOG_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session_logging, "USER_LOG_ROOT", tmp_path / "users")
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="sess_exit_memory", user_id="user")
    context.skill_states = {
        "skill_runtime": {
            "active_skill_id": "subject_advisor",
            "global_facts": {},
            "skill_facts": {"subject_advisor": {}},
            "stage_facts": {"subject_advisor": {"init": {}}},
        }
    }
    context.interaction_state["active_skill"] = "subject_advisor"
    repository.create(context)
    orchestrator = _OrchestratorWithExitMemory()
    runner = StreamingRunner(repository, _FactService(), orchestrator)

    transition = runner.prepare_skill_transition(
        context.session_id,
        "user",
        action="exit",
        target_skill_id=None,
        source="exit_button",
    )

    assert orchestrator.memory_store.calls
    assert context.known_facts.get_value("score_level") == "年级前列"
    assert context.skill_states["skill_runtime"]["global_facts"]["score_level"] == "年级前列"
    assert transition["memory_exit_finalize"] == {
        "status": "success",
        "promoted_fact_keys": ["score_level"],
    }


def test_messages_stream_records_run_file_and_session_aggregate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(session_logging, "SESSION_LOG_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session_logging, "USER_LOG_ROOT", tmp_path / "users")
    monkeypatch.setattr(session_logging, "DEFAULT_RUNTIME_CONFIG_PATH", tmp_path / "runtime.yml")
    session_logging.load_sse_recording_config.cache_clear()
    (tmp_path / "runtime.yml").write_text(
        f"sse_recording:\n  enabled: true\n  root_dir: {tmp_path / 'sessions'}\n  format: jsonl\n",
        encoding="utf-8",
    )
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="sess_stream", user_id="user")
    repository.create(context)
    runner = StreamingRunner(repository, _FactService(), _StreamingOrchestrator())

    events = list(
        runner.stream_message(
            context.session_id,
            "user",
            "继续",
            protocol="unified-v1",
        )
    )

    assert events
    sse_dir = tmp_path / "sessions" / context.session_id / "sse"
    run_files = sorted(path for path in sse_dir.glob("*.jsonl") if path.name != "session_stream.jsonl")
    assert len(run_files) == 1
    run_records = [json.loads(line) for line in run_files[0].read_text(encoding="utf-8").splitlines()]
    session_records = [
        json.loads(line)
        for line in (sse_dir / "session_stream.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {record["wire_event"] for record in run_records} == {"state"}
    assert all(record["payload"]["protocol"] == "hailiang.sse.v2" for record in run_records)
    assert any(record["internal_event"] == "main_content_end" for record in run_records)
    assert {record["run_id"] for record in session_records} == {run_records[0]["run_id"]}
    assert {record["stream_scope"] for record in run_records} == {"run"}
    assert {record["stream_scope"] for record in session_records} == {"session"}
    session_logging.load_sse_recording_config.cache_clear()


def test_cancel_immediately_marks_current_run_and_preserves_only_received_reply():
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="sess_cancel", user_id="user")
    repository.create(context)
    orchestrator = _CancellableStreamingOrchestrator()
    runner = StreamingRunner(repository, _FactService(), orchestrator)
    lease = runner.reserve_turn(context.session_id, context.user_id)
    stream = runner.stream_message(context.session_id, context.user_id, "请回答", lease=lease)

    first_event = next(stream)
    assert first_event.startswith("event: state\n")
    assert orchestrator.first_delta_sent.wait(timeout=1)
    assert runner.cancel_run(context.session_id, context.user_id, lease.generation)
    assert context.session_meta["cancelled_stream_generation"] == lease.generation
    orchestrator.release.set()

    events = list(stream)
    payloads = _state_payloads(events)
    assert any(payload["status"] == "stopped" for payload in payloads)
    assert not any(payload["assistant"]["content"] == "不应保存的完整回复" for payload in payloads)
    assistant_messages = [message for message in context.messages if message.get("role") == "assistant"]
    assert [message["content"] for message in assistant_messages] == ["已收到的部分回复"]
    assert assistant_messages[0]["generation_status"] == "cancelled"


def test_skill_transition_records_transition_run_and_followup_run(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(session_logging, "SESSION_LOG_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session_logging, "USER_LOG_ROOT", tmp_path / "users")
    monkeypatch.setattr(session_logging, "DEFAULT_RUNTIME_CONFIG_PATH", tmp_path / "runtime.yml")
    session_logging.load_sse_recording_config.cache_clear()
    (tmp_path / "runtime.yml").write_text(
        f"sse_recording:\n  enabled: true\n  root_dir: {tmp_path / 'sessions'}\n  format: jsonl\n",
        encoding="utf-8",
    )
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="sess_transition_stream", user_id="user")
    context.add_message("assistant", "请选择下一步")
    context.messages[-1]["route_suggestions"] = [{"target_skill_id": "subject_advisor"}]
    context._ensure_message_ids()
    repository.create(context)
    runner = StreamingRunner(repository, _FactService(), _StreamingOrchestrator())

    transition = runner.prepare_skill_transition(
        context.session_id,
        "user",
        action="enter",
        target_skill_id="subject_advisor",
        source="route_suggestion",
        source_message_id=context.messages[0]["message_id"],
        source_interaction_id="route_suggestions",
    )
    list(
        runner.stream_skill_transition(
            context.session_id,
            "user",
            action="enter",
            target_skill_id="subject_advisor",
            source="route_suggestion",
            prepared_transition=transition,
            protocol="unified-v1",
        )
    )

    sse_dir = tmp_path / "sessions" / context.session_id / "sse"
    run_files = sorted(path for path in sse_dir.glob("*.jsonl") if path.name != "session_stream.jsonl")
    assert len(run_files) == 1
    session_records = [
        json.loads(line)
        for line in (sse_dir / "session_stream.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len({record["run_id"] for record in session_records}) == 1
    assert any(record["source_endpoint"] == "sessions/chat/stream" for record in session_records)
    assert {record["wire_event"] for record in session_records} == {"state"}
    assert any(
        (record["payload"].get("skill_transition") or {}).get("to_skill_id") == "subject_advisor"
        for record in session_records
    )
    assert any(record["internal_event"] == "main_content_end" for record in session_records)
    session_logging.load_sse_recording_config.cache_clear()
