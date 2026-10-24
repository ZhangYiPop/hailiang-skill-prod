from types import SimpleNamespace
import time

from hailiang_skills.harness.formal_v3 import FormalV3TurnExecutor
from hailiang_skills.api.routes.harness_chat_stream_v3 import formal_v3_enabled
from test_harness import make_runtime
from test_harness_context_v2 import v2_policy


class _Repository:
    def __init__(self, context):
        self.context = context
        self.saved = 0

    def get(self, _session_id):
        return self.context

    def save(self, context):
        self.context = context
        self.saved += 1


def test_v3_is_pinned_to_the_formal_multi_agent_runtime():
    assert formal_v3_enabled() is True


def test_formal_v3_executor_uses_orchestrator_and_leaves_harness_scheduler_idle(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: None)
    run_id = runtime.create(
        "session", "user", "profile", "请帮我规划", v2_policy(),
        wire_protocol="hailiang.sse.v3", execution_runtime="formal",
    )
    # The normal Harness scheduler must never claim this synthetic record.
    assert runtime.claim(run_id) is None

    context = SimpleNamespace(session_id="session", session_meta={}, messages=[])

    class Orchestrator:
        def handle_message(self, message, target_context):
            assert message == "请帮我规划"
            target_context.messages.append({"role": "assistant", "content": "正式运行时回答"})
            return SimpleNamespace(assistant_message="正式运行时回答")

    repository = _Repository(context)
    executor = FormalV3TurnExecutor(
        repository=repository, orchestrator=Orchestrator(), harness_runtime=runtime,
    )
    # Exercise the worker body synchronously so the assertion has no timing
    # dependency while production still uses a detached V3 worker thread.
    executor._run(run_id=run_id, message="请帮我规划")

    run = runtime.store.get(run_id)
    assert repository.saved == 1
    assert run["status"] == "completed"
    assert run["result"] == "正式运行时回答"
    assert run["tasks"][run["root_task_id"]]["result"] == "正式运行时回答"
    assert "status_callback" not in context.session_meta
    runtime.close()


def test_formal_v3_executor_projects_native_fact_form_and_resumes(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: None)
    run_id = runtime.create(
        "session", "user", "profile", "规划", v2_policy(),
        wire_protocol="hailiang.sse.v3", execution_runtime="formal",
    )
    context = SimpleNamespace(session_id="session", session_meta={}, messages=[])

    class Orchestrator:
        calls = []

        def handle_message(self, message, target_context):
            self.calls.append(message)
            if len(self.calls) == 1:
                target_context.messages.append({
                    "role": "assistant", "content": "请补充信息", "blocks": [{
                        "type": "fact_form", "payload": {"form_id": "profile", "title": "学生情况", "fields": [
                            {"key": "province", "label": "所在省份", "input_type": "text", "required": True},
                        ]},
                    }],
                })
                return SimpleNamespace(assistant_message="请补充信息")
            target_context.messages.append({"role": "assistant", "content": "已结合浙江信息完成分析"})
            return SimpleNamespace(assistant_message="已结合浙江信息完成分析")

    repository = _Repository(context)
    executor = FormalV3TurnExecutor(repository=repository, orchestrator=Orchestrator(), harness_runtime=runtime)
    executor._run(run_id=run_id, message="规划")
    waiting = runtime.store.get(run_id)
    request_id = "formal_input_profile"
    assert waiting["status"] == "waiting_for_user"
    assert waiting["inputs"][request_id]["status"] == "pending"

    # Validate the exact message the adapter gives the formal runtime on V3
    # answer submission; the production adapter resumes on a worker thread.
    assert executor.answer(run_id, request_id=request_id, answers={"province": "浙江"}, text=None, skipped=False)
    for _ in range(100):
        if runtime.store.get(run_id)["status"] == "completed":
            break
        time.sleep(.01)
    assert runtime.store.get(run_id)["status"] == "completed"
    assert "浙江" in runtime.store.get(run_id)["result"]
    runtime.close()


def test_formal_v3_executor_projects_team_handoff_as_a_v3_input(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: None)
    run_id = runtime.create(
        "session", "user", "profile", "帮孩子规划升学", v2_policy(),
        wire_protocol="hailiang.sse.v3", execution_runtime="formal",
    )
    context = SimpleNamespace(session_id="session", session_meta={"expert_team_id": "team-a"}, messages=[])

    class Orchestrator:
        calls = []

        def handle_message(self, message, target_context):
            self.calls.append(message)
            if len(self.calls) == 1:
                target_context.messages.append({
                    "role": "assistant", "content": "请选择专家", "team_handoff": {
                        "handoff_id": "handoff_1", "team_id": "team-a", "reason": "升学问题",
                        "original_user_message": "帮孩子规划升学",
                        "candidates": [{"expert_id": "admission", "mention_name": "升学指导专家"}],
                    },
                })
                return SimpleNamespace(assistant_message="请选择专家")
            assert target_context.session_meta["team_member_switch"]["target_expert_id"] == "admission"
            target_context.messages.append({"role": "assistant", "content": "这是升学建议"})
            return SimpleNamespace(assistant_message="这是升学建议")

    repository = _Repository(context)
    executor = FormalV3TurnExecutor(repository=repository, orchestrator=Orchestrator(), harness_runtime=runtime)
    executor._run(run_id=run_id, message="帮孩子规划升学")
    request_id = "formal_handoff_handoff_1"
    waiting = runtime.store.get(run_id)
    assert waiting["status"] == "waiting_for_user"
    assert waiting["inputs"][request_id]["fields"][0]["options"][0]["value"] == "admission"

    assert executor.answer(run_id, request_id=request_id, answers={"target_expert_id": "admission"}, text=None, skipped=False)
    for _ in range(100):
        if runtime.store.get(run_id)["status"] == "completed":
            break
        time.sleep(.01)
    assert runtime.store.get(run_id)["result"] == "这是升学建议"
    runtime.close()
