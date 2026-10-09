from hailiang_skills.core.context import SessionContext
from hailiang_skills.runtime_bridge.facts import sync_context_to_runtime_state, sync_runtime_state_to_context
from hailiang_skills.runtime_bridge.main_planner import _evaluate_reply_progress, _stage_skill_progress_patch
from hailiang_skills.runtime_bridge.question_progress import record_assistant_questions, reconcile_user_answer
from hailiang_skills.skill_runtime.models import ChatMessage, SessionState
from hailiang_skills.runtime_bridge.context_contract import GENERATION_FAILURE_REPLY, context_contract_version
from hailiang_skills.runtime_bridge.main_planner import MainPlannerOrchestrator, _reply_progress_contract
from types import SimpleNamespace
from hailiang_skills.core.fact_prompt_projection import build_effective_fact_ledger


def test_fact_projection_reports_actual_context_version():
    _, diagnostics = build_effective_fact_ledger(global_facts={}, current_skill_facts={},
        memory_facts={}, context_contract_version=3)
    assert diagnostics["context_contract_version"] == 3


def test_final_prompt_contains_executed_script_results():
    runtime = MainPlannerOrchestrator.__new__(MainPlannerOrchestrator)
    runtime.runtime_bridge_config = SimpleNamespace(active_window_messages=20)
    state = SessionState(session_id="script-result", messages=[ChatMessage(role="user", content="继续")],
        status_flags={"context_contract_version": 3, "ms_agent_runtime": {
            "execution_outputs": [{"ok": True, "json_output": {"recommendations": ["sample"]}}]}})
    messages = runtime._messages_from_assembly(state, SimpleNamespace(final_prompt="base"), ())
    assert '"recommendations": ["sample"]' in messages[0].content
    assert messages[-1].content == "继续"
    state.status_flags["ms_agent_runtime"] = {"execution_outputs": []}
    assert "Executed Script Results" not in runtime._messages_from_assembly(
        state, SimpleNamespace(final_prompt="base"), ())[0].content


def test_model_history_uses_chronological_context_not_duplicated_memory():
    runtime = MainPlannerOrchestrator.__new__(MainPlannerOrchestrator)
    runtime.runtime_bridge_config = SimpleNamespace(active_window_messages=20)
    messages = [ChatMessage(role="user", content="喜欢画画"),
                ChatMessage(role="assistant", content="几年级？"),
                ChatMessage(role="user", content="初二"),
                ChatMessage(role="assistant", content="有过培训吗？"),
                ChatMessage(role="user", content="没有参加过培训班")]
    state = SessionState(session_id="history", messages=messages,
        status_flags={"context_contract_version": 3},
        conversation_memory={"recent_messages": [
            {"role": "user", "content": "初二"},
            {"role": "assistant", "content": "有过培训吗？"}]})
    assert runtime._conversation_messages_for_model(state) == messages
    runtime.runtime_bridge_config.active_window_messages = 3
    assert runtime._conversation_messages_for_model(state) == messages[-3:]


def test_repeated_submission_after_failure_does_not_allow_stale_answer():
    stale = "孩子喜欢画画，不过需要先了解孩子几年级才能给出具体建议。"
    state = SessionState(session_id="repeat", status_flags={"context_contract_version": 3},
        messages=[ChatMessage(role="assistant", content=stale),
                  ChatMessage(role="user", content="初二"),
                  ChatMessage(role="assistant", content=GENERATION_FAILURE_REPLY),
                  ChatMessage(role="user", content="初二")])
    contract = _reply_progress_contract(state, skill_id="sample")
    assert contract["same_user_message_streak"] == 2
    assert not contract["repeated_user_turn_allowed"]
    assert _evaluate_reply_progress(stale, contract)["reasons"] == ["exact_duplicate_reply"]


def test_ordinal_answer_retains_selected_option_and_source():
    state = SessionState(session_id="ordinal", status_flags={"active_turn_id": "turn-choice"})
    record_assistant_questions(state, "expert_direct", "你更关心哪一块？\n\n- 初中阶段怎么走\n- 怎么培养兴趣？")
    result = reconcile_user_answer(state, "expert_direct", "第一个，这条路初中阶段怎么走")
    assert result["changed"]
    assert result["answered"][0]["source_turn_id"] == "turn-choice"
    assert result["answered"][0]["selected_option"] == "初中阶段怎么走"


def test_progress_labels_are_diagnostics_and_inferred_facts_are_not_promoted():
    state = SessionState(session_id="progress", status_flags={"context_contract_version": 3},
                         messages=[ChatMessage(role="user", content="现在是初二")])
    progress = _stage_skill_progress_patch(state, "sample", {
        "confirmed_facts": {"grade": "初二", "city": "杭州"},
        "stage_label": "invented-stage", "pending_topics": ["城市"], "next_action": "再问城市",
    })
    assert state.global_facts == {"grade": "初二"}
    assert progress["pending_topics"] == []
    assert progress["next_action"] == ""
    assert "_pending_skill_progress_transaction" not in state.status_flags


