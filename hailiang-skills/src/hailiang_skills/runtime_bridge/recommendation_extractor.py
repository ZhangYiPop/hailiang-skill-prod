"""Recommendation step: call recommend skill / LLM to extract jump_cards."""

from __future__ import annotations

import json
from typing import Any, Protocol

from hailiang_skills.runtime_bridge.jump_cards import parse_jump_cards
from hailiang_skills.skill_runtime.models import ChatMessage


class RecommendLLMClient(Protocol):
    def complete(self, messages: list[ChatMessage], **kwargs: Any) -> str: ...


def _catalog_items(catalog: Any) -> list[dict[str, Any]]:
    if isinstance(catalog, dict):
        items = catalog.get("items") or catalog.get("jump_cards") or catalog.get("recommendations") or []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
    if isinstance(catalog, list):
        return [item for item in catalog if isinstance(item, dict)]
    return []


def build_recommend_prompt(
    *,
    recommendation_rules: str,
    catalog: Any,
    user_question: str = "",
    assistant_reply: str = "",
    max_cards: int = 3,
) -> list[ChatMessage]:
    items = _catalog_items(catalog)
    slim = []
    for item in items:
        intent = str(item.get("intent") or "").strip()
        object_type = str(item.get("object_type") or "").strip()
        if not intent or not object_type:
            continue
        # `when` is the model-facing trigger hint; fall back to description for older catalogs.
        when = str(item.get("when") or item.get("description") or "").strip()
        slim.append({
            "intent": intent,
            "object_type": object_type,
            "sub_type": str(item.get("sub_type") or "").strip(),
            "title": str(item.get("title") or item.get("name") or "").strip(),
            "when": when,
        })
    system_parts = [
        str(recommendation_rules or "").strip(),
        "只从下列推荐清单中挑选，输出 jump_cards JSON。不要查询会话状态，不要编造清单外条目。",
        "主要依据本轮话题/回答是否命中某条 when；用户很少会直接点名测评或咨询师。回答在解释或追问细节时，只要话题已命中 when，仍应出卡。",
        "多个条目相关时只留最相关的一条；仅寒暄、拒答、无关闲聊或只能靠专家身份硬猜时才返回空数组。",
        f"最多 {max(0, int(max_cards))} 条。若没有合适条目，返回 {{\"jump_cards\": []}}。",
        "推荐清单（when = 何时推荐）：",
        json.dumps(slim, ensure_ascii=False, indent=2),
        "用户本轮问题：",
        str(user_question or "").strip() or "（未提供）",
        "专家/平台回复：",
        str(assistant_reply or "").strip() or "（未提供）",
        "输出格式：",
        json.dumps(
            {
                "jump_cards": [
                    {
                        "intent": "open",
                        "object_type": "skill",
                        "sub_type": "",
                        "filter": {},
                        "title": "展示标题",
                        "description": "简短说明",
                    }
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
    ]
    system = "\n\n".join(part for part in system_parts if part)
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content="请抽取本轮 jump_cards。"),
    ]


def extract_jump_cards(
    client: RecommendLLMClient,
    catalog: Any,
    *,
    recommendation_rules: str = "",
    user_question: str = "",
    assistant_reply: str = "",
    max_cards: int = 3,
) -> list[dict[str, Any]]:
    """Ask the recommend model for jump_cards; validate against catalog."""
    if not _catalog_items(catalog):
        return []
    prompt = build_recommend_prompt(
        recommendation_rules=recommendation_rules,
        catalog=catalog,
        user_question=user_question,
        assistant_reply=assistant_reply,
        max_cards=max_cards,
    )
    raw = client.complete(prompt, request_purpose="recommend_jump_cards")
    try:
        return parse_jump_cards(raw, catalog=catalog, max_cards=max_cards)
    except Exception:  # noqa: BLE001 - recommendation is best-effort
        return []
