from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.conversation_facts import apply_model_context_updates
from hailiang_skills.skills.facts_extractor import FactsExtractorSkill


def test_model_context_updates_are_evidence_checked_and_session_scoped():
    context = SessionContext(profile_id="profile_child")
    context.update_fact("grade", "高一", source_skill="profile", scope="profile")
    context.add_message("user", "孩子现在4 年级，平时喜欢画画")
    message_id = context.messages[-1]["message_id"]

    result = apply_model_context_updates(context, {
        "identity": {
            "role": "parent", "source_message_id": message_id,
            "evidence": "孩子现在", "confidence": 0.94,
        },
        "facts": [
            {"key": "grade", "value": "4 年级", "source_message_id": message_id,
             "evidence": "4 年级", "confidence": 0.96},
            {"key": "interest_domains", "value": ["画画"], "source_message_id": message_id,
             "evidence": "喜欢画画", "confidence": 0.9},
        ],
    }, source="test_model")

    assert result["accepted_fact_keys"] == ["grade", "interest_domains"]
    assert context.session_facts.get_value("grade") == "四年级"
    assert context.profile_facts.get_value("grade") == "高一"
    assert context.session_facts.get_value("interest_domains") == ["画画"]
    assert context.session_meta["conversation_identity"]["role"] == "parent"
    assert context.session_facts.facts["grade"].source_id == message_id
    assert context.session_facts.facts["grade"].scope == "session"


def test_invalid_or_older_evidence_does_not_overwrite_current_session_facts():
    context = SessionContext()
    context.add_message("user", "孩子现在五年级")
    older_id = context.messages[-1]["message_id"]
    context.add_message("user", "其实已经六年级了")
    newer_id = context.messages[-1]["message_id"]
    apply_model_context_updates(context, {"facts": [{
        "key": "grade", "value": "六年级", "source_message_id": newer_id,
        "evidence": "六年级", "confidence": 0.95,
    }]}, source="test_model")

    result = apply_model_context_updates(context, {"facts": [
        {"key": "grade", "value": "五年级", "source_message_id": older_id,
         "evidence": "五年级", "confidence": 0.95},
        {"key": "grade", "value": "三年级", "source_message_id": newer_id,
         "evidence": "不存在的证据", "confidence": 0.99},
    ]}, source="test_model")

    assert context.session_facts.get_value("grade") == "六年级"
    assert len(result["rejected"]) == 2
    event = context.event_trace[-1]
    assert event["event_type"] == "conversation_fact_extraction"
    assert event["payload"]["status"] == "rejected"


def test_unknown_identity_does_not_clear_a_confirmed_role():
    context = SessionContext()
    context.add_message("user", "我是家长")
    parent_id = context.messages[-1]["message_id"]
    apply_model_context_updates(context, {"identity": {
        "role": "parent", "source_message_id": parent_id,
        "evidence": "我是家长", "confidence": 0.9,
    }}, source="test_model")
    context.add_message("user", "幫我看看這個")
    unknown_id = context.messages[-1]["message_id"]

    apply_model_context_updates(context, {"identity": {
        "role": "unknown", "source_message_id": unknown_id,
        "evidence": "看看這個", "confidence": 0.4,
    }}, source="test_model")

    assert context.session_meta["conversation_identity"]["role"] == "parent"


def test_facts_extractor_does_not_use_regex_when_model_result_is_unavailable():
    context = SessionContext()
    context.add_message("user", "孩子现在4年级")

    result = FactsExtractorSkill(llm_client=None).run("孩子现在4年级", context)

    assert result.state_patch["fact_updates"] == {}
    assert result.state_patch["context_updates"] is None
