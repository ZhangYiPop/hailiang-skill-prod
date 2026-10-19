"""Platform preflight gate for identity / school stage before orchestration."""

from __future__ import annotations

from typing import Any


IDENTITY_FACT_KEYS = ("user_role", "school_stage")
PLATFORM_IDENTITY_FORM_ID = "platform_identity_form"

USER_ROLE_VALUES = ("家长", "学生")
SCHOOL_STAGE_VALUES = ("小学", "初中", "高中")

_USER_ROLE_ALIASES = {
    "家长": "家长",
    "父母": "家长",
    "监护人": "家长",
    "parent": "家长",
    "parents": "家长",
    "guardian": "家长",
    "学生": "学生",
    "孩子": "学生",
    "学员": "学生",
    "student": "学生",
}

_SCHOOL_STAGE_ALIASES = {
    "小学": "小学",
    "小学阶段": "小学",
    "primary": "小学",
    "elementary": "小学",
    "初中": "初中",
    "初中阶段": "初中",
    "junior": "初中",
    "middle": "初中",
    "高中": "高中",
    "高中阶段": "高中",
    "senior": "高中",
    "high": "高中",
    "highschool": "高中",
}


def _fact_value(facts: Any, key: str) -> Any:
    if facts is None:
        return None
    if isinstance(facts, dict):
        if key in facts:
            return facts.get(key)
        nested = facts.get("facts")
        if isinstance(nested, dict) and key in nested:
            value = nested.get(key)
            if hasattr(value, "value"):
                return getattr(value, "value")
            return value
        return None
    getter = getattr(facts, "get_value", None)
    if callable(getter):
        return getter(key)
    mapping = getattr(facts, "facts", None)
    if isinstance(mapping, dict) and key in mapping:
        record = mapping[key]
        return getattr(record, "value", record)
    return getattr(facts, key, None)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def missing_identity_fields(facts: Any) -> list[str]:
    """Return missing platform identity fields in stable order."""
    missing: list[str] = []
    for key in IDENTITY_FACT_KEYS:
        if _is_missing(_fact_value(facts, key)):
            missing.append(key)
    return missing


def build_platform_identity_form(*, missing: list[str] | None = None) -> dict[str, Any]:
    """Build the fixed platform fact_form block for identity / school stage."""
    keys = [key for key in (missing or list(IDENTITY_FACT_KEYS)) if key in IDENTITY_FACT_KEYS]
    if not keys:
        keys = list(IDENTITY_FACT_KEYS)
    fields: list[dict[str, Any]] = []
    if "user_role" in keys:
        fields.append(
            {
                "fact_key": "user_role",
                "label": "您的身份",
                "input_type": "single_select",
                "required": True,
                "placeholder": "请选择您的身份",
                "example": "例如：家长",
                "options": [
                    {"label": "家长", "value": "家长"},
                    {"label": "学生", "value": "学生"},
                ],
                "submit_mode": "auto",
                "scope": "user",
                "value_type": "string",
            }
        )
    if "school_stage" in keys:
        fields.append(
            {
                "fact_key": "school_stage",
                "label": "学段",
                "input_type": "single_select",
                "required": True,
                "placeholder": "请选择学段",
                "example": "例如：高中",
                "options": [
                    {"label": "小学", "value": "小学"},
                    {"label": "初中", "value": "初中"},
                    {"label": "高中", "value": "高中"},
                ],
                "submit_mode": "auto",
                "scope": "profile",
                "value_type": "string",
            }
        )
    return {
        "type": "fact_form",
        "payload": {
            "form_id": PLATFORM_IDENTITY_FORM_ID,
            "title": "补充身份信息",
            "description": "请先确认身份和学段，我们再为你匹配专家。",
            "fields": fields,
        },
    }


def _normalize_user_role(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text in USER_ROLE_VALUES:
        return text
    return _USER_ROLE_ALIASES.get(text.lower()) or _USER_ROLE_ALIASES.get(text)


def _normalize_school_stage(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text in SCHOOL_STAGE_VALUES:
        return text
    compact = text.lower().replace(" ", "").replace("_", "")
    return _SCHOOL_STAGE_ALIASES.get(text) or _SCHOOL_STAGE_ALIASES.get(compact)


def apply_platform_identity_answers(answers: Any) -> dict[str, str]:
    """Normalize platform identity form answers to canonical fact values."""
    if not isinstance(answers, dict):
        return {}
    normalized: dict[str, str] = {}
    if "user_role" in answers:
        role = _normalize_user_role(answers.get("user_role"))
        if role:
            normalized["user_role"] = role
    if "school_stage" in answers:
        stage = _normalize_school_stage(answers.get("school_stage"))
        if stage:
            normalized["school_stage"] = stage
    return normalized
