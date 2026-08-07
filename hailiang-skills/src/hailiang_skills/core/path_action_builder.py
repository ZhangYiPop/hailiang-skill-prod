from __future__ import annotations

from hailiang_skills.schemas.message_blocks import MessageBlock
from hailiang_skills.skills.assets import load_json


def build_path_actions_block(path_names: list[str]) -> dict | None:
    if not path_names:
        return None
    catalog = load_json("assets/generated/multiroute/path_catalog.json", [])
    actions = []
    for path_name in path_names:
        matched = next(
            (
                item
                for item in catalog
                if item.get("primary_category") == path_name or item.get("path_id") == path_name
            ),
            None,
        )
        if not matched:
            continue
        actions.append(
            {
                "path_id": matched.get("path_id"),
                "path_name": matched.get("primary_category"),
                "description": matched.get("description", ""),
                "source": {
                    "file": "assets/generated/multiroute/path_catalog.json",
                    "record_id": matched.get("path_id"),
                    "sheet": matched.get("sheet_group"),
                },
            }
        )
    if not actions:
        return None
    return MessageBlock(type="path_actions", payload={"actions": actions}).model_dump()


def build_citations_block(context, active_skill: str | None = None) -> dict | None:
    fact_items = []
    asset_items = []
    for key, record in context.known_facts.facts.items():
        if record.value in (None, "", [], {}):
            continue
        fact_items.append(
            {
                "kind": "fact",
                "key": key,
                "title": key,
                "summary": f"{key} · {record.scope}",
                "detail": {
                    "key": key,
                    "value": record.value,
                    "scope": record.scope,
                    "source_type": record.source_type,
                    "source_id": record.source_id,
                    "source_label": record.source_label or record.source_skill,
                    "source_skill": record.source_skill,
                    "updated_at": record.updated_at,
                },
            }
        )
    for item in context.candidate_paths[:5]:
        if not item.get("primary_category"):
            continue
        asset_items.append(
            {
                "kind": "asset",
                "title": item.get("primary_category"),
                "summary": item.get("description") or item.get("sheet_group") or "",
                "detail": {
                    "title": item.get("primary_category"),
                    "record_id": item.get("path_id"),
                    "file": "assets/generated/multiroute/path_catalog.json",
                    "sheet": item.get("sheet_group"),
                    "active_skill": active_skill,
                    "raw": item,
                },
            }
        )
    if not fact_items and not asset_items:
        return None
    return MessageBlock(
        type="citations",
        payload={
            "groups": [
                {"kind": "fact", "label": "Fact", "items": fact_items[:12]},
                {"kind": "asset", "label": "Asset", "items": asset_items[:12]},
            ]
        },
    ).model_dump()
