from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from agent_skill_runtime_core import MSAgentRuntimeProbe

from hailiang_skills.runtime_bridge.conversation_memory import (
    ConversationMemoryStore,
    supplement_questionnaire_evidence,
)
from hailiang_skills.runtime_bridge.main_planner import MainPlannerOrchestrator
from hailiang_skills.runtime_bridge.ms_agent_adapter import MSAgentRuntimeAdapter, SandboxPrepareResult
from hailiang_skills.skill_runtime.models import ChatMessage, SessionState
from hailiang_skills.skill_runtime.session import build_prompt_assembly
from hailiang_skills.skill_runtime.skill_loader import load_skill_bundle_from_directory


class FakeExecutionInput:
    def __init__(self, *, args, stdin, working_dir, requirements):
        self.args = args
        self.stdin = stdin
        self.working_dir = working_dir
        self.requirements = requirements


class FakeSkillContainer:
    SANDBOX_ROOT = "/sandbox"
    install_calls = 0
    execute_calls = 0

    def __init__(self, *, workspace_dir: Path, use_sandbox: bool) -> None:
        self.workspace_dir = workspace_dir
        self.use_sandbox = use_sandbox
        self._skill_dirs = {}

    def mount_skill_directory(self, skill_id: str, skill_dir: Path) -> None:
        self._skill_dirs[skill_id] = skill_dir

    async def _execute_in_sandbox(self, *, shell_command: str, requirements: list[str]):
        del shell_command, requirements
        FakeSkillContainer.install_calls += 1
        return {"exit_code": 0, "stdout": "installed", "stderr": ""}

    async def execute_python_script(self, script_path: Path, *, skill_id: str, input_spec: FakeExecutionInput):
        del skill_id
        FakeSkillContainer.execute_calls += 1
        return {
            "exit_code": 0,
            "stdout": "ok",
            "stderr": "",
            "script_path": str(script_path),
            "stdin_payload": json.loads(input_spec.stdin),
        }


class FakeMemoryLLM:
    def complete(self, messages, *, logger=None) -> str:
        del messages, logger
        return json.dumps(
            {
                "summary": "孩子高一，想做选科规划。",
                "facts": {
                    "global": {
                        "grade": {
                            "value": "高一",
                            "confidence": 0.9,
                            "evidence": "用户明确说孩子高一",
                        }
                    }
                },
            },
            ensure_ascii=False,
        )


def _runtime_probe() -> MSAgentRuntimeProbe:
    return MSAgentRuntimeProbe(
        status="available",
        imports={
            "SkillContainer": FakeSkillContainer,
            "ExecutionInput": FakeExecutionInput,
        },
    )


def _make_script_skill(tmp_path: Path, *, requirements: str = "") -> Path:
    skill_dir = tmp_path / "skill"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: Test\n---\n# Test\n", encoding="utf-8")
    (scripts_dir / "tool.py").write_text("print('ok')\n", encoding="utf-8")
    if requirements:
        (scripts_dir / "requirements.txt").write_text(requirements, encoding="utf-8")
    return skill_dir


def test_ms_agent_adapter_bridges_lowercase_skill_entrypoint(tmp_path: Path) -> None:
    skill_dir = tmp_path / "multi_path_planning"
    skill_dir.mkdir()
    (skill_dir / "skill.md").write_text(
        "---\nname: Multi path\ndescription: Test\nskill_id: multi_path_planning\n---\n",
        encoding="utf-8",
    )
    captured: dict[str, Path] = {}

    class FakeCore:
        def run_single_skill_turn(self, **kwargs):
            captured["skill_dir"] = Path(kwargs["skill_dir"])
            assert (captured["skill_dir"] / "SKILL.md").read_text(encoding="utf-8") == (
                skill_dir / "skill.md"
            ).read_text(encoding="utf-8")
            return "loaded", {}, []

    adapter = MSAgentRuntimeAdapter(runtime_dir=tmp_path / "runtime", runtime_probe=_runtime_probe())
    adapter._runtime_core = lambda: FakeCore()  # type: ignore[method-assign]
    adapter.run_single_skill_turn(skill_dir=skill_dir)

    assert captured["skill_dir"] != skill_dir
    assert "SKILL.md" not in {item.name for item in skill_dir.iterdir()}
    assert not captured["skill_dir"].exists()


