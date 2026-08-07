from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = Path(__file__).parent.parent.parent.parent / "config"
_ROUTING_CONFIG_PATH = _CONFIG_DIR / "skills_routing_config.yml"
_REGISTRY_CONFIG_PATH = _CONFIG_DIR / "skills_registry.yml"

_routing_config: dict[str, Any] | None = None
_registry_config: dict[str, Any] | None = None


def load_routing_config(force_reload: bool = False) -> dict[str, Any]:
    global _routing_config
    if _routing_config is None or force_reload:
        if _ROUTING_CONFIG_PATH.exists():
            with open(_ROUTING_CONFIG_PATH, encoding="utf-8") as f:
                _routing_config = yaml.safe_load(f) or {}
        else:
            _routing_config = {}
    return _routing_config


def load_registry_config(force_reload: bool = False) -> dict[str, Any]:
    global _registry_config
    if _registry_config is None or force_reload:
        if _REGISTRY_CONFIG_PATH.exists():
            with open(_REGISTRY_CONFIG_PATH, encoding="utf-8") as f:
                _registry_config = yaml.safe_load(f) or {}
        else:
            _registry_config = {}
    return _registry_config


def get_routing_keywords() -> dict[str, dict[str, Any]]:
    config = load_routing_config()
    return config.get("routing_keywords", {})


def get_skill_registry() -> dict[str, dict[str, Any]]:
    config = load_registry_config()
    return config.get("skills", {})


def get_skill_info(skill_name: str) -> dict[str, Any] | None:
    registry = get_skill_registry()
    return registry.get(skill_name)


def get_future_scenarios() -> dict[str, dict[str, Any]]:
    config = load_routing_config()
    return config.get("future_scenarios", {})


def get_keyword_match(
    text: str,
    category: str | None = None,
) -> tuple[str | None, float, str]:
    keywords_config = get_routing_keywords()

    if category and category in keywords_config:
        kw_section = keywords_config[category]
        keywords = kw_section.get("keywords", [])
        for kw in keywords:
            if kw in text:
                return (
                    kw,
                    kw_section.get("confidence", 0.5),
                    kw_section.get("target_skill", "chat"),
                )
        return None, 0.0, "chat"

    for kw_section in keywords_config.values():
        keywords = kw_section.get("keywords", [])
        for kw in keywords:
            if kw in text:
                return (
                    kw,
                    kw_section.get("confidence", 0.5),
                    kw_section.get("target_skill", "chat"),
                )
    return None, 0.0, "chat"


def get_scenario_keyword_match(text: str) -> dict[str, Any] | None:
    scenarios = get_future_scenarios()
    matches: list[dict[str, Any]] = []
    for scenario_id, config in scenarios.items():
        for keyword in config.get("keywords", []):
            if keyword in text:
                matches.append(
                    {
                        "scenario_id": scenario_id,
                        "keyword": keyword,
                        "status": config.get("status", "planning"),
                        "target_skill": config.get("target_skill"),
                    }
                )
    if not matches:
        return None
    matches.sort(key=lambda item: len(item["keyword"]), reverse=True)
    return matches[0]


def get_skill_can_exit_to(skill_name: str) -> list[str]:
    info = get_skill_info(skill_name)
    if info:
        return info.get("can_exit_to", [])
    return []


def is_skill_skip_stability_check(skill_name: str) -> bool:
    info = get_skill_info(skill_name)
    if info:
        return info.get("skip_stability_check", False)
    return False


def get_skill_priority(skill_name: str) -> int:
    info = get_skill_info(skill_name)
    if info:
        return info.get("priority", 0)
    return 0


def get_skill_prompt_key(skill_name: str) -> str | None:
    info = get_skill_info(skill_name)
    if info:
        return info.get("prompt_key")
    return None


def is_terminal_skill(skill_name: str) -> bool:
    info = get_skill_info(skill_name)
    if info:
        return info.get("is_terminal", False)
    return False
