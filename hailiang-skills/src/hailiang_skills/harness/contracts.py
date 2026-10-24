from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TERMINAL = {"completed", "failed", "cancelled"}
TOOL_TERMINAL = TERMINAL | {"skipped"}
SKILL_ASSET_SEARCH_TOOL = "search_skill_assets"
LEGACY_RAG_SEARCH_TOOL = "rag_search"
SEARCH_TOOL_NAMES = {SKILL_ASSET_SEARCH_TOOL, LEGACY_RAG_SEARCH_TOOL, "web_search"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class HarnessError(ValueError):
    def __init__(self, code: str, status: int = 422):
        self.code, self.status = code, status
        super().__init__(code)


def _normalize_option(option: Any) -> Any:
    """Accept model-emitted ``key`` as an alias of the required ``value`` field.
    Also handle {option_key: option_text} format like {"A": "跑出去找朋友玩"}."""
    if not isinstance(option, dict):
        return option
    normalized = dict(option)
    # 处理 {key: text} 格式（如 {"A": "跑出去找朋友玩"}）
    if "label" not in normalized and "value" not in normalized:
        for k, v in normalized.items():
            if k not in ("label", "value") and isinstance(v, str):
                return {"label": str(k), "value": str(v)}
    if "value" not in normalized and "key" in normalized:
        normalized["value"] = normalized.pop("key")
    elif "key" in normalized and normalized.get("key") == normalized.get("value"):
        normalized.pop("key", None)
    return normalized


class InputField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=240)
    input_type: Literal["text", "single_select", "multi_select", "integer", "number"] = "text"
    required: bool = True
    options: list[dict[str, str]] = Field(default_factory=list, max_length=40)
    scope: Literal["task", "session", "profile", "user"] = "task"
    fact_key: str | None = None
    subject: str = Field(default="", max_length=160)
    applicable_time: str = Field(default="", max_length=160)
    placeholder: str = Field(default="", max_length=240)
    min: float | None = None
    max: float | None = None

    @field_validator("options", mode="before")
    @classmethod
    def normalize_options(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [_normalize_option(item) for item in value]

    @model_validator(mode="after")
    def valid_options(self):
        if self.input_type in {"single_select", "multi_select"}:
            if not self.options or any(set(o) != {"label", "value"} for o in self.options):
                raise ValueError("options require label and value")
            if len({o["value"] for o in self.options}) != len(self.options):
                raise ValueError("duplicate option values")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("invalid numeric bounds")
        return self


def _normalize_option_dict(o: dict) -> dict[str, str]:
    """将任意 option dict 规范化为 {label, value} 格式。

    处理三种模型常见输出：
    - {label, value} → 直接返回
    - {key: text} 如 {"A": "跑出去找朋友玩"} → {label: "A", value: "跑出去找朋友玩"}
    - 只有 value 缺 label → label 补为 value
    - 只有 label 缺 value → value 补为 label
    - 其他格式 → 尽量提取可用的 label/value
    """
    # 已经是标准格式
    if "label" in o and "value" in o:
        return {"label": str(o["label"]), "value": str(o["value"])}
    # {key: text} 格式：取第一个非 label/value 的键值对
    for k, v in o.items():
        if k not in ("label", "value") and isinstance(v, str):
            return {"label": str(k), "value": str(v)}
    # 兜底：互为缺省，避免 str(o) 产生 Python repr 串（如 "{'value': '初三'}"）
    val = str(o.get("value") or "")
    lbl = str(o.get("label") or val)
    if not val:
        val = lbl
    if not lbl:
        lbl = val
    if not lbl:
        lbl = val = str(o)
    return {"label": lbl, "value": val}


class InputArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=240)
    fields: list[InputField] = Field(min_length=1, max_length=12)
    clarification_reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def unique_keys(self):
        if len({field.key for field in self.fields}) != len(self.fields):
            raise ValueError("duplicate question keys")
        return self