def test_sandbox_prepare_schedules_and_reports_running(tmp_path: Path) -> None:
    skill_dir = _make_script_skill(tmp_path)
    adapter = MSAgentRuntimeAdapter(runtime_dir=tmp_path / "runtime", runtime_probe=_runtime_probe())
    started = threading.Event()
    release = threading.Event()

    def blocking_prepare(skill_id: str, skill_dir: Path, prepare_key: str, requirements_hash: str):
        started.set()
        release.wait(timeout=2)
        return SandboxPrepareResult(
            status="success",
            state="success",
            detail="sandbox prepare completed",
            payload={
                "skill_id": skill_id,
                "skill_dir": str(skill_dir),
                "prepare_key": prepare_key,
                "requirements_hash": requirements_hash,
            },
        )

    adapter._prepare_sandbox_sync = blocking_prepare  # type: ignore[method-assign]

    scheduled = adapter.schedule_sandbox_prepare("sample", skill_dir)
    running = adapter.schedule_sandbox_prepare("sample", skill_dir)
    release.set()

    assert started.wait(timeout=1)
    assert scheduled.payload["state"] == "scheduled"
    assert running.payload["state"] == "running"
    for record in adapter._sandbox_prepare_records.values():
        record.future.result(timeout=2)


def test_sandbox_prepare_cache_hit_after_requirements_install(tmp_path: Path) -> None:
    skill_dir = _make_script_skill(tmp_path, requirements="pandas==2.0.0\n")
    runtime_dir = tmp_path / "runtime"
    FakeSkillContainer.install_calls = 0
    first = MSAgentRuntimeAdapter(runtime_dir=runtime_dir, runtime_probe=_runtime_probe())
    first._docker_available = lambda: (True, "docker available")  # type: ignore[method-assign]

    first_result, _first_steps = first.await_sandbox_prepare("sample", skill_dir)
    second = MSAgentRuntimeAdapter(runtime_dir=runtime_dir, runtime_probe=_runtime_probe())
    second._docker_available = lambda: (True, "docker available")  # type: ignore[method-assign]
    second_result, second_steps = second.await_sandbox_prepare("sample", skill_dir)

    assert first_result.state == "success"
    assert second_result.state == "cache_hit"
    assert FakeSkillContainer.install_calls == 1
    assert any(step.name == "sandbox_prepare" and step.payload["state"] == "cache_hit" for step in second_steps)


def test_script_execution_waits_for_prepare_and_uses_script_inputs(tmp_path: Path) -> None:
    skill_dir = _make_script_skill(tmp_path, requirements="pandas==2.0.0\n")
    adapter = MSAgentRuntimeAdapter(runtime_dir=tmp_path / "runtime", runtime_probe=_runtime_probe())
    adapter._docker_available = lambda: (True, "docker available")  # type: ignore[method-assign]
    adapter.await_sandbox_prepare("sample", skill_dir)
    FakeSkillContainer.execute_calls = 0

    outputs, steps = adapter.execute_scripts_in_sandbox(
        skill_id="sample",
        skill_dir=skill_dir,
        loaded_scripts=[{"name": "tool.py", "path": "scripts/tool.py"}],
        execute_scripts=True,
        script_inputs={
            "*": {"query": "fallback"},
            "tool.py": {"query": "hello", "active_skill_id": "sample"},
        },
    )

    assert outputs[0]["stdin_payload"]["query"] == "hello"
    assert outputs[0]["stdin_payload"]["active_skill_id"] == "sample"
    assert FakeSkillContainer.execute_calls == 1
    assert any(step.name == "sandbox_prepare" for step in steps)
    assert any(step.name == "requirements_cache" and step.payload["state"] == "hit" for step in steps)
    assert any(step.name == "script_execution" and step.status == "success" for step in steps)


def test_main_planner_allowlisted_scripts_use_local_fast_path(tmp_path: Path) -> None:
    skill_dir = Path(__file__).resolve().parents[1] / "runtime_skills" / "main_planner"
    sanitizer_path = skill_dir / "scripts" / "__pycache__" / "output_sanitizer.py"
    assert (skill_dir / "scripts" / "profile_op.py").is_file()
    assert sanitizer_path.is_file()

    adapter = MSAgentRuntimeAdapter(runtime_dir=tmp_path / "runtime", runtime_probe=_runtime_probe())
    outputs, steps = adapter.execute_scripts_in_sandbox(
        skill_id="career_plan_entity",
        skill_dir=skill_dir,
        loaded_scripts=[
            {"name": "profile_op.py", "path": "scripts/profile_op.py"},
            {"name": "output_sanitizer.py", "path": "scripts/__pycache__/output_sanitizer.py"},
        ],
        execute_scripts=True,
        script_inputs={
            "profile_op.py": {"action": "read", "query": "给孩子做规划"},
            "*": {"action": "read", "query": "给孩子做规划", "facts": {"grade": "高一"}},
        },
    )

    assert len(outputs) == 2
    assert all(item["execution_mode"] == "local_fast_path" for item in outputs)
    assert all(item["exit_code"] == 0 for item in outputs)
    assert any(step.name == "script_execution" and step.payload["execution_mode"] == "local_fast_path" for step in steps)


