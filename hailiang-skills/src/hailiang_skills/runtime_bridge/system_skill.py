"""System skill package validation for orchestrate_head / orchestrate_tail / recommend.

System skills are not business skills: packages are SKILL.md-only (recommend may
also ship assets/catalog.json), frontmatter is limited to five fields, and the
body must follow the fixed five-section outline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from hailiang_skills.runtime_bridge.jump_cards import JumpCardCatalog, JumpCardsError


SystemSkillPurpose = Literal["orchestrate_head", "orchestrate_tail", "recommend"]

ALLOWED_FRONTMATTER_KEYS = ("skill_type", "purpose", "skill_id", "name", "version")
REQUIRED_SECTION_TITLES = ("职责", "可用输入", "判断规则", "输出要求", "禁止")
SECTION_HEADER_RE = re.compile(
    r"^#{1,3}\s*(?:\d+[.\u3001、)]\s*)?(职责|可用输入|判断规则|输出要求|禁止)\s*$",
    re.MULTILINE,
)
FORBIDDEN_ROOT_NAMES = {
    "runtime_contract.json",
    "scripts",
    "references",
    "questionnaire.config.json",
    "teams",
    "runtime_agent_teams",
}


class SystemSkillError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = str(code or "SYSTEM_SKILL_INVALID").strip() or "SYSTEM_SKILL_INVALID"
        self.message = str(message or "").strip() or self.code
        super().__init__(f"{self.code}: {self.message}")


@dataclass(frozen=True, slots=True)
class SystemSkillDefinition:
    skill_id: str
    name: str
    version: str
    purpose: SystemSkillPurpose
    body: str
    source_dir: Path | None = None
    catalog: dict[str, Any] | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)

    def as_snapshot(self) -> dict[str, Any]:
        payload = {
            "skill_id": self.skill_id,
            "name": self.name,
            "version": self.version,
            "purpose": self.purpose,
            "body": self.body,
            "skill_type": "system",
        }
        if self.catalog is not None:
            payload["catalog"] = self.catalog
        return payload


def default_system_skills_root() -> Path:
    return Path(__file__).resolve().parents[3] / "runtime_system_skills"


def parse_system_skill_markdown(text: str, *, expected_purpose: str | None = None) -> tuple[dict[str, Any], str]:
    frontmatter, body = _split_frontmatter(text)
    validated = validate_system_skill_frontmatter(frontmatter, expected_purpose=expected_purpose)
    validate_system_skill_body(body)
    return validated, body


def validate_system_skill_frontmatter(
    frontmatter: dict[str, Any],
    *,
    expected_purpose: str | None = None,
) -> dict[str, Any]:
    if not isinstance(frontmatter, dict) or not frontmatter:
        raise SystemSkillError("SYSTEM_SKILL_FRONTMATTER", "frontmatter 不能为空")
    unknown = [key for key in frontmatter if key not in ALLOWED_FRONTMATTER_KEYS]
    if unknown:
        raise SystemSkillError(
            "SYSTEM_SKILL_FRONTMATTER_KEY",
            "frontmatter 仅允许 " + "、".join(ALLOWED_FRONTMATTER_KEYS) + f"；发现: {', '.join(unknown)}",
        )
    missing = [key for key in ALLOWED_FRONTMATTER_KEYS if key not in frontmatter]
    if missing:
        raise SystemSkillError(
            "SYSTEM_SKILL_FRONTMATTER_MISSING",
            "frontmatter 缺少 " + "、".join(missing),
        )
    skill_type = str(frontmatter.get("skill_type") or "").strip()
    purpose = str(frontmatter.get("purpose") or "").strip()
    skill_id = str(frontmatter.get("skill_id") or "").strip()
    name = str(frontmatter.get("name") or "").strip()
    version = str(frontmatter.get("version") or "").strip()
    if skill_type != "system":
        raise SystemSkillError("SYSTEM_SKILL_TYPE", "skill_type 必须是 system")
    if purpose not in {"orchestrate_head", "orchestrate_tail", "recommend"}:
        raise SystemSkillError(
            "SYSTEM_SKILL_PURPOSE",
            "purpose 必须是 orchestrate_head、orchestrate_tail 或 recommend",
        )
    if expected_purpose and purpose != expected_purpose:
        raise SystemSkillError(
            "SYSTEM_SKILL_PURPOSE_MISMATCH",
            f"purpose 期望 {expected_purpose}，实际 {purpose}",
        )
    if not skill_id or not name or not version:
        raise SystemSkillError("SYSTEM_SKILL_IDENTITY", "skill_id、name、version 不能为空")
    return {
        "skill_type": skill_type,
        "purpose": purpose,
        "skill_id": skill_id,
        "name": name,
        "version": version,
    }


def validate_system_skill_body(body: str) -> None:
    text = str(body or "").strip()
    if not text:
        raise SystemSkillError("SYSTEM_SKILL_BODY_EMPTY", "SKILL.md 正文不能为空")
    found = SECTION_HEADER_RE.findall(text)
    if tuple(found) != REQUIRED_SECTION_TITLES:
        raise SystemSkillError(
            "SYSTEM_SKILL_SECTIONS",
            "正文必须且仅按顺序包含五节：职责、可用输入、判断规则、输出要求、禁止",
        )


def validate_system_skill_package(
    package_dir: str | Path,
    *,
    expected_purpose: str | None = None,
) -> SystemSkillDefinition:
    root = Path(package_dir).expanduser().resolve()
    if not root.is_dir():
        raise SystemSkillError("SYSTEM_SKILL_DIR", f"系统 skill 目录不存在: {root}")
    skill_file = root / "SKILL.md"
    if not skill_file.is_file():
        raise SystemSkillError("SYSTEM_SKILL_FILE", "系统 skill 包必须包含 SKILL.md")

    for child in sorted(root.iterdir()):
        name = child.name
        if name.startswith("."):
            continue
        if name == "SKILL.md":
            continue
        if name in FORBIDDEN_ROOT_NAMES:
            raise SystemSkillError("SYSTEM_SKILL_FORBIDDEN_PATH", f"系统 skill 禁止包含 {name}")
        if name == "assets":
            continue
        raise SystemSkillError(
            "SYSTEM_SKILL_EXTRA_PATH",
            f"系统 skill 根目录只允许 SKILL.md（recommend 可另有 assets/）: 发现 {name}",
        )

    frontmatter, body = parse_system_skill_markdown(
        skill_file.read_text(encoding="utf-8"),
        expected_purpose=expected_purpose,
    )
    purpose = str(frontmatter["purpose"])
    catalog_payload: dict[str, Any] | None = None
    assets_dir = root / "assets"
    if purpose == "recommend":
        if not assets_dir.is_dir():
            raise SystemSkillError("SYSTEM_SKILL_CATALOG_MISSING", "recommend 包必须包含 assets/catalog.json")
        catalog_file = assets_dir / "catalog.json"
        if not catalog_file.is_file():
            raise SystemSkillError("SYSTEM_SKILL_CATALOG_MISSING", "recommend 包必须包含 assets/catalog.json")
        for child in sorted(assets_dir.iterdir()):
            if child.name.startswith("."):
                continue
            if child.name != "catalog.json":
                raise SystemSkillError(
                    "SYSTEM_SKILL_ASSETS_EXTRA",
                    f"recommend assets/ 仅允许 catalog.json: 发现 {child.name}",
                )
        try:
            loaded = json.loads(catalog_file.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise SystemSkillError("SYSTEM_SKILL_CATALOG_JSON", f"catalog.json 解析失败: {exc.msg}") from exc
        if not isinstance(loaded, dict):
            raise SystemSkillError("SYSTEM_SKILL_CATALOG_TYPE", "catalog.json 顶层必须是对象")
        try:
            JumpCardCatalog.from_payload(loaded)
        except JumpCardsError as exc:
            code = exc.code
            if code == "CATALOG_EMPTY":
                code = "SYSTEM_SKILL_CATALOG_EMPTY"
            elif code.startswith("CATALOG_"):
                code = f"SYSTEM_SKILL_{code}"
            raise SystemSkillError(code, exc.message) from exc
        catalog_payload = loaded
    elif assets_dir.exists():
        raise SystemSkillError(
            "SYSTEM_SKILL_ASSETS_FORBIDDEN",
            f"{purpose} 包不允许 assets/ 目录",
        )

    return SystemSkillDefinition(
        skill_id=str(frontmatter["skill_id"]),
        name=str(frontmatter["name"]),
        version=str(frontmatter["version"]),
        purpose=purpose,  # type: ignore[arg-type]
        body=body.strip(),
        source_dir=root,
        catalog=catalog_payload,
        frontmatter=frontmatter,
    )


def load_system_skill_registry(
    root: str | Path | None = None,
) -> dict[str, SystemSkillDefinition]:
    base = Path(root).expanduser().resolve() if root else default_system_skills_root()
    if not base.is_dir():
        return {}
    registry: dict[str, SystemSkillDefinition] = {}
    for child in sorted(item for item in base.iterdir() if item.is_dir()):
        if not (child / "SKILL.md").is_file():
            continue
        definition = validate_system_skill_package(child)
        if definition.skill_id in registry:
            raise SystemSkillError("SYSTEM_SKILL_DUPLICATE", f"重复的 system skill_id: {definition.skill_id}")
        registry[definition.skill_id] = definition
    return registry


def load_system_skills_by_purpose(
    root: str | Path | None = None,
) -> dict[str, SystemSkillDefinition]:
    by_id = load_system_skill_registry(root)
    by_purpose: dict[str, SystemSkillDefinition] = {}
    for definition in by_id.values():
        if definition.purpose in by_purpose:
            raise SystemSkillError(
                "SYSTEM_SKILL_PURPOSE_DUPLICATE",
                f"重复的 purpose: {definition.purpose}",
            )
        by_purpose[definition.purpose] = definition
    return by_purpose


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = str(text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        raise SystemSkillError("SYSTEM_SKILL_FRONTMATTER", "SKILL.md 必须以 YAML frontmatter 开头")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise SystemSkillError("SYSTEM_SKILL_FRONTMATTER", "frontmatter 缺少结束标记 ---")
    raw = "\n".join(lines[1:end])
    try:
        loaded = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise SystemSkillError("SYSTEM_SKILL_FRONTMATTER_YAML", f"frontmatter 解析失败: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SystemSkillError("SYSTEM_SKILL_FRONTMATTER", "frontmatter 必须是对象")
    body = "\n".join(lines[end + 1 :]).strip()
    return loaded, body
