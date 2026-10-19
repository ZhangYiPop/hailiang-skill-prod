"""Jump cards payload and recommend catalog validation.

Model emits semantic jump_cards JSON; catalog.json enumerates allowed
intent / object_type / sub_type combinations for validation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


ALLOWED_FILTER_KEYS = ("region", "grade", "keyword", "category")


class JumpCardsError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


class JumpCardFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: str = ""
    grade: str = ""
    keyword: str = ""
    category: str = ""


class JumpCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1)
    object_type: str = Field(min_length=1)
    sub_type: str = ""
    reason: str = ""
    filter: JumpCardFilter = Field(default_factory=JumpCardFilter)
    title: str = ""
    description: str = ""

    @field_validator("filter", mode="before")
    @classmethod
    def _normalize_filter(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise JumpCardsError("INVALID_FILTER", "filter 必须是对象")
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key or "").strip()
            if not name:
                continue
            if name not in ALLOWED_FILTER_KEYS:
                raise JumpCardsError("INVALID_FILTER_KEY", f"filter 键不被允许: {name}")
            cleaned[name] = "" if item is None else str(item)
        return cleaned


class JumpCardsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jump_cards: list[JumpCard] = Field(default_factory=list)

    @model_validator(mode="after")
    def _max_one_by_default(self) -> JumpCardsPayload:
        # Structural default; parse_and_validate may tighten further via max_cards.
        return self


@dataclass(frozen=True, slots=True)
class CatalogItem:
    id: str
    title: str
    intent: str
    object_type: str
    sub_type: str = ""
    kind: str = "feature"
    when: str = ""
    url_or_route: str = ""
    description: str = ""

    def enum_key(self) -> tuple[str, str, str]:
        return (self.intent, self.object_type, self.sub_type)


@dataclass(frozen=True, slots=True)
class JumpCardCatalog:
    schema_version: int = 1
    items: tuple[CatalogItem, ...] = field(default_factory=tuple)

    @classmethod
    def from_payload(cls, payload: Any) -> JumpCardCatalog:
        if not isinstance(payload, dict):
            raise JumpCardsError("CATALOG_TYPE", "catalog 顶层必须是对象")
        try:
            schema_version = int(payload.get("schema_version") or 1)
        except (TypeError, ValueError) as exc:
            raise JumpCardsError("CATALOG_SCHEMA", "schema_version 必须是整数") from exc
        if schema_version != 1:
            raise JumpCardsError("CATALOG_SCHEMA", f"不支持的 catalog schema_version: {schema_version}")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise JumpCardsError("CATALOG_EMPTY", "catalog.items 不能为空")
        items: list[CatalogItem] = []
        seen_ids: set[str] = set()
        seen_enums: set[tuple[str, str, str]] = set()
        for index, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                raise JumpCardsError("CATALOG_ITEM", f"catalog.items[{index}] 必须是对象")
            item_id = str(raw.get("id") or "").strip()
            title = str(raw.get("title") or "").strip()
            intent = str(raw.get("intent") or "").strip()
            object_type = str(raw.get("object_type") or "").strip()
            sub_type = str(raw.get("sub_type") or "").strip()
            if not item_id or not title or not intent or not object_type:
                raise JumpCardsError(
                    "CATALOG_ITEM",
                    f"catalog.items[{index}] 必须包含 id、title、intent、object_type",
                )
            if item_id in seen_ids:
                raise JumpCardsError("CATALOG_DUPLICATE_ID", f"catalog id 重复: {item_id}")
            key = (intent, object_type, sub_type)
            if key in seen_enums:
                raise JumpCardsError(
                    "CATALOG_DUPLICATE_ENUM",
                    f"catalog 枚举重复: {intent}/{object_type}/{sub_type or '-'}",
                )
            if object_type == "single_assessment" and not sub_type:
                raise JumpCardsError(
                    "CATALOG_SUB_TYPE",
                    f"catalog.items[{index}] single_assessment 必须带 sub_type",
                )
            seen_ids.add(item_id)
            seen_enums.add(key)
            items.append(
                CatalogItem(
                    id=item_id,
                    title=title,
                    intent=intent,
                    object_type=object_type,
                    sub_type=sub_type,
                    kind=str(raw.get("kind") or "feature").strip() or "feature",
                    when=str(raw.get("when") or "").strip(),
                    url_or_route=str(raw.get("url_or_route") or "").strip(),
                    description=str(raw.get("description") or "").strip(),
                )
            )
        return cls(schema_version=schema_version, items=tuple(items))

    @classmethod
    def load(cls, path: str | Path) -> JumpCardCatalog:
        file_path = Path(path)
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise JumpCardsError("CATALOG_JSON", f"catalog.json 解析失败: {exc.msg}") from exc
        return cls.from_payload(payload)

    def index(self) -> dict[tuple[str, str, str], CatalogItem]:
        return {item.enum_key(): item for item in self.items}

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "items": [
                {
                    "id": item.id,
                    "title": item.title,
                    "kind": item.kind,
                    "when": item.when,
                    "intent": item.intent,
                    "object_type": item.object_type,
                    **({"sub_type": item.sub_type} if item.sub_type else {}),
                    "url_or_route": item.url_or_route,
                    **({"description": item.description} if item.description else {}),
                }
                for item in self.items
            ],
        }


def _extract_json(raw: str | dict[str, Any] | list[Any]) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {"jump_cards": []}
    fenced = re.search(r"```(?:json)?\s*([\[\{].*?[\]\}])\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    else:
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start >= 0 and end > start:
                text = text[start : end + 1]
                break
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise JumpCardsError("INVALID_JSON", f"无法解析 jump_cards JSON: {exc}") from exc


def _catalog_index(catalog: Any) -> dict[tuple[str, str, str], dict[str, Any]]:
    if isinstance(catalog, JumpCardCatalog):
        return {
            key: {
                "id": item.id,
                "title": item.title,
                "intent": item.intent,
                "object_type": item.object_type,
                "sub_type": item.sub_type,
                "description": item.description,
            }
            for key, item in catalog.index().items()
        }
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    items: list[Any]
    if isinstance(catalog, dict):
        if "items" in catalog:
            try:
                return _catalog_index(JumpCardCatalog.from_payload(catalog))
            except JumpCardsError:
                pass
        raw_items = catalog.get("items") or catalog.get("jump_cards") or catalog.get("recommendations") or []
        items = list(raw_items) if isinstance(raw_items, list) else []
    elif isinstance(catalog, list):
        items = catalog
    else:
        items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("intent") or "").strip(),
            str(item.get("object_type") or "").strip(),
            str(item.get("sub_type") or "").strip(),
        )
        if key[0] and key[1]:
            index[key] = item
    return index


def parse_and_validate_jump_cards(
    raw: str | dict[str, Any] | list[Any],
    *,
    catalog: Any = None,
    max_cards: int = 1,
) -> JumpCardsPayload:
    """Strict parse for system-skill / publish paths. Unknown enums raise."""
    payload_raw = _extract_json(raw)
    if isinstance(payload_raw, list):
        payload_raw = {"jump_cards": payload_raw}
    if not isinstance(payload_raw, dict):
        raise JumpCardsError("JUMP_CARDS_PAYLOAD", "jump_cards 载荷无效")
    try:
        payload = JumpCardsPayload.model_validate(payload_raw)
    except JumpCardsError:
        raise
    except ValidationError as exc:
        raise JumpCardsError("JUMP_CARDS_INVALID", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise JumpCardsError("JUMP_CARDS_INVALID", str(exc)) from exc

    limit = max(0, int(max_cards))
    if len(payload.jump_cards) > limit:
        raise JumpCardsError("JUMP_CARDS_MAX", f"jump_cards 最多 {limit} 条")

    catalog_index = _catalog_index(catalog) if catalog is not None else {}
    for card in payload.jump_cards:
        if card.object_type == "single_assessment" and not card.sub_type:
            raise JumpCardsError("JUMP_CARDS_SUB_TYPE_REQUIRED", "single_assessment 必须带 sub_type")
        key = (card.intent, card.object_type, card.sub_type)
        if catalog_index and key not in catalog_index:
            # Require exact enum match in strict mode (no empty-subtype fallback).
            raise JumpCardsError(
                "JUMP_CARDS_ENUM",
                f"jump_cards 枚举不在 catalog 中: {card.intent}/{card.object_type}/{card.sub_type or '-'}",
            )
    return payload


def parse_jump_cards(
    raw: str | dict[str, Any] | list[Any],
    *,
    catalog: Any = None,
    max_cards: int = 1,
) -> list[dict[str, Any]]:
    """Best-effort parse for recommendation extraction. Unknown enums are dropped."""
    payload = _extract_json(raw)
    if isinstance(payload, list):
        cards_raw = payload
    elif isinstance(payload, dict):
        cards_raw = payload.get("jump_cards") or payload.get("cards") or []
    else:
        raise JumpCardsError("INVALID_PAYLOAD", "jump_cards 载荷无效")
    if not isinstance(cards_raw, list):
        raise JumpCardsError("INVALID_PAYLOAD", "jump_cards 必须是数组")

    catalog_index = _catalog_index(catalog) if catalog is not None else {}
    cards: list[dict[str, Any]] = []
    for item in cards_raw:
        if not isinstance(item, dict):
            continue
        try:
            card = JumpCard.model_validate(item)
        except JumpCardsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise JumpCardsError("INVALID_CARD", str(exc)) from exc
        if card.object_type == "single_assessment" and not card.sub_type:
            raise JumpCardsError("MISSING_SUB_TYPE", "single_assessment 必须带 sub_type")
        key = (card.intent, card.object_type, card.sub_type)
        if catalog_index and key not in catalog_index and (card.intent, card.object_type, "") not in catalog_index:
            continue
        entry = card.model_dump()
        catalog_hit = catalog_index.get(key) or catalog_index.get((card.intent, card.object_type, ""))
        if catalog_hit:
            if not entry.get("title"):
                entry["title"] = str(catalog_hit.get("title") or catalog_hit.get("name") or "")
            if not entry.get("description"):
                entry["description"] = str(catalog_hit.get("description") or "")
        cards.append(entry)
        if len(cards) >= max(0, int(max_cards)):
            break
    return cards