def test_sandbox_prepare_blocks_execution_when_docker_is_missing(tmp_path: Path) -> None:
    skill_dir = _make_script_skill(tmp_path)
    adapter = MSAgentRuntimeAdapter(runtime_dir=tmp_path / "runtime", runtime_probe=_runtime_probe())
    adapter._docker_available = lambda: (False, "docker executable not found")  # type: ignore[method-assign]

    outputs, steps = adapter.execute_scripts_in_sandbox(
        skill_id="sample",
        skill_dir=skill_dir,
        loaded_scripts=[{"name": "tool.py", "path": "scripts/tool.py"}],
        execute_scripts=True,
        script_inputs={"tool.py": {"query": "hello"}},
    )

    assert outputs == []
    assert any(step.name == "sandbox_prepare" and step.payload["state"] == "error" for step in steps)
    assert any(
        step.name == "script_execution"
        and step.status == "warning"
        and "Docker sandbox is unavailable" in step.detail
        for step in steps
    )


def test_conversation_memory_updates_summary_facts_and_contract_hash(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "runtime_contract.json").write_text(
        json.dumps({"facts": {"global": ["grade"], "skill": [], "stage": {}}}),
        encoding="utf-8",
    )
    store = ConversationMemoryStore(
        runtime_dir=tmp_path / "runtime",
        enabled=True,
        active_window_messages=2,
    )
    store.append_turn(
        user_id="u1",
        session_id="s1",
        active_skill_id="main_planner",
        user_message="孩子高一，想选科",
        assistant_message="我先记录高一和选科目标。",
    )
    store.append_turn(
        user_id="u1",
        session_id="s1",
        active_skill_id="main_planner",
        user_message="我在浙江",
        assistant_message="接下来结合浙江政策分析。",
    )

    result = store.prepare_for_turn(
        user_id="u1",
        session_id="s1",
        active_skill_id="main_planner",
        skill_dir=skill_dir,
        llm_client=FakeMemoryLLM(),
    )

    assert result.context["summary"] == "孩子高一，想做选科规划。"
    assert result.context["facts"]["global"]["grade"] == "高一"
    assert result.context["recent_messages"] == [
        {"role": "user", "content": "我在浙江"},
        {"role": "assistant", "content": "接下来结合浙江政策分析。"},
    ]
    assert result.context["questionnaire_evidence_messages"] == [
        {
            "role": "user",
            "content": "孩子高一，想选科",
            "source_skill_id": "main_planner",
        },
        {
            "role": "user",
            "content": "我在浙江",
            "source_skill_id": "main_planner",
        },
    ]
    assert result.context["status"]["questionnaire_evidence_messages"] == 2
    assert result.context["status"]["runtime_contract_hash"]
    assert result.context["status"]["summary_updated_through_message_index"] == 2
    assert result.step.status == "success"


def test_questionnaire_evidence_uses_session_history_while_async_memory_is_empty() -> None:
    context = supplement_questionnaire_evidence(
        {"summary": "", "facts": {}, "questionnaire_evidence_messages": []},
        [
            {
                "role": "user",
                "content": "我是高一新生，英语大考能考120，应该在北京高考。",
                "metadata": {},
            },
            {"role": "assistant", "content": "已了解。", "metadata": {}},
            {
                "role": "user",
                "content": "进入multi_path_planning",
                "metadata": {"hidden": True, "message_type": "skill_transition_command"},
            },
        ],
    )

    assert context["questionnaire_evidence_messages"] == [
        {
            "role": "user",
            "content": "我是高一新生，英语大考能考120，应该在北京高考。",
            "source_skill_id": "session_history",
        }
    ]


