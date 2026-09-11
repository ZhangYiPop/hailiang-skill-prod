from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QuestionDefinition(BaseModel):
    question_id: str
    question_text: str
    question_type: str = "single"
    answer_options: list[str] = Field(default_factory=list)
    slot_targets: list[str] = Field(default_factory=list)
    ask_when: str | None = None
    stop_when: str | None = None
    priority: int = 100
    group_id: str | None = None
    depends_on_paths: list[str] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


QUESTION_INPUT_TYPES = {"single_select", "multi_select", "text", "integer", "number"}


def validate_questionnaire_config(value: Any) -> list[str]:
    """Validate the business-editable native questionnaire contract."""
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["questionnaire.config_json 必须是对象"]
    if value.get("schema_version", 1) != 1:
        errors.append("questionnaire.config_json.schema_version 必须为 1")
    questions = value.get("questions")
    if not isinstance(questions, list):
        return [*errors, "questionnaire.config_json.questions 必须是数组"]
    seen: set[str] = set()
    for index, question in enumerate(questions, start=1):
        prefix = f"questions[{index}]"
        if not isinstance(question, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        question_id = str(question.get("id") or question.get("question_id") or "").strip()
        if not question_id:
            errors.append(f"{prefix}.id 不能为空")
        elif question_id in seen:
            errors.append(f"{prefix}.id 重复：{question_id}")
        seen.add(question_id)
        if not str(question.get("label") or "").strip():
            errors.append(f"{prefix}.label 不能为空")
        input_type = str(question.get("input_type") or "text")
        if input_type not in QUESTION_INPUT_TYPES:
            errors.append(f"{prefix}.input_type 不支持：{input_type}")
        options = question.get("options", [])
        if input_type in {"single_select", "multi_select"}:
            if not isinstance(options, list) or not options:
                errors.append(f"{prefix}.options 必须是非空数组")
            else:
                option_values: set[str] = set()
                for option_index, option in enumerate(options, start=1):
                    raw = option.get("value") if isinstance(option, dict) else option
                    option_value = str(raw or "").strip()
                    if not option_value:
                        errors.append(f"{prefix}.options[{option_index}] 缺少 value")
                    elif option_value in option_values:
                        errors.append(f"{prefix}.options value 重复：{option_value}")
                    option_values.add(option_value)
        if input_type == "multi_select":
            maximum = question.get("max_selections")
            if maximum is not None and (not isinstance(maximum, int) or maximum < 1):
                errors.append(f"{prefix}.max_selections 必须是正整数")
        if input_type in {"integer", "number"}:
            minimum, maximum = question.get("min"), question.get("max")
            if minimum is not None and not isinstance(minimum, (int, float)):
                errors.append(f"{prefix}.min 必须是数字")
            if maximum is not None and not isinstance(maximum, (int, float)):
                errors.append(f"{prefix}.max 必须是数字")
            if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)) and minimum > maximum:
                errors.append(f"{prefix}.min 不能大于 max")
            decimal_places = question.get("decimal_places")
            if decimal_places is not None and (not isinstance(decimal_places, int) or decimal_places < 0):
                errors.append(f"{prefix}.decimal_places 必须是非负整数")
    return errors
