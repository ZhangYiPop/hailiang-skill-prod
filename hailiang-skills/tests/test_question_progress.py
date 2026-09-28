from hailiang_skills.runtime_bridge.question_progress import (
    detect_answered_question_repetition,
    question_ledger_projection,
    record_assistant_questions,
    reconcile_user_answer,
    same_user_message_streak,
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