def test_conversation_memory_keeps_raw_history_until_active_window_is_exceeded(tmp_path: Path) -> None:
    class UnexpectedMemoryLLM:
        def complete(self, messages, *, logger=None) -> str:
            raise AssertionError("memory summary must not run before the active window is exceeded")

    store = ConversationMemoryStore(
        runtime_dir=tmp_path / "runtime",
        enabled=True,
        active_window_messages=8,
    )
    for index in range(4):
        store.append_turn(
            user_id="u1",
            session_id="s1",
            active_skill_id="career_plan_entity",
            user_message=f"用户回答{index}",
            assistant_message=f"助手回复{index}",
        )

    result = store.prepare_for_turn(
        user_id="u1",
        session_id="s1",
        active_skill_id="career_plan_entity",
        skill_dir=None,
        llm_client=UnexpectedMemoryLLM(),
    )

    assert result.step.status == "skipped"
    assert result.context["summary"] == ""
    assert len(result.context["recent_messages"]) == 8
    assert result.context["recent_messages"][-2]["content"] == "用户回答3"
    assert result.context["status"]["summary_target_message_index"] == 0


def test_conversation_memory_schedules_short_skill_turns_on_exit_without_waiting(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "runtime_contract.json").write_text(
        json.dumps({"facts": {"global": ["grade"], "skill": [], "stage": {}}}),
        encoding="utf-8",
    )
    store = ConversationMemoryStore(
        runtime_dir=tmp_path / "runtime",
        enabled=True,
        active_window_messages=8,
    )
    store.append_turn(
        user_id="u1",
        session_id="s1",
        active_skill_id="career_plan_entity",
        user_message="我是高一学生，想了解生涯规划",
        assistant_message="已记录你的年级和目标。",
    )

    class BlockingMemoryLLM:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def complete(self, messages, *, logger=None, request_purpose="unspecified") -> str:
            del messages, logger
            assert request_purpose == "conversation_memory"
            self.started.set()
            assert self.release.wait(timeout=2)
            return FakeMemoryLLM().complete(())

    llm = BlockingMemoryLLM()
    started = time.monotonic()
    result = store.finalize_for_skill_exit(
        user_id="u1",
        session_id="s1",
        active_skill_id="career_plan_entity",
        skill_dir=skill_dir,
        llm_client=llm,
    )

    assert time.monotonic() - started < 0.2
    assert result.step.status == "success"
    assert llm.started.wait(timeout=1)
    assert result.context["summary"] == ""
    assert result.context["recent_messages"] == [
        {"role": "user", "content": "我是高一学生，想了解生涯规划"},
        {"role": "assistant", "content": "已记录你的年级和目标。"},
    ]
    persisted = json.loads(store._memory_path("u1", "s1").read_text(encoding="utf-8"))
    assert persisted["messages"][0]["skill_id"] == "career_plan_entity"
    llm.release.set()


def test_conversation_memory_exit_keeps_raw_turns_when_compaction_is_unavailable(tmp_path: Path) -> None:
    store = ConversationMemoryStore(
        runtime_dir=tmp_path / "runtime",
        enabled=True,
        active_window_messages=8,
    )
    store.append_turn(
        user_id="u1",
        session_id="s1",
        active_skill_id="career_plan_entity",
        user_message="我成绩在年级前列",
        assistant_message="已记录你的成绩情况。",
    )

    result = store.finalize_for_skill_exit(
        user_id="u1",
        session_id="s1",
        active_skill_id="career_plan_entity",
        skill_dir=None,
        llm_client=None,
    )

    assert result.step.status == "warning"
    assert result.context["recent_messages"] == [
        {"role": "user", "content": "我成绩在年级前列"},
        {"role": "assistant", "content": "已记录你的成绩情况。"},
    ]


