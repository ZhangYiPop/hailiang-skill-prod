from __future__ import annotations

from hailiang_skills.runtime_bridge.consultation_outcome import RoundExpertsState, select_final_reply
from hailiang_skills.skill_runtime.models import ChatMessage


class ScriptedClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        assert isinstance(messages[0], ChatMessage)
        return self.text


def test_round_experts_state_to_sse_and_lifecycle():
    state = RoundExpertsState(assigned_expert_ids=["a", "b"])
    state.mark_running("a")
    state.mark_running("b")
    assert state.to_sse()["running_expert_ids"] == ["a", "b"]
    state.mark_completed("a")
    state.mark_failed("b")
    payload = state.to_sse()
    assert payload["completed_expert_ids"] == ["a"]
    assert payload["failed_expert_ids"] == ["b"]
    assert payload["status"] == "partial"
    assert payload["running_expert_ids"] == []


def test_select_final_reply_direct_for_single_expert():
    result = select_final_reply(
        [{"expert_id": "career_plan_expert", "text": "志愿建议", "status": "completed"}]
    )
    assert result == {
        "mode": "direct",
        "text": "志愿建议",
        "source_expert_id": "career_plan_expert",
    }


def test_select_final_reply_failed_when_no_success():
    result = select_final_reply([{"expert_id": "a", "text": "", "status": "failed"}])
    assert result["mode"] == "failed"
    assert result["text"] == ""


def test_select_final_reply_default_concat_merge():
    result = select_final_reply(
        [
            {"expert_id": "a", "mention_name": "A专家", "text": "内容A", "status": "completed"},
            {"expert_id": "b", "mention_name": "B专家", "text": "内容B", "status": "completed"},
        ]
    )
    assert result["mode"] == "merge"
    assert result["source_expert_id"] == ""
    assert "内容A" in result["text"] and "内容B" in result["text"]


def test_select_final_reply_uses_injected_call_tail():
    def call_tail(**kwargs):
        assert len(kwargs["expert_results"]) == 2
        return "合并结果"

    result = select_final_reply(
        [
            {"expert_id": "a", "text": "1", "status": "completed"},
            {"expert_id": "b", "text": "2", "status": "completed"},
        ],
        call_tail=call_tail,
        tail_rules="TAIL",
    )
    assert result == {"mode": "merge", "text": "合并结果", "source_expert_id": ""}


def test_select_final_reply_uses_client_tail_merge():
    client = ScriptedClient("模型合并正文")
    result = select_final_reply(
        [
            {"expert_id": "a", "text": "1", "status": "completed"},
            {"expert_id": "b", "text": "2", "status": "completed"},
        ],
        client=client,
        tail_rules="TAIL",
        user_question="问题",
    )
    assert result["mode"] == "merge"
    assert result["text"] == "模型合并正文"
    assert client.calls == 1