def normalize_input_arguments(arguments: dict) -> dict:
    """Accept the common label-to-value select shorthand without widening the schema."""
    if not isinstance(arguments, dict) or not isinstance(arguments.get("fields"), list):
        return arguments
    normalized = dict(arguments)
    normalized["fields"] = []
    for field in arguments["fields"]:
        if not isinstance(field, dict):
            normalized["fields"].append(field)
            continue
        item = dict(field)
        options = item.get("options")
        if item.get("input_type") in {"single_select", "multi_select"} and isinstance(options, dict) and all(
            isinstance(label, str) and isinstance(value, str) for label, value in options.items()
        ):
            item["options"] = [{"label": label, "value": value} for label, value in options.items()]
        elif item.get("input_type") in {"single_select", "multi_select"} and isinstance(options, list):
            # 模型常生成只有 value 缺 label、或只有 label 但 value 为空串的 option。
            # 也处理 {key: text} 格式（如 {"A": "跑出去找朋友玩"}）。
            item["options"] = [
                (
                    lambda o: (
                        _normalize_option_dict(o)
                    ) if isinstance(o, dict) else {"label": str(o), "value": str(o)}
                )(o)
                for o in options
            ]
        normalized["fields"].append(item)
    return normalized


class AnswerSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1)
    profile_id: str | None = None
    submission_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
    answers: dict[str, Any] = Field(default_factory=dict)
    text: str | None = Field(default=None, max_length=120000)
    skipped: bool = False


class InternalAnswerSubmission(BaseModel):
    """Trusted V3 adapter input; the public V2 answer contract remains strict."""
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1)
    profile_id: str | None = None
    submission_id: str | None = Field(default=None, max_length=100)
    expected_version: int | None = Field(default=None, ge=1)
    answers: dict[str, Any] = Field(default_factory=dict)
    text: str | None = Field(default=None, max_length=120000)
    skipped: bool = False


def validate_answers(fields: list[dict], submission: AnswerSubmission | InternalAnswerSubmission, *, allow_partial: bool = False) -> dict:
    if submission.skipped:
        if submission.answers or submission.text:
            raise HarnessError("SKIP_WITH_ANSWERS")
        return {"skipped": True, "answers": {}, "skipped_fields": [f["key"] for f in fields]}
    values = dict(submission.answers)
    if submission.text is not None:
        if len(fields) != 1 or fields[0]["input_type"] != "text" or values:
            raise HarnessError("TEXT_REQUIRES_SINGLE_FIELD")
        values[fields[0]["key"]] = submission.text
    if set(values) - {f["key"] for f in fields}:
        raise HarnessError("UNKNOWN_ANSWER_FIELD")
    for f in fields:
        value = values.get(f["key"])
        if value is None or value == "" or value == [] or isinstance(value, str) and not value.strip():
            # 空着不填和「全部跳过」同一口径：这题本轮未答，不因 required 拒绝提交。
            values.pop(f["key"], None)
            continue
        kind = f["input_type"]
        choices = {item["value"] for item in f["options"]}
        if kind == "text" and (not isinstance(value, str) or len(value) > 120000):
            raise HarnessError("INVALID_TEXT_ANSWER")
        if kind == "single_select" and (not isinstance(value, str) or value not in choices):
            raise HarnessError("INVALID_OPTION")
        if kind == "multi_select" and (not isinstance(value, list) or not all(isinstance(v, str) and v in choices for v in value) or len(value) != len(set(value))):
            raise HarnessError("INVALID_OPTIONS")
        if kind in {"integer", "number"}:
            import math
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise HarnessError("INVALID_NUMBER")
            if kind == "integer" and int(value) != value:
                raise HarnessError("INVALID_INTEGER")
            if f["min"] is not None and value < f["min"] or f["max"] is not None and value > f["max"]:
                raise HarnessError("NUMBER_OUT_OF_RANGE")
    return {"skipped": not bool(values), "answers": values,
            "skipped_fields": [f["key"] for f in fields if f["key"] not in values]}