def test_deferred_conversation_memory_preserves_turn_appended_while_job_runs(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "runtime_contract.json").write_text(
        json.dumps({"facts": {"global": ["grade"], "skill": [], "stage": {}}}),
        encoding="utf-8",
    )

    class BlockingMemoryLLM:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def complete(self, messages, *, logger=None, request_purpose="unspecified") -> str:
            del messages, logger
            assert request_purpose == "conversation_memory"
            self.started.set()
            assert self.release.wait(timeout=2)
            return json.dumps(
                {
                    "summary": "上一轮确认孩子高一。",
                    "facts": {"global": {"grade": {"value": "高一", "confidence": 1}}},
                },
                ensure_ascii=False,
            )

    store = ConversationMemoryStore(
        runtime_dir=tmp_path / "runtime",
        enabled=True,
        active_window_messages=2,
    )
    store.append_turn(
        user_id="u1",
        session_id="s1",
        active_skill_id="general_chat",
        user_message="孩子高一",
        assistant_message="已记录",
    )
    store.append_turn(
        user_id="u1",
        session_id="s1",
        active_skill_id="general_chat",
        user_message="想进入生涯规划",
        assistant_message="可以点击进入",
    )
    llm = BlockingMemoryLLM()

    result = store.prepare_for_turn(
        user_id="u1",
        session_id="s1",
        active_skill_id="career_plan_entity",
        skill_dir=skill_dir,
        llm_client=llm,
        defer_update=True,
    )

    assert result.context["status"]["memory_update_status"] == "scheduled"
    assert result.step.payload["deferred"] is True
    assert result.context["recent_messages"] == []
    assert len(result.context["reference_messages"]) == 4
    assert {item["source_skill_id"] for item in result.context["reference_messages"]} == {"general_chat"}
    assert llm.started.wait(timeout=1)
    store.append_turn(
        user_id="u1",
        session_id="s1",
        active_skill_id="career_plan_entity",
        user_message="进入生涯规划",
        assistant_message="请告诉我所在省份",
    )
    llm.release.set()

    memory_path = store._memory_path("u1", "s1")
    deadline = time.monotonic() + 2
    memory = {}
    while time.monotonic() < deadline:
        memory = json.loads(memory_path.read_text(encoding="utf-8"))
        if memory.get("memory_update_status") == "success":
            break
        time.sleep(0.01)

    assert memory["memory_update_status"] == "success"
    assert len(memory["messages"]) == 6
    assert memory["conversation_summary"] == "上一轮确认孩子高一。"
    assert memory["summary_updated_through_message_index"] == 2


def test_prompt_includes_soul_and_conversation_memory(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: Test\n---\n# Test Skill\n", encoding="utf-8")
    (skill_dir / "runtime_contract.json").write_text("{}", encoding="utf-8")
    bundle = load_skill_bundle_from_directory(skill_dir)
    state = SessionState(
        session_id="s1",
        active_skill_id=bundle.contract.skill_id,
        messages=[ChatMessage(role="user", content="继续")],
        soul_context={"content": "回答要温和、简洁", "content_hash": "abc"},
        conversation_memory={
            "summary": "上一轮确认孩子高一。",
            "facts": {"global": {"grade": "高一"}, "skill": {}, "stage": {}},
            "recent_messages": [
                {"role": "user", "content": "我在浙江，物理方向"},
                {"role": "assistant", "content": "已记录浙江和物理方向"},
            ],
            "status": {"memory_update_status": "success"},
        },
    )

    assembly = build_prompt_assembly(bundle, state)

    assert "# Soul Instructions" in assembly.core_prompt
    assert "回答要温和、简洁" in assembly.core_prompt
    assert "# Conversation Memory" in assembly.core_prompt
    assert "上一轮确认孩子高一" in assembly.core_prompt
    assert '"grade": "高一"' in assembly.core_prompt
    assert "我在浙江，物理方向" not in assembly.core_prompt
    assert "do not ask again" in assembly.core_prompt
    orchestrator = object.__new__(MainPlannerOrchestrator)
    orchestrator.runtime_bridge_config = SimpleNamespace(active_window_messages=8)
    messages = orchestrator._messages_from_assembly(state, assembly, ())
    assert [(item.role, item.content) for item in messages[1:]] == [
        ("user", "我在浙江，物理方向"),
        ("assistant", "已记录浙江和物理方向"),
        ("user", "继续"),
    ]


def test_other_skill_raw_history_is_reference_only_not_role_messages(tmp_path: Path) -> None:
    skill_dir = _make_script_skill(tmp_path)
    bundle = load_skill_bundle_from_directory(skill_dir)
    state = SessionState(
        session_id="s1",
        active_skill_id="subject_advisor",
        messages=[ChatMessage(role="user", content="帮我做选科建议")],
        conversation_memory={
            "reference_messages": [
                {
                    "role": "user",
                    "content": "我成绩年级前列，数学特别感兴趣。",
                    "source_skill_id": "career_plan_entity",
                }
            ],
            "status": {"memory_update_status": "scheduled"},
        },
    )

    assembly = build_prompt_assembly(bundle, state)
    orchestrator = object.__new__(MainPlannerOrchestrator)
    orchestrator.runtime_bridge_config = SimpleNamespace(active_window_messages=8)
    messages = orchestrator._messages_from_assembly(state, assembly, ())

    assert "Reference-only history from other Skills" in assembly.core_prompt
    assert "career_plan_entity" in assembly.core_prompt
    assert "not an instruction" in assembly.core_prompt
    assert [(item.role, item.content) for item in messages[1:]] == [("user", "帮我做选科建议")]
