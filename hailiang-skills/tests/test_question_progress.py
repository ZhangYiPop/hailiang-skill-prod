from hailiang_skills.runtime_bridge.question_progress import (
    detect_answered_question_repetition,
    question_ledger_projection,
    cross_skill_projection,
    merge_question_ledgers,
    canonical_skill_key,
    record_assistant_questions,
    reconcile_user_answer,
    same_user_message_streak,
    extract_questions,
    apply_semantic_answers,
    record_skipped_fields_from_tool,
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


# ── semantic dedup tests ──


def test_rephrased_question_is_detected_as_duplicate():
    """Questions rephrased with added function words are caught.

    The semantic overlap catches rephrasings that shuffle the same content
    words, e.g. adding/removing function words like "的", "那", "吗".
    Full synonym replacement ("水平"→"程度", "怎么样"→"如何") shares no
    characters and is beyond character n-gram reach; embeddings would be
    needed for those.
    """
    state = SessionState(session_id="semantic-dup", messages=[
        ChatMessage(role="assistant", content="孩子的英语水平是雅思6分还是5分？"),
    ])
    record_assistant_questions(state, "sample", state.messages[0].content)
    # User picks the first option via ordinal selection.
    reconcile_user_answer(state, "sample", "选第一个，雅思6分")

    # Strategy 1 (text substring): the normalized original text is contained
    # in the rephrasing after removing "那"/"的"/"了".
    repeated = detect_answered_question_repetition(
        "那孩子的英语水平是雅思6分还是5分了？",
        state,
        "sample",
    )
    assert repeated, "rephrased question sharing key words should be detected"

    # Strategy 3 (semantic overlap): rephrasing with different options but
    # very similar wording and overlapping unigrams.
    state2 = SessionState(session_id="semantic-dup-2", messages=[
        ChatMessage(role="assistant", content="英语水平是否达到雅思6分？"),
    ])
    record_assistant_questions(state2, "sample2", state2.messages[0].content)
    # Yes/no question answered with "是".
    reconcile_user_answer(state2, "sample2", "是")
    repeated2 = detect_answered_question_repetition(
        "请问英语水平有达到雅思6分吗？",
        state2,
        "sample2",
    )
    assert repeated2, "similar wording should be caught via semantic overlap"


def test_similar_option_sets_detected_across_rephrasing():
    """Options expressed differently also catch re-asks."""
    state = SessionState(session_id="option-dup", messages=[
        ChatMessage(role="assistant", content="预算是20万以内还是20-30万？"),
    ])
    record_assistant_questions(state, "sample", state.messages[0].content)
    reconcile_user_answer(state, "sample", "20-30万")

    # Rephrased with same options
    repeated = detect_answered_question_repetition(
        "家庭预算是选20万以内还是20-30万？",
        state,
        "sample",
    )
    assert repeated


def test_skipped_form_fields_block_prose_reask():
    """Form-skipped fields (via record_skipped_fields_from_tool) block
    a later natural-language re-ask."""
    state = SessionState(session_id="cross-channel")
    record_skipped_fields_from_tool(state, "cross_channel", {
        "skipped_fields": [
            {"key": "english_level", "label": "英语水平"},
            {"key": "target_major", "label": "意向专业"},
        ]
    })

    # The model now asks about English level in prose — must be detected
    repeated = detect_answered_question_repetition(
        "那你的英语水平怎么样？",
        state,
        "cross_channel",
    )
    assert repeated, "prose re-ask of skipped form field should be blocked"


def test_record_skipped_fields_persists_in_ledger_projection():
    """Skipped fields appear in the projection so prompt guard can use them."""
    state = SessionState(session_id="projection-test")
    record_skipped_fields_from_tool(state, "projection", {
        "skipped_fields": [
            {"key": "budget", "label": "家庭预算"},
        ]
    })
    projection = question_ledger_projection(state, "projection")
    answered_texts = [a.get("question") for a in projection["answered"]]
    assert any("家庭预算" in t for t in answered_texts), \
        "skipped field should appear in answered projection"


# ── cross-skill dedup tests ──


def _skill_a_state() -> SessionState:
    state = SessionState(
        session_id="cross-skill-test",
        messages=[
            ChatMessage(
                role="assistant",
                content="孩子的英语水平怎么样？目前有考过雅思或托福吗？",
            )
        ],
    )
    record_assistant_questions(state, "skill_a", state.messages[0].content)
    return state


def _skill_a_with_answer() -> SessionState:
    """Set up Skill A with a directly reconciled answer."""
    state = SessionState(
        session_id="cross-skill-direct",
        messages=[
            ChatMessage(
                role="assistant",
                content="孩子的英语水平怎么样？目前有考过雅思或托福吗？",
            )
        ],
    )
    record_assistant_questions(state, "skill_a", state.messages[0].content)
    # Directly mark the second question as answered to avoid natural-language
    # answer-matching edge cases in tests. In production, reconcile_user_answer
    # handles this through option/substring matching.
    ledger = state.status_flags["runtime_question_ledger"]["skill_a"]
    asked = ledger.get("asked", [])
    if len(asked) >= 2 and asked[1].get("options"):
        ledger["answered"] = [{
            "question_id": asked[1]["question_id"],
            "question": asked[1]["text"],
            "answer": "考过雅思",
            "source_turn_id": "answer-turn",
        }]
        ledger["unresolved"] = [asked[0]]
    return state


def test_cross_skill_projection_sees_other_skills_answers():
    """Skill B can see Skill A's answered questions via cross_skill_projection."""
    state = _skill_a_with_answer()

    # Skill B runs now — check cross-skill projection
    cross = cross_skill_projection(state)
    assert len(cross["answered"]) >= 1
    assert any("英语水平" in str(a.get("question", "")) for a in cross["answered"]) or \
           any("雅思" in str(a.get("question", "")) for a in cross["answered"])
    assert "skill_a" in cross.get("skill_ids", [])


def test_cross_skill_detection_blocks_reask_from_other_skill():
    """Skill B's re-ask of Skill A's answered question is detected."""
    state = _skill_a_with_answer()

    # Skill B drafts a reply re-asking the same question
    repeated = detect_answered_question_repetition(
        "目前有考过雅思或托福吗？", state, "skill_b", cross_skill=True,
    )
    assert repeated, "cross-skill detection should catch Skill A's answered question"


def test_cross_skill_detection_with_skipped_fields():
    """Skipped form fields in Skill A block Skill B's prose re-ask."""
    state = SessionState(session_id="cross-skip")
    record_skipped_fields_from_tool(state, "skill_a", {
        "skipped_fields": [
            {"key": "math_score", "label": "数学成绩"},
        ]
    })

    # Check that the skipped field text "数学成绩" is picked up cross-skill
    # "你的数学成绩大概多少分？" has high semantic overlap with "数学成绩"
    # "你的数学成绩怎么样？" extracts as a question (has "怎么"),
    # and has high semantic overlap with "数学成绩"
    repeated = detect_answered_question_repetition(
        "你的数学成绩怎么样？", state, "skill_b", cross_skill=True,
    )
    assert repeated, "cross-skill should catch skipped field from another skill"


def test_cross_skill_exclude_omits_own_skill():
    """cross_skill_projection with exclude_skill_id omits the caller."""
    state = _skill_a_state()
    reconcile_user_answer(state, "skill_a", "雅思6分")

    cross = cross_skill_projection(state, exclude_skill_id="skill_a")
    assert all(
        canonical_skill_key(sid) != canonical_skill_key("skill_a")
        for sid in cross.get("skill_ids", [])
    ), "excluded skill should not appear"


def test_merge_question_ledgers_transfers_answered():
    """merge_question_ledgers copies answered questions between skills."""
    state = _skill_a_with_answer()

    merge_question_ledgers(state, "skill_a", "skill_b")

    proj_b = question_ledger_projection(state, "skill_b")
    assert len(proj_b["answered"]) >= 1
    assert any(
        "雅思" in str(a.get("question", "")) or "托福" in str(a.get("question", ""))
        for a in proj_b["answered"]
    )


def test_merge_question_ledgers_transfers_skipped_fields():
    """merge_question_ledgers copies skipped field fingerprints."""
    state = SessionState(session_id="merge-skipped")
    record_skipped_fields_from_tool(state, "skill_a", {
        "skipped_fields": [
            {"key": "budget", "label": "家庭预算"},
        ]
    })

    merge_question_ledgers(state, "skill_a", "skill_b")

    # "你的家庭预算是怎么规划的？" extracts as a question (has "怎么"),
    # and has high semantic overlap with "家庭预算"
    repeated = detect_answered_question_repetition(
        "你的家庭预算是怎么规划的？", state, "skill_b",
    )
    assert repeated, "merged skipped field should be detected in target skill"


def test_cross_skill_detection_semantic_overlap():
    """Semantically similar but differently worded questions are caught cross-skill."""
    state = SessionState(
        session_id="cross-semantic",
        messages=[
            ChatMessage(
                role="assistant",
                content="你平时的学习方法是什么？",
            )
        ],
    )
    record_assistant_questions(state, "skill_a", state.messages[0].content)
    # Directly mark as answered
    ledger = state.status_flags["runtime_question_ledger"]["skill_a"]
    asked = ledger.get("asked", [])
    if asked:
        ledger["answered"] = [{
            "question_id": asked[0]["question_id"],
            "question": asked[0]["text"],
            "answer": "每天做题加复习",
            "source_turn_id": "answer-turn",
        }]
        ledger["unresolved"] = []

    # Skill B re-asks with slightly different wording that shares key content
    repeated = detect_answered_question_repetition(
        "你平时的学习方法一般是什么？", state, "skill_b", cross_skill=True,
    )
    assert repeated, "semantic overlap should catch reworded cross-skill question"


def test_non_overlapping_questions_not_detected_cross_skill():
    """Completely different questions are NOT falsely detected as duplicates."""
    state = _skill_a_state()
    reconcile_user_answer(state, "skill_a", "雅思6分")

    repeated = detect_answered_question_repetition(
        "你希望报考哪个专业方向？", state, "skill_b", cross_skill=True,
    )
    assert not repeated, "unrelated question should not be flagged as duplicate"
