"""Load and query the skill duty registration table (规范 §9 / P3)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from hailiang_skills.core.skill_ids import canonical_skill_id

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "skill_duty_registry.yml"
_VALID_STAGES = frozenset({"primary", "junior", "senior"})


@lru_cache(maxsize=1)
def load_skill_duty_registry(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    config_path = Path(path) if path else _CONFIG_PATH
    if not config_path.is_file():
        return {}
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    skills = payload.get("skills") if isinstance(payload, dict) else None
    if not isinstance(skills, dict):
        return {}
    registry: dict[str, dict[str, Any]] = {}
    for raw_id, item in skills.items():
        skill_id = canonical_skill_id(str(raw_id or "").strip()) or str(raw_id or "").strip()
        if not skill_id or not isinstance(item, dict):
            continue
        stages = _normalize_stages(item.get("school_stages"))
        registry[skill_id] = {
            "skill_id": skill_id,
            "name": str(item.get("name") or "").strip(),
            "school_stages": stages,
        }
    return registry


def registered_school_stages(skill_id: str, *, path: str | Path | None = None) -> tuple[str, ...] | None:
    """Return registered stages, or None when the skill is not in the duty table."""
    entry = load_skill_duty_registry(path).get(canonical_skill_id(skill_id) or skill_id)
    if entry is None:
        return None
    return tuple(entry.get("school_stages") or ())


def clear_skill_duty_registry_cache() -> None:
    load_skill_duty_registry.cache_clear()


def _normalize_stages(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str):
        text = raw.strip().lower()
        if not text or text == "all":
            return ()
        parts = [part.strip() for part in text.replace("-", "_").replace("/", ",").split(",") if part.strip()]
    elif isinstance(raw, list):
        parts = [str(item).strip().lower() for item in raw if str(item).strip()]
    else:
        return ()
    if "all" in parts:
        return ()
    return tuple(part for part in parts if part in _VALID_STAGES)
