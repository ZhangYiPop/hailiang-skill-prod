from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hailiang_skills.core.facts_config import (
    get_enabled_fact_keys,
    get_fact_label,
    get_fact_meta,
    get_fact_scope_policy,
)
from hailiang_skills.schemas.message_blocks import MessageBlock


_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "fact_form_config.yml"
_fact_form_config: dict[str, Any] | None = None
_FORM_FIELD_PRIORITY = {
    "student_region": 0,
    "special_identity_tags": 1,
    "physical_requirements": 2,
}


def load_fact_form_config(force_reload: bool = False) -> dict[str, Any]:
    global _fact_form_config
    if _fact_form_config is None or force_reload:
        if _CONFIG_PATH.exists():
            with _CONFIG_PATH.open(encoding="utf-8") as fh:
                _fact_form_config = yaml.safe_load(fh) or {}
        else:
            _fact_form_config = {}
    return _fact_form_config


def sort_fact_keys_for_form(fact_keys: list[str]) -> list[str]:
    unique_keys: list[str] = []
    seen: set[str] = set()
    for fact_key in fact_keys:
        if not fact_key or fact_key in seen:
            continue
        seen.add(fact_key)
        unique_keys.append(fact_key)
    return sorted(
        unique_keys,
        key=lambda fact_key: (_FORM_FIELD_PRIORITY.get(fact_key, 100), fact_key),
    )


def build_fact_form_fields(fact_keys: list[str] | None = None) -> list[dict[str, Any]]:
    config = load_fact_form_config().get("facts", {})
    enabled_fact_keys = set(get_enabled_fact_keys())
    keys = sort_fact_keys_for_form(fact_keys or list(get_enabled_fact_keys()))
    fields: list[dict[str, Any]] = []
    for fact_key in keys:
        if fact_key not in enabled_fact_keys:
            continue
        field_config = config.get(fact_key)
        meta = get_fact_meta(fact_key)
        value_type = meta.get("value_type")
        input_type = (field_config or {}).get("input_type")
        if not input_type:
            if value_type == "string_list":
                input_type = "text"
            elif value_type == "boolean":
                input_type = "single_select"
            else:
                input_type = "text"
        options = (field_config or {}).get("options", [])
        if not options and value_type == "boolean":
            options = [
                {"label": "是", "value": "true"},
                {"label": "否", "value": "false"},
            ]
        fields.append(
            {
                "fact_key": fact_key,
                "label": (field_config or {}).get("label") or get_fact_label(fact_key),
                "input_type": input_type,
                "required": bool((field_config or {}).get("required", True)),
                "placeholder": (field_config or {}).get("placeholder", ""),
                "example": (field_config or {}).get("example", ""),
                "options": options,
                "submit_mode": (field_config or {}).get("submit_mode", "manual"),
                "scope": (field_config or {}).get("scope") or get_fact_scope_policy(fact_key),
                "value_type": value_type,
            }
        )
    return fields


def build_missing_fact_form_block(
    missing_facts: list[str],
    *,
    form_id: str = "missing_facts_form",
) -> dict[str, Any] | None:
    if not missing_facts:
        return None
    fields = build_fact_form_fields(missing_facts)
    if not fields:
        return None
    return MessageBlock(
        type="fact_form",
        payload={
            "form_id": form_id,
            "title": "补充关键信息",
            "fields": fields,
        },
    ).model_dump()
