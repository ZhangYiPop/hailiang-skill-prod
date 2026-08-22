"""Reference-only Expert Team bundle loader.

Teams compose published ``single_expert`` bundles.  They deliberately cannot
carry Skills or embed another team, so the normal Expert Bundle remains the
only place that owns an expert's Skill lock and role rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hailiang_skills.runtime_bridge.expert_bundle import ExpertRegistry


class ExpertTeamBundleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExpertTeamMember:
    expert_id: str
    mention_name: str
    routing_brief: str = ""


@dataclass(frozen=True, slots=True)
class ExpertTeamDefinition:
    team_id: str
    name: str
    rules_markdown: str
    coordinator_expert_id: str
    members: tuple[ExpertTeamMember, ...]
    source_dir: Path | None = None

    @property
    def member_expert_ids(self) -> tuple[str, ...]:
        return tuple(member.expert_id for member in self.members)

    def member_for_expert(self, expert_id: str) -> ExpertTeamMember | None:
        target = str(expert_id or "").strip()
        return next((member for member in self.members if member.expert_id == target), None)

    def member_for_mention(self, mention_name: str) -> ExpertTeamMember | None:
        target = str(mention_name or "").strip()
        return next((member for member in self.members if member.mention_name == target), None)


@dataclass(slots=True)
class ExpertTeamRegistry:
    definitions: dict[str, ExpertTeamDefinition]

    def get(self, team_id: str) -> ExpertTeamDefinition | None:
        return self.definitions.get(str(team_id or "").strip())

    def require(self, team_id: str) -> ExpertTeamDefinition:
        definition = self.get(team_id)
        if definition is None:
            raise ExpertTeamBundleError(f"专家团不存在: {team_id}")
        return definition


def load_local_expert_team_registry(
    teams_root: str | Path,
    expert_registry: ExpertRegistry,
) -> ExpertTeamRegistry:
    root = Path(teams_root).expanduser().resolve()
    if not root.is_dir():
        return ExpertTeamRegistry(definitions={})
    definitions: dict[str, ExpertTeamDefinition] = {}
    for child in sorted(item for item in root.iterdir() if item.is_dir()):
        if not (child / "team.yaml").is_file():
            continue
        definition = load_expert_team_bundle(child, expert_registry)
        if definition.team_id in definitions:
            raise ExpertTeamBundleError(f"重复的专家团 ID: {definition.team_id}")
        definitions[definition.team_id] = definition
    return ExpertTeamRegistry(definitions=definitions)


def load_expert_team_bundle(
    bundle_dir: str | Path,
    expert_registry: ExpertRegistry,
) -> ExpertTeamDefinition:
    root = Path(bundle_dir).expanduser().resolve()
    if (root / "skills").exists():
        raise ExpertTeamBundleError(f"专家团包禁止携带 skills/ 目录: {root}")
    if (root / "teams").exists() or (root / "runtime_agent_teams").exists():
        raise ExpertTeamBundleError("专家团不能嵌套专家团")
    config_path = root / "team.yaml"
    rules_path = root / "TEAM.md"
    missing = [path.name for path in (config_path, rules_path) if not path.is_file()]
    if missing:
        raise ExpertTeamBundleError(f"专家团包缺少必需文件: {', '.join(missing)}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ExpertTeamBundleError(f"team.yaml 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise ExpertTeamBundleError("team.yaml 必须是对象")
    if int(raw.get("schema_version", 1) or 1) != 1:
        raise ExpertTeamBundleError("不支持的 Expert Team Bundle schema_version")
    if str(raw.get("topology") or "").strip() != "team":
        raise ExpertTeamBundleError("专家团 team.yaml 必须声明 topology: team")
    team_id = str(raw.get("id") or raw.get("team_id") or "").strip()
    name = str(raw.get("name") or "").strip()
    coordinator_expert_id = str(raw.get("coordinator_expert_id") or "").strip()
    if not team_id or not name or not coordinator_expert_id:
        raise ExpertTeamBundleError("team.yaml 必须包含 id、name、coordinator_expert_id")
    if str(raw.get("rule_file") or "TEAM.md").strip() != "TEAM.md":
        raise ExpertTeamBundleError("专家团规则文件必须是包根目录的 TEAM.md")
    members_raw = raw.get("members")
    if not isinstance(members_raw, list) or not members_raw:
        raise ExpertTeamBundleError("team.yaml 必须声明至少一个 members")
    members: list[ExpertTeamMember] = []
    member_ids: set[str] = set()
    mentions: set[str] = set()
    for item in members_raw:
        if not isinstance(item, dict):
            raise ExpertTeamBundleError("team.yaml.members 元素必须是对象")
        expert_id = str(item.get("expert_id") or "").strip()
        if not expert_id or expert_id in member_ids:
            raise ExpertTeamBundleError("members 包含空或重复 expert_id")
        expert = expert_registry.get(expert_id)
        if expert is None:
            raise ExpertTeamBundleError(f"专家团成员不存在或不是单专家: {expert_id}")
        mention_name = str(item.get("mention_name") or expert.name).strip()
        if not mention_name or mention_name in mentions:
            raise ExpertTeamBundleError("members.mention_name 必须在团队内唯一")
        member_ids.add(expert_id)
        mentions.add(mention_name)
        members.append(ExpertTeamMember(
            expert_id=expert_id,
            mention_name=mention_name,
            routing_brief=str(item.get("routing_brief") or "").strip(),
        ))
    if coordinator_expert_id not in member_ids:
        raise ExpertTeamBundleError("coordinator_expert_id 必须属于 members")
    return ExpertTeamDefinition(
        team_id=team_id,
        name=name,
        rules_markdown=rules_path.read_text(encoding="utf-8").strip(),
        coordinator_expert_id=coordinator_expert_id,
        members=tuple(members),
        source_dir=root,
    )


def build_expert_team_catalog(team_registry: ExpertTeamRegistry | None, expert_registry: ExpertRegistry | None) -> list[dict[str, Any]]:
    if team_registry is None:
        return []
    items: list[dict[str, Any]] = []
    for team in sorted(team_registry.definitions.values(), key=lambda item: item.team_id):
        coordinator = expert_registry.get(team.coordinator_expert_id) if expert_registry else None
        members = []
        for member in team.members:
            expert = expert_registry.get(member.expert_id) if expert_registry else None
            members.append({
                "expert_id": member.expert_id,
                "name": expert.name if expert else member.expert_id,
                "mention_name": member.mention_name,
                "routing_brief": member.routing_brief,
                "is_coordinator": member.expert_id == team.coordinator_expert_id,
            })
        items.append({
            "team_id": team.team_id,
            "name": team.name,
            "description": _team_summary(team.rules_markdown),
            "topology": "team",
            "coordinator_expert_id": team.coordinator_expert_id,
            "coordinator_name": coordinator.name if coordinator else team.coordinator_expert_id,
            "members": members,
        })
    return items


def _team_summary(markdown: str) -> str:
    for line in str(markdown or "").splitlines():
        text = line.strip().lstrip("- ").strip()
        if text and not text.startswith("#"):
            return text[:180]
    return "由主协调专家分流、并由成员专家持续接管的专家团。"
