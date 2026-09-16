from types import SimpleNamespace

from hailiang_skills.core.team_handoff_confirmation import (
    active_handoff_decision,
    block_text_handoff_confirmation,
    is_conservative_handoff_acknowledgement,
)
from hailiang_skills.runtime_bridge.agentscope_expert_runtime import AgentScopeExpertRuntime


def test_conservative_acknowledgement_rejects_new_question_or_hesitation():
    assert is_conservative_handoff_acknowledgement(" 好的，请继续！ ")
    assert is_conservative_handoff_acknowledgement("就按这个来")
    assert not is_conservative_handoff_acknowledgement("好的，但是我还想问数学怎么办")
    assert not is_conservative_handoff_acknowledgement("继续，那孩子顶撞该怎么办？")


def test_active_handoff_prefers_card_and_never_selects_multiple_candidates():
    context = SimpleNamespace(
        session_meta={},
        messages=[{
            "role": "assistant",
            "team_handoff": {"status": "active", "team_id": "team", "candidates": [{"expert_id": "a"}, {"expert_id": "b"}]},
            "interactions": {"team_handoff": {"status": "active"}},
        }],
    )
    assert active_handoff_decision(context, team_id="team", text="继续")["kind"] == "multiple"


def test_pending_intent_recovers_single_candidate_when_card_is_missing():
    context = SimpleNamespace(
        session_meta={"pending_team_handoff_intent": {"status": "active", "team_id": "team", "candidates": [{"expert_id": "a"}]}},
        messages=[],
    )
    assert active_handoff_decision(context, team_id="team", text="麻烦你")["kind"] == "single_recovery"


def test_text_confirmation_expires_old_card_and_issues_new_card_without_selection():
    source = {
        "message_id": "msg_old",
        "role": "assistant",
        "team_handoff": {
            "handoff_id": "handoff_old", "status": "active", "team_id": "team",
            "candidates": [{"expert_id": "a"}],
        },
        "interactions": {"team_handoff": {"status": "active"}},
        "metadata": {"team_handoff": {"status": "active"}},
    }
    context = SimpleNamespace(session_meta={}, messages=[source])
    decision = active_handoff_decision(context, team_id="team", text="同意的")
    fresh = block_text_handoff_confirmation(context, decision)
    assert source["team_handoff"]["status"] == "expired"
    assert source["interactions"]["team_handoff"]["status"] == "expired"
    assert fresh and fresh["status"] == "active"
    assert fresh["handoff_id"] != "handoff_old"
    assert fresh["previous_handoff_id"] == "handoff_old"
    assert context.session_meta["pending_team_handoff"]["handoff_id"] == fresh["handoff_id"]


def test_handoff_fallback_copy_is_layout_independent():
    reply = AgentScopeExpertRuntime._team_handoff_reply({
        "candidates": [{"mention_name": "学习指导师"}], "reason": "该问题更适合专项学习指导。",
    })
    assert "下方" not in reply and "上方" not in reply
    assert "请确认" in reply
