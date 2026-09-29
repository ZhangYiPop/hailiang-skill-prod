from hailiang_skills.runtime_bridge.question_progress import (
    detect_answered_question_repetition,
    invalidate_skill_question_unit,
    question_ledger_projection,
    record_assistant_questions,
    reconcile_user_answer,
    register_skill_question_unit,
    resolve_skill_question_unit,
    same_user_message_streak,
    skill_question_unit_projection,
)
from hailiang_skills.skill_runtime.models import ChatMessage, SessionState


def _state_with_question() -> SessionState:
    state = SessionState(
        session_id="question-progress-test",
        messages=[
            ChatMessage(
                role="assistant",
                content=(
                    "他遇到难题放弃，是所有科目都这样，还是只有数学？"
                    "另外，他平时写作业是掐时间、闭卷，还是不限时、能翻书？"
                ),
            )
        ],
    )
    record_assistant_questions(state, "study_question", state.messages[0].content)
    return state


def test_natural_language_answer_closes_only_matching_question():
    state = _state_with_question()

    result = reconcile_user_answer(
        state,
        "study_question",
        "所有的科目都这样，平时遇到事就容易退缩",
    )

    assert result["changed"] is True
    assert len(result["answered"]) == 1
    projection = question_ledger_projection(state, "study_question")
    assert len(projection["answered"]) == 1
    assert len(projection["unresolved"]) == 1


def test_answered_question_is_detected_in_a_later_draft():
    state = _state_with_question()
    reconcile_user_answer(state, "study_question", "所有的科目都这样")

    repeated = detect_answered_question_repetition(
        state.messages[0].content,
        state,
        "study_question",
    )

    assert repeated


def test_unrelated_answer_does_not_close_question():
    state = _state_with_question()

    result = reconcile_user_answer(state, "study_question", "他每天写到晚上十一点")

    assert result["changed"] is False
    assert len(question_ledger_projection(state, "study_question")["unresolved"]) == 2


def test_same_user_message_streak_allows_one_repeat_then_counts_third_turn():
    state = SessionState(
        session_id="same-user-message-streak",
        messages=[
            ChatMessage(role="user", content="孩子遇到难题就放弃怎么办？"),
            ChatMessage(role="assistant", content="先确认一个具体场景。"),
            ChatMessage(role="user", content="孩子遇到难题就放弃怎么办？"),
            ChatMessage(role="assistant", content="请补充孩子通常怎么处理难题。"),
            ChatMessage(role="user", content="孩子遇到难题就放弃怎么办？"),
        ],
    )

    assert same_user_message_streak(state) == 3
    state.messages.pop()
    assert same_user_message_streak(state) == 2


def test_skill_question_unit_keeps_authored_composite_question_intact():
    state = SessionState(session_id="question-unit-test")
    authored_unit = (
        "孩子以前有没有参加过兴趣班、在学校或家里系统学过？大概持续多久？"
        "现在是否还在学；如果已经停了，现在还感兴趣吗、为什么停？"
    )

    registered = register_skill_question_unit(
        state,
        "specialty_middle",
        authored_unit,
        source_message_id="assistant-1",
        stage="尝试盘点",
    )

    assert registered is not None
    assert registered["text"] == authored_unit
    assert registered["status"] == "pending"
    # The business unit is one authored unit, not a list of platform fields.
    assert "questions" not in registered
    resolved = resolve_skill_question_unit(
        state,
        "specialty_middle",
        status="not_applicable",
        evidence="现在还感兴趣，没有停",
        source_message_id="user-2",
    )
    assert resolved is not None
    assert skill_question_unit_projection(state, "specialty_middle")["status"] == "not_applicable"


def test_pending_question_unit_is_invalidated_when_skill_changes():
    state = SessionState(session_id="question-unit-switch-test")
    register_skill_question_unit(state, "skill-a", "孩子现在还感兴趣吗？")

    invalidated = invalidate_skill_question_unit(state, "skill-a", reason="active_skill_changed")

    assert invalidated is not None
    assert invalidated["status"] == "invalidated"
    assert skill_question_unit_projection(state, "skill-a")["invalidated_reason"] == "active_skill_changed"
