from hailiang_skills.runtime_bridge.question_progress import (
    detect_answered_question_repetition,
    question_ledger_projection,
    record_assistant_questions,
    reconcile_user_answer,
    same_user_message_streak,
    extract_questions,
    apply_semantic_answers,
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


def test_paraphrased_option_answer_closes_question():
    state = SessionState(
        session_id="question-progress-paraphrase",
        messages=[
            ChatMessage(
                role="assistant",
                content="孩子目前是单纯享受画画的过程，还是已经想往专业方向走？",
            )
        ],
    )
    record_assistant_questions(state, "specialty_middle", state.messages[0].content)

    result = reconcile_user_answer(
        state,
        "specialty_middle",
        "孩子是享受画画这个过程",
    )

    assert result["changed"] is True
    assert len(question_ledger_projection(state, "specialty_middle")["answered"]) == 1


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


def test_explanation_is_not_a_choice_and_negative_course_answer_is_evidence():
    text = ("画画的分支很多。想先了解一下：孩子平时是随手涂鸦、临摹动漫，还是上过系统课程？"
            "这能帮我判断更适合往艺术特长还是兴趣方向发展。")
    questions = extract_questions(text)
    assert len(questions) == 1
    assert "分支很多" not in questions[0]["text"]
    state = SessionState(session_id="negative-course", status_flags={"active_turn_id": "answer-turn"})
    record_assistant_questions(state, "sample", text)
    result = reconcile_user_answer(state, "sample", "孩子平时是随手涂鸦，也临摹的。没有上过课程")
    assert result["changed"]
    assert result["unresolved"] == []
    assert result["answered"][0]["source_turn_id"] == "answer-turn"
    assert not extract_questions("请确认是否由这位专家接管回答。")


def test_negative_predicate_does_not_resolve_unrelated_question():
    state = _state_with_question()
    assert not reconcile_user_answer(state, "study_question", "没有上过课程")["changed"]


def test_semantic_answer_accepts_evidence_and_tracks_partial_information():
    state = SessionState(session_id="semantic", status_flags={"active_turn_id": "turn-answer"})
    record_assistant_questions(state, "sample", "接受过系统训练吗？所在城市和预算是什么？")
    questions = question_ledger_projection(state, "sample")["unresolved"]
    state.messages.append(ChatMessage(role="user", content="没有系统的训练过，在杭州"))
    result = apply_semantic_answers(state, "sample", [
        {"question_id": questions[0]["question_id"], "status": "answered", "evidence": "没有系统的训练过"},
        {"question_id": questions[1]["question_id"], "status": "partial", "evidence": "在杭州", "missing": "预算"},
        {"question_id": "invented", "status": "answered", "evidence": "在杭州"},
        {"question_id": questions[1]["question_id"], "status": "answered", "evidence": "预算两万元"},
    ])
    projection = question_ledger_projection(state, "sample")
    assert len(projection["answered"]) == 1
    assert projection["answered"][0]["source_turn_id"] == "turn-answer"
    assert len(projection["unresolved"]) == 1
    assert projection["unresolved"][0]["answer_evidence"]["missing"] == "预算"
    assert len(result["rejected_question_ids"]) == 2
