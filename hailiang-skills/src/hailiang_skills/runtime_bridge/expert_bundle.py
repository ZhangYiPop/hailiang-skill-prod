"""Hailiang Expert Bundle loader.

An Expert Bundle is intentionally a *reference-only* package: Skills always
come from the platform's runtime_skills registry.  This keeps one immutable
Skill version reusable by many differently configured experts.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import yaml


class ExpertBundleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LockedSkill:
    skill_id: str
    version: str


@dataclass(frozen=True, slots=True)
class ExpertDefinition:
    agent_id: str
    name: str
    rules_markdown: str
    skills: tuple[LockedSkill, ...]
    max_iters: int = 4
    max_skill_calls: int = 3
    capabilities: tuple[str, ...] = (
        "execute_skill",
        "request_declared_form",
        "read_effective_facts",
    )
    topology: str = "single_expert"
    source_dir: Path | None = None

    @property
    def authorized_skill_ids(self) -> tuple[str, ...]:
        return tuple(item.skill_id for item in self.skills)


@dataclass(slots=True)
class ExpertRegistry:
    definitions: dict[str, ExpertDefinition]

    def get(self, agent_id: str) -> ExpertDefinition | None:
        return self.definitions.get(str(agent_id or "").strip())

    def require(self, agent_id: str) -> ExpertDefinition:
        definition = self.get(agent_id)
        if definition is None:
            raise ExpertBundleError(f"专家不存在: {agent_id}")
        return definition


def build_expert_catalog(expert_registry: ExpertRegistry | None, runtime_registry) -> list[dict[str, Any]]:
    """Return the small, public catalog needed by the testing UI.

    The catalog deliberately exposes only a human-readable summary and the
    locked Skill IDs.  It never returns an expert's raw prompt or grants the
    client a way to alter the lock file.
    """
    if expert_registry is None:
        return []
    items: list[dict[str, Any]] = []
    for definition in sorted(expert_registry.definitions.values(), key=lambda item: item.agent_id):
        skills = []
        for skill_id in definition.authorized_skill_ids:
            bundle = runtime_registry.get(skill_id) if runtime_registry is not None else None
            metadata = getattr(bundle, "runtime_metadata", None)
            skills.append({
                "skill_id": skill_id,
                "label": str(getattr(metadata, "name", "") or skill_id),
            })
        items.append({
            "expert_id": definition.agent_id,
            "name": definition.name,
            "description": _expert_summary(definition.rules_markdown),
            "topology": definition.topology,
            "skill_ids": list(definition.authorized_skill_ids),
            "skills": skills,
        })
    return items


def load_local_expert_registry(experts_root: str | Path, runtime_registry) -> ExpertRegistry:
    root = Path(experts_root).expanduser().resolve()
    if not root.is_dir():
        return ExpertRegistry(definitions={})
    definitions: dict[str, ExpertDefinition] = {}
    for child in sorted(item for item in root.iterdir() if item.is_dir()):
        if not (child / "agent.yaml").is_file():
            continue
        definition = load_expert_bundle(child, runtime_registry)
        if definition.agent_id in definitions:
            raise ExpertBundleError(f"重复的专家 ID: {definition.agent_id}")
        definitions[definition.agent_id] = definition
    return ExpertRegistry(definitions=definitions)


def load_expert_bundle(bundle_dir: str | Path, runtime_registry) -> ExpertDefinition:
    root = Path(bundle_dir).expanduser().resolve()
    if (root / "skills").exists():
        raise ExpertBundleError(f"专家包禁止携带 skills/ 目录: {root}")
    agent_path = root / "agent.yaml"
    rules_path = root / "AGENT.md"
    lock_path = root / "skills.lock.json"
    missing = [path.name for path in (agent_path, rules_path, lock_path) if not path.is_file()]
    if missing:
        raise ExpertBundleError(f"专家包缺少必需文件: {', '.join(missing)}")
    try:
        raw = yaml.safe_load(agent_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ExpertBundleError(f"agent.yaml 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise ExpertBundleError("agent.yaml 必须是对象")
    if int(raw.get("schema_version", 1) or 1) != 1:
        raise ExpertBundleError("不支持的 Expert Bundle schema_version")
    topology = str(raw.get("topology") or "single_expert").strip()
    if topology != "single_expert":
        raise ExpertBundleError("v1 仅支持 topology: single_expert；team/委派配置尚未启用")
    if any(key in raw for key in ("members", "delegation", "delegation_policy")):
        raise ExpertBundleError("v1 不接受 members 或 delegation 配置；专家团能力仅保留数据模型")
    agent_id = str(raw.get("id") or raw.get("agent_id") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not agent_id or not name:
        raise ExpertBundleError("agent.yaml 必须包含 id 和 name")
    if str(raw.get("rule_file") or "AGENT.md").strip() != "AGENT.md":
        raise ExpertBundleError("v1 专家规则文件必须是包根目录的 AGENT.md")
    declared_ids = _skill_ids(raw.get("skills"))
    if not declared_ids:
        raise ExpertBundleError("agent.yaml 必须声明至少一个 skills")
    locks = _load_locks(lock_path)
    if set(declared_ids) != set(locks):
        raise ExpertBundleError("agent.yaml.skills 必须与 skills.lock.json 的 Skill ID 完全一致")
    validated: list[LockedSkill] = []
    for skill_id in declared_ids:
        lock = locks[skill_id]
        bundle = runtime_registry.get_raw(skill_id)
        if bundle is None:
            raise ExpertBundleError(f"专家依赖的 runtime_skills Skill 不存在: {skill_id}")
        actual_id = str(bundle.runtime_metadata.skill_id or bundle.contract.skill_id or skill_id)
        # Older Runtime Skills did not all publish a version. Lock them to an
        # explicit baseline instead of weakening version validation.
        actual_version = str(bundle.runtime_metadata.version or bundle.metadata.get("version") or "0.0.0")
        if actual_id != lock.skill_id:
            raise ExpertBundleError(f"Skill ID 不匹配: lock={lock.skill_id}, runtime={actual_id}")
        if actual_version != lock.version:
            raise ExpertBundleError(f"Skill 版本不匹配 ({skill_id}): lock={lock.version}, runtime={actual_version}")
        validated.append(lock)
    budget = raw.get("budget") if isinstance(raw.get("budget"), dict) else {}
    max_iters = _positive_int(budget.get("max_iters", raw.get("max_iters", 4)), "max_iters")
    max_skill_calls = _positive_int(budget.get("max_skill_calls", raw.get("max_skill_calls", 3)), "max_skill_calls")
    if max_iters > 4 or max_skill_calls > 3:
        raise ExpertBundleError("v1 安全上限为 max_iters=4、max_skill_calls=3")
    capabilities = tuple(str(item).strip() for item in raw.get("capabilities", []) if str(item).strip())
    allowed = {"execute_skill", "request_declared_form", "read_effective_facts"}
    if capabilities and not set(capabilities).issubset(allowed):
        raise ExpertBundleError("专家包声明了 v1 不允许的能力")
    return ExpertDefinition(
        agent_id=agent_id,
        name=name,
        rules_markdown=rules_path.read_text(encoding="utf-8").strip(),
        skills=tuple(validated),
        max_iters=max_iters,
        max_skill_calls=max_skill_calls,
        capabilities=capabilities or tuple(sorted(allowed)),
        topology=topology,
        source_dir=root,
    )


def _skill_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        skill_id = str(item.get("skill_id") if isinstance(item, dict) else item or "").strip()
        if not skill_id or skill_id in result:
            raise ExpertBundleError("agent.yaml.skills 包含空或重复的 Skill ID")
        result.append(skill_id)
    return result


def _load_locks(path: Path) -> dict[str, LockedSkill]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExpertBundleError(f"skills.lock.json 解析失败: {exc}") from exc
    entries = raw.get("skills") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ExpertBundleError("skills.lock.json 必须包含 skills 数组")
    locks: dict[str, LockedSkill] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ExpertBundleError("skills.lock.json.skills 元素必须是对象")
        skill_id = str(entry.get("skill_id") or entry.get("id") or "").strip()
        version = str(entry.get("version") or "").strip()
        if not skill_id or not version:
            raise ExpertBundleError("skills.lock.json 每项必须包含 skill_id、version")
        if skill_id in locks:
            raise ExpertBundleError(f"skills.lock.json 存在重复 Skill: {skill_id}")
        locks[skill_id] = LockedSkill(skill_id, version)
    return locks


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ExpertBundleError(f"{name} 必须是正整数") from exc
    if parsed < 1:
        raise ExpertBundleError(f"{name} 必须是正整数")
    return parsed


def _expert_summary(rules_markdown: str) -> str:
    for line in str(rules_markdown or "").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        content = raw.lstrip("- ").strip()
        if content and not content.startswith("你是"):
            return content[:180]
    return "由平台锁定 Skill 组合与业务规则的专家。"