def test_only_exact_duplicate_is_blocked_and_repeat_request_is_allowed():
    previous = "请先了解孩子的学习安排，然后选择每周两次的基础训练。"
    contract = {"context_contract_version": 3, "previous_assistant": previous,
                "recent_assistant_replies": [previous], "repeated_question_ids": []}
    similar = previous + "也可先体验一次。"
    assert _evaluate_reply_progress(similar, contract)["accepted"]
    assert _evaluate_reply_progress(previous, contract)["reasons"] == ["exact_duplicate_reply"]
    contract["repeat_requested"] = True
    assert _evaluate_reply_progress(previous, contract)["accepted"]


def test_answered_question_correction_retries_only_prose_and_preserves_state():
    runtime = MainPlannerOrchestrator.__new__(MainPlannerOrchestrator)
    events, calls = [], []
    runtime._record_events = lambda context, items: events.extend(items)
    question = "孩子喜欢哪种画？是临摹动漫，还是素描？"
    state = SessionState(session_id="prose-only", global_facts={"interest": "素描"},
        messages=[ChatMessage(role="assistant", content=question), ChatMessage(role="user", content="孩子喜欢素描")],
        status_flags={"context_contract_version": 3})
    record_assistant_questions(state, "sample", question)
    reconcile_user_answer(state, "sample", "孩子喜欢素描")
    before_stage = state.stage
    draft = "孩子喜欢素描，很好。" + question
    corrected = "接下来先了解学习经历，再按技能流程判断需要补充的条件。"
    def complete(messages, *args, **kwargs):
        calls.append(messages)
        assert "不得输出最终推荐" in messages[-1].content
        return SimpleNamespace(final_text=corrected)
    context = SessionContext(session_id="prose-only")
    reply, diagnostic = runtime._guard_final_reply_progress(reply=draft, state=state, skill_name="sample",
        messages=state.messages, client=SimpleNamespace(complete_with_tools=complete),
        logger=SimpleNamespace(log=lambda *args, **kwargs: None), context=context, normalize=lambda text: text)
    assert reply == corrected
    assert len(calls) == 1
    assert diagnostic["retry_count"] == 1
    assert state.global_facts == {"interest": "素描"}
    assert state.stage == before_stage
    assert state.status_flags["runtime_question_ledger"]["sample"]["answered"]


def test_fact_projection_preserves_provenance_and_rejects_unsupported_value():
    context = SessionContext(session_id="facts")
    context.add_message("user", "现在是初二", metadata={"turn_id": "turn-1"})
    state = SessionState(session_id="facts", global_facts={"grade": "初二", "city": "杭州"},
                         status_flags={"context_contract_version": 3})
    sync_runtime_state_to_context(context, state)
    assert context.known_facts.get_value("grade") == "初二"
    assert context.known_facts.get_value("city") is None
    sync_context_to_runtime_state(context, state)
    assert state.global_facts == {"grade": "初二"}


def test_normalized_fact_requires_exact_user_evidence():
    context = SessionContext(session_id="normalized")
    context.add_message("user", "没有上过兴趣班，就自己画着玩", metadata={"turn_id": "turn-training"})
    state = SessionState(session_id="normalized", status_flags={"context_contract_version": 3},
        messages=[ChatMessage(role="user", content=context.messages[-1]["content"])])
    _stage_skill_progress_patch(state, "sample", {"confirmed_facts": {
        "training_level": {"value": "未接受系统培训", "evidence": "没有上过兴趣班"},
        "city": {"value": "杭州", "evidence": "在杭州"},
    }})
    sync_runtime_state_to_context(context, state)
    assert context.known_facts.get_value("training_level") == "未接受系统培训"
    assert context.known_facts.get_value("city") is None
    assert context.known_facts.facts["training_level"].source_turn_id == "turn-training"


def test_failed_retry_keeps_user_facts_and_records_actual_failure():
    runtime = MainPlannerOrchestrator.__new__(MainPlannerOrchestrator)
    events = []
    runtime._record_events = lambda context, items: events.extend(items)
    state = SessionState(session_id="failed", global_facts={"grade": "初二"},
        messages=[ChatMessage(role="assistant", content="这是一段上一轮已经完整生成过的回复，需要避免重新发送。"),
                  ChatMessage(role="user", content="走美术中考特长生")],
        status_flags={"context_contract_version": 3})
    stale = state.messages[0].content
    context = SessionContext(session_id="failed", session_meta={"active_turn_id": "turn-current"})
    client = SimpleNamespace(complete_with_tools=lambda *args, **kwargs: SimpleNamespace(final_text=stale))
    reply, diagnostic = runtime._guard_final_reply_progress(reply=stale, state=state, skill_name="sample",
        messages=state.messages, client=client, logger=SimpleNamespace(log=lambda *args, **kwargs: None),
        context=context, normalize=lambda text: text)
    assert reply == GENERATION_FAILURE_REPLY
    assert diagnostic["retry_count"] == 1
    assert state.global_facts == {"grade": "初二"}
    assert context.session_meta["reply_generation_status"] == "failed"
    assert any(item["event_type"] == "reply_generation_failed" for item in events)


