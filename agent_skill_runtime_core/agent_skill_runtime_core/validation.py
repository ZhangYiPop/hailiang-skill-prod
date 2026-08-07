from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class SkillPackageError(ValueError):
    pass


def _frontmatter(markdown: str) -> dict[str, Any]:
    if not markdown.startswith("---"):
        return {}
    parts = markdown.split("---", 2)
    if len(parts) < 3:
        return {}
    parsed = yaml.safe_load(parts[1]) or {}
    return parsed if isinstance(parsed, dict) else {}


def validate_skill_directory(skill_dir: str | Path, *, strict_identity: bool = True) -> dict[str, Any]:
    root = Path(skill_dir).expanduser().resolve()
    if not root.is_dir():
        raise SkillPackageError(f"skill directory does not exist: {root}")

    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        raise SkillPackageError("SKILL.md is required")

    metadata = _frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    name = str(metadata.get("name") or "").strip()
    description = str(metadata.get("description") or "").strip()
    skill_id = str(metadata.get("skill_id") or metadata.get("id") or "").strip()
    if not name:
        raise SkillPackageError("SKILL.md frontmatter must include non-empty name")
    if not description:
        raise SkillPackageError("SKILL.md frontmatter must include non-empty description")
    if strict_identity and skill_id and root.name != skill_id:
        raise SkillPackageError(
            f"skill directory name must match skill_id for strict runtime loading: {root.name} != {skill_id}"
        )

    for folder in ("references", "scripts", "assets"):
        candidate = root / folder
        if candidate.exists() and not candidate.is_dir():
            raise SkillPackageError(f"{folder}/ must be a directory when present")

    for path in root.rglob("*"):
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise SkillPackageError(f"unsafe path outside skill root: {path}") from exc
        if ".." in relative.parts:
            raise SkillPackageError(f"unsafe relative path in skill package: {relative.as_posix()}")

    return metadata
