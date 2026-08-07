"""Turn Native multi-path conclusions into the existing path action contract."""

from __future__ import annotations

from typing import Any

from hailiang_skills.skills.assets import load_json


MULTI_PATH_SKILL_ID = "multi_path_planning"
_CATALOG_PATH = "assets/generated/multiroute/path_catalog.json"
_ALIASES = {
    "三大专项（高校专项/地方专项）": ("三大专项",),
    "艺术体育": ("艺术升学", "体育升学"),
}


def path_catalog() -> list[dict[str, Any]]:
    raw = load_json(_CATALOG_PATH, [])
    return [item for item in raw if isinstance(item, dict) and item.get("path_id") and item.get("primary_category")]


def _catalog_indexes(catalog: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_id = {str(item["path_id"]): item for item in catalog}
    by_name = {str(item["primary_category"]): item for item in catalog}
    return by_id, by_name


def resolve_matched_paths(raw_paths: Any, catalog: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Resolve model-provided path IDs/names against the generated whitelist."""
    catalog = catalog if catalog is not None else path_catalog()
    by_id, by_name = _catalog_indexes(catalog)
    values = raw_paths if isinstance(raw_paths, list) else []
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_value in values:
        value = str(raw_value or "").strip()
        if not value:
            continue
        matches = []
        if value in by_id:
            matches = [by_id[value]]
        elif value in by_name:
            matches = [by_name[value]]
        else:
            matches = [by_name[alias] for alias in _ALIASES.get(value, ()) if alias in by_name]
        for item in matches:
            path_id = str(item.get("path_id") or "")
            if path_id and path_id not in seen:
                seen.add(path_id)
                resolved.append(item)
    return resolved


def extract_paths_from_reply(reply: str, catalog: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Fallback for valid legacy-style text conclusions when state_patch is absent."""
    catalog = catalog if catalog is not None else path_catalog()
    matches: list[tuple[int, int, dict[str, Any]]] = []
    text = str(reply or "")
    for item in catalog:
        name = str(item.get("primary_category") or "")
        if not name:
            continue
        position = text.find(name)
        if position >= 0:
            matches.append((position, -len(name), item))
    for alias, names in _ALIASES.items():
        position = text.find(alias)
        if position < 0:
            continue
        for name in names:
            item = next((entry for entry in catalog if entry.get("primary_category") == name), None)
            if item is not None:
                matches.append((position, -len(alias), item))
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _position, _length, item in sorted(matches, key=lambda value: (value[0], value[1])):
        path_id = str(item.get("path_id") or "")
        if path_id and path_id not in seen:
            seen.add(path_id)
            resolved.append(item)
    return resolved


def resolve_native_path_options(
    skill_id: str,
    stage: str,
    reply: str,
    matched_paths: Any,
) -> list[dict[str, Any]]:
    """Resolve structured paths first, then use text only at the output stage."""
    if str(skill_id or "") != MULTI_PATH_SKILL_ID:
        return []
    catalog = path_catalog()
    structured = resolve_matched_paths(matched_paths, catalog)
    if structured:
        return structured
    if str(stage or "") != "output":
        return []
    return extract_paths_from_reply(reply, catalog)
