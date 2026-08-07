from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from hailiang_skills.core.facts_config import get_fact_meta
from hailiang_skills.schemas.provenance import Provenance
from hailiang_skills.skills.common import (
    normalize_budget_level,
    normalize_career_orientation_values,
    normalize_exam_qualification_status,
)

EXPLICIT_UNKNOWN_TEXTS = {
    "未知",
    "目前未知",
    "暂时未知",
    "暂未知",
    "不清楚",
    "暂不清楚",
    "不知道",
    "未确定",
    "待确认",
    "待补充",
    "暂无",
}
EXPLICIT_UNKNOWN_FACT_KEYS = {
    "student_province",
    "student_region",
    "subject_group",
    "score_total",
    "score_recent_avg",
    "score_source",
    "score_band_tag",
    "budget_level",
    "family_type",
    "ethnicity",
    "hukou_years",
    "school_status_years",
    "exam_qualification_status",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_province(value: str) -> str:
    return (
        value.replace("壮族自治区", "")
        .replace("回族自治区", "")
        .replace("维吾尔自治区", "")
        .replace("自治区", "")
        .replace("省", "")
        .replace("市", "")
        .strip()
    )


def _normalize_subject_group(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    physics_keywords = [
        "物理",
        "理科",
        "物理类",
        "物化生",
        "物化地",
        "物化政",
        "物生地",
        "物生政",
    ]
    history_keywords = [
        "历史",
        "文科",
        "历史类",
        "史政地",
        "史政生",
        "史地政",
        "史地化",
        "史化生",
    ]
    if any(keyword in text for keyword in physics_keywords):
        return "物理"
    if any(keyword in text for keyword in history_keywords):
        return "历史"
    return text or None


def is_explicit_unknown_value(key: str, value: Any) -> bool:
    if key not in EXPLICIT_UNKNOWN_FACT_KEYS or not isinstance(value, str):
        return False
    text = value.strip().lower().replace(" ", "")
    normalized_candidates = {item.lower().replace(" ", "") for item in EXPLICIT_UNKNOWN_TEXTS}
    return text in normalized_candidates


def normalize_fact_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if is_explicit_unknown_value(key, value):
        return None

    meta = get_fact_meta(key)
    value_type = meta.get("value_type")

    if key == "student_province" and isinstance(value, str):
        return _normalize_province(value)
    if key == "subject_group":
        return _normalize_subject_group(value)
    if key == "budget_level" and isinstance(value, str):
        return normalize_budget_level(value)
    if key == "exam_qualification_status" and isinstance(value, str):
        return normalize_exam_qualification_status(value)
    if key == "career_orientation":
        normalized_values = normalize_career_orientation_values(value)
        return normalized_values if value_type == "string_list" else (normalized_values[0] if normalized_values else None)

    if value_type == "number":
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return int(text) if text.isdigit() else float(text)
            except ValueError:
                return value
        return value

    if value_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "yes", "1", "是", "已是"}:
                return True
            if lowered in {"false", "no", "0", "否", "不是"}:
                return False
        return value

    if value_type == "string_list":
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            parts = [item.strip() for item in text.replace("，", ",").split(",")]
            if len(parts) == 1 and "\n" in text:
                parts = [item.strip() for item in text.splitlines()]
            return [item for item in parts if item]
        return value

    if value_type == "string" and isinstance(value, str):
        return value.strip()

    return value


class FactRecord(BaseModel):
    value: Any
    confidence: float = 1.0
    source_skill: str
    source_type: str = "skill"
    source_id: str | None = None
    source_label: str | None = None
    scope: str = "user"
    source_turn_id: str | None = None
    provenance: Provenance | None = None
    updated_at: str = Field(default_factory=utc_now_iso)


class KnownFacts(BaseModel):
    facts: dict[str, FactRecord] = Field(default_factory=dict)

    def set_fact(
        self,
        key: str,
        value: Any,
        source_skill: str,
        confidence: float = 1.0,
        source_type: str = "skill",
        source_id: str | None = None,
        source_label: str | None = None,
        scope: str = "user",
        source_turn_id: str | None = None,
        provenance: Provenance | None = None,
    ) -> FactRecord:
        normalized_value = normalize_fact_value(key, value)
        record = FactRecord(
            value=normalized_value,
            confidence=confidence,
            source_skill=source_skill,
            source_type=source_type,
            source_id=source_id,
            source_label=source_label,
            scope=scope,
            source_turn_id=source_turn_id,
            provenance=provenance,
        )
        self.facts[key] = record
        return record

    def get_value(self, key: str, default: Any = None) -> Any:
        record = self.facts.get(key)
        return default if record is None else record.value

    def reset_fact(self, key: str) -> FactRecord | None:
        return self.facts.pop(key, None)
