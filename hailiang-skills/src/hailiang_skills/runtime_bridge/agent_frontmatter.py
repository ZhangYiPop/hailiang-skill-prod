"""Structured, optional routing metadata stored at the top of ``AGENT.md``."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any

import yaml


class AgentFrontMatterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AgentRoutingRule:
    skill_id: str
    when: tuple[str, ...]
    priority: int | None
    index: int


@dataclass(frozen=True, slots=True)
class AgentFrontMatter:
    exists: bool
    metadata: dict[str, Any]
    body: str
    routing_rules: tuple[AgentRoutingRule, ...]


def parse_agent_frontmatter(markdown: str) -> AgentFrontMatter:
    """Parse optional top-of-file YAML without changing the Markdown body."""
    text = str(markdown or "")
    if not text.startswith("---\n"):
        return AgentFrontMatter(False, {}, text, ())
    closing = text.find("\n---", 4)
    if closing < 0:
        raise AgentFrontMatterError("AGENT.md YAML front matter 缺少结束分隔符 ---")
    yaml_text = text[4:closing]
    body_start = closing + 4
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    try:
        metadata = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise AgentFrontMatterError(f"AGENT.md YAML front matter 格式错误: {exc}") from exc
    if not isinstance(metadata, dict):
        raise AgentFrontMatterError("AGENT.md YAML front matter 必须是对象")
    routing = metadata.get("skill_routing")
    if routing is None:
        return AgentFrontMatter(True, metadata, text[body_start:], ())
    if not isinstance(routing, dict):
        raise AgentFrontMatterError("skill_routing 必须是对象")
    raw_rules = routing.get("rules")
    if not isinstance(raw_rules, list):
        raise AgentFrontMatterError("skill_routing.rules 必须是数组")
    rules: list[AgentRoutingRule] = []
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            raise AgentFrontMatterError(f"skill_routing.rules[{index}] 必须是对象")
        skill_id = str(item.get("skill_id") or "").strip()
        if not skill_id:
            raise AgentFrontMatterError(f"skill_routing.rules[{index}].skill_id 不能为空")
        raw_when = item.get("when", [])
        if not isinstance(raw_when, list) or not all(isinstance(value, str) for value in raw_when):
            raise AgentFrontMatterError(f"skill_routing.rules[{index}].when 必须是字符串数组")
        priority = item.get("priority")
        if priority is not None and (isinstance(priority, bool) or not isinstance(priority, int)):
            raise AgentFrontMatterError(f"skill_routing.rules[{index}].priority 必须是整数")
        rules.append(AgentRoutingRule(skill_id, tuple(value.strip() for value in raw_when if value.strip()), priority, index))
    return AgentFrontMatter(True, metadata, text[body_start:], tuple(rules))


def validate_agent_skill_routing(markdown: str, bound_skill_ids: Iterable[str]) -> list[str]:
    """Validate the strict ID contract only when a front matter exists."""
    try:
        parsed = parse_agent_frontmatter(markdown)
    except AgentFrontMatterError as exc:
        return [str(exc)]
    if not parsed.exists:
        return []
    bound_ids = {str(skill_id).strip() for skill_id in bound_skill_ids if str(skill_id).strip()}
    ids = [rule.skill_id for rule in parsed.routing_rules]
    duplicates = sorted({skill_id for skill_id in ids if ids.count(skill_id) > 1})
    errors = [f"AGENT.md skill_routing.rules 存在重复 Skill ID: {', '.join(duplicates)}"] if duplicates else []
    declared = set(ids)
    missing = sorted(bound_ids - declared)
    extra = sorted(declared - bound_ids)
    if missing:
        errors.append(f"AGENT.md skill_routing.rules 缺少当前绑定 Skill: {', '.join(missing)}")
    if extra:
        errors.append(f"AGENT.md skill_routing.rules 包含未绑定 Skill: {', '.join(extra)}")
    return errors