def test_unified_switch_can_restore_legacy_contract(monkeypatch):
    monkeypatch.setenv("HAILIANG_UNIFIED_CONTEXT_ENABLED", "false")
    assert context_contract_version() == 2
    monkeypatch.setenv("HAILIANG_UNIFIED_CONTEXT_ENABLED", "true")
    assert context_contract_version() == 3


def test_interrupted_stream_discards_partial_text_and_retries_once():
    runtime = MainPlannerOrchestrator.__new__(MainPlannerOrchestrator)
    events, visible = [], []
    runtime._record_events = lambda context, items: events.extend(items)
    runtime._emit_runtime_status = lambda *args: None
    runtime._record_runtime_prompt = lambda *args, **kwargs: None
    runtime._emit_reasoning_delta = lambda *args: None
    runtime._emit_reply_delta = lambda context, text: visible.append(text)
    runtime._finalize_skill_progress = lambda *args, **kwargs: None

    class InterruptedClient:
        retries = 0

        def stream_complete(self, messages, **kwargs):
            yield SimpleNamespace(content_delta="这是未完成的半句话", reasoning_delta="")
            raise ConnectionError("stream disconnected")

        def complete_with_tools(self, *args, **kwargs):
            self.retries += 1
            return SimpleNamespace(final_text="这是本轮重新生成的完整回答。")

    client = InterruptedClient()
    context = SessionContext(session_id="interrupted")
    state = SessionState(session_id="interrupted", status_flags={"context_contract_version": 3})
    bundle = SimpleNamespace(metadata={}, runtime_metadata=SimpleNamespace(response_policy=None))
    reply, _ = runtime._stream_runtime_final_text(bundle, state, "sample", None,
        [ChatMessage(role="user", content="继续")], client,
        SimpleNamespace(log=lambda *args, **kwargs: None), context, phase="runtime_final_response")
    assert reply == "这是本轮重新生成的完整回答。"
    assert visible == [reply]
    assert client.retries == 1
    assert any(item["event_type"] == "reply_stream_interrupted" for item in events)


def test_conversation_answers_survive_expert_direct_and_skill_switch():
    runtime = MainPlannerOrchestrator.__new__(MainPlannerOrchestrator)
    runtime.main_bundle = SimpleNamespace()
    runtime.runtime_registry = SimpleNamespace(is_enabled=lambda skill: True)
    runtime._expert_authorizes_skill = lambda *args: True
    runtime._split_multi_path_skill_by_stage = lambda *args: None
    runtime._record_events = lambda *args: None
    context = SessionContext(session_id="replay")
    state = SessionState(session_id="replay", active_skill_id="expert_direct",
                         status_flags={"context_contract_version": 3})
    turns = [
        ("孩子喜欢画画可以培养什么特长？", {"interest": "画画"}),
        ("@特长发展专家", {}),
        ("现在是初二", {"grade": "初二"}),
        ("是纯兴趣的。没有上过兴趣班。就自己画着玩", {
            "training_level": {"value": "未接受系统培训", "evidence": "没有上过兴趣班"}}),
        ("嗯？我不是已经回答了", {}),
        ("第一个，这条路初中阶段怎么走", {}),
        ("走美术中考特长生", {"goal": "美术中考特长生"}),
    ]
    for index, (message, facts) in enumerate(turns):
        context.add_message("user", message, metadata={"turn_id": f"turn-{index}"})
        state.messages.append(ChatMessage(role="user", content=message))
        state.status_flags["active_turn_id"] = f"turn-{index}"
        if index == 5:
            record_assistant_questions(state, "expert_direct", "想选哪一块？\n- 初中阶段怎么走\n- 怎么培养兴趣？")
        reconcile_user_answer(state, state.active_skill_id, message)
        _stage_skill_progress_patch(state, state.active_skill_id, {"confirmed_facts": facts, "pending_topics": []})
        sync_runtime_state_to_context(context, state)
        sync_context_to_runtime_state(context, state)
    context.session_meta["expert_requested_skill_id"] = "special_development_plan"
    assert runtime._route_with_main_planner(state, context) == "special_development_plan"
    assert state.global_facts == {"interest": "画画", "grade": "初二", "training_level": "未接受系统培训", "goal": "美术中考特长生"}
    answer = state.status_flags["runtime_question_ledger"]["special_development_plan"]["answered"][0]
    assert answer["selected_option"] == "初中阶段怎么走"
    assert answer["source_turn_id"] == "turn-5"
    assert "expert_requested_skill_id" not in context.session_meta
