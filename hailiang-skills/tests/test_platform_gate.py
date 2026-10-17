from __future__ import annotations

from hailiang_skills.runtime_bridge.platform_gate import (
    PLATFORM_IDENTITY_FORM_ID,
    apply_platform_identity_answers,
    build_platform_identity_form,
    missing_identity_fields,
)


def test_missing_identity_fields_detects_both():
    assert missing_identity_fields({}) == ["user_role", "school_stage"]
    assert missing_identity_fields({"user_role": "家长"}) == ["school_stage"]
    assert missing_identity_fields({"user_role": "家长", "school_stage": "高中"}) == []


def test_missing_identity_fields_reads_known_facts_style_objects():
    class _Record:
        def __init__(self, value):
            self.value = value

    class _Facts:
        def __init__(self):
            self.facts = {"user_role": _Record("学生")}

        def get_value(self, key):
            record = self.facts.get(key)
            return None if record is None else record.value

    assert missing_identity_fields(_Facts()) == ["school_stage"]


def test_build_platform_identity_form_shape():
    block = build_platform_identity_form()
    assert block["type"] == "fact_form"
    payload = block["payload"]
    assert payload["form_id"] == PLATFORM_IDENTITY_FORM_ID
    keys = [field["fact_key"] for field in payload["fields"]]
    assert keys == ["user_role", "school_stage"]
    role_field = payload["fields"][0]
    assert role_field["scope"] == "user"
    assert {opt["value"] for opt in role_field["options"]} == {"家长", "学生"}
    stage_field = payload["fields"][1]
    assert stage_field["scope"] == "profile"
    assert {opt["value"] for opt in stage_field["options"]} == {"小学", "初中", "高中"}


def test_build_platform_identity_form_can_limit_to_missing_keys():
    block = build_platform_identity_form(missing=["school_stage"])
    keys = [field["fact_key"] for field in block["payload"]["fields"]]
    assert keys == ["school_stage"]


def test_apply_platform_identity_answers_normalizes_aliases():
    assert apply_platform_identity_answers({"user_role": "parent", "school_stage": "senior"}) == {
        "user_role": "家长",
        "school_stage": "高中",
    }
    assert apply_platform_identity_answers({"user_role": "学生", "school_stage": "初中"}) == {
        "user_role": "学生",
        "school_stage": "初中",
    }
    assert apply_platform_identity_answers({"user_role": "外星人"}) == {}
