from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "facts_schema.yml"

_facts_config: dict[str, Any] | None = None


def load_facts_config(force_reload: bool = False) -> dict[str, Any]:
    global _facts_config
    if _facts_config is None or force_reload:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                _facts_config = yaml.safe_load(f) or {}
        else:
            _facts_config = {}
    return _facts_config


def get_fact_schema() -> dict[str, dict[str, Any]]:
    return load_facts_config().get("facts", {})


def get_enabled_fact_keys() -> list[str]:
    schema = get_fact_schema()
    return [key for key, meta in schema.items() if meta.get("enabled", True)]


def get_fact_labels() -> dict[str, str]:
    schema = get_fact_schema()
    return {key: meta.get("label", key) for key, meta in schema.items()}


def get_fact_label(fact_key: str) -> str:
    return get_fact_labels().get(fact_key, fact_key)


def get_fact_meta(fact_key: str) -> dict[str, Any]:
    return get_fact_schema().get(fact_key, {})


def get_fact_scope_policy(fact_key: str, default: str = "profile") -> str:
    meta = get_fact_meta(fact_key)
    scope_policy = meta.get("scope_policy")
    legacy_scope_aliases = {
        "user": "shared",
    }
    normalized_scope = legacy_scope_aliases.get(scope_policy, scope_policy)
    return normalized_scope if normalized_scope in {"shared", "profile", "session"} else default


def get_facts_by_scenario(scenario: str) -> dict[str, dict[str, Any]]:
    schema = get_fact_schema()
    return {
        key: meta
        for key, meta in schema.items()
        if meta.get("enabled", True)
        and (
            meta.get("scenario") == scenario
            or scenario in (meta.get("scenario_group") or [])
        )
    }


def get_planning_fact_placeholders() -> dict[str, Any]:
    return load_facts_config().get("future_scenarios", {})
