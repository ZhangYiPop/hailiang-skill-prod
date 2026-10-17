"""Orchestrator tail: merge multi-expert answers with tail_rules."""

from __future__ import annotations

import json
from typing import Any, Protocol

from hailiang_skills.skill_runtime.models import ChatMessage


class TailLLMClient(Protocol):
    def complete(self, messages: list[ChatMessage], **kwargs: Any) -> str: ...


def _expert_rows(expert_results: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in expert_results or []:
        if not isinstance(item, dict):
            continue
        expert_id = str(item.get("expert_id") or item.get("source_expert_id") or "").strip()
        text = str(item.get("text") or item.get("content") or "").strip()
        status = str(item.get("status") or ("completed" if text else "failed")).strip() or "failed"
        mention = str(item.get("mention_name") or item.get("name") or expert_id).strip()
        rows.append(
            {
                "expert_id": expert_id,
                "mention_name": mention,
                "status": status,
                "text": text,
            }
        )
    return rows


def build_tail_prompt(
    *,
    tail_rules: str,
    expert_results: Any,
    user_question: str = "",
    collaboration_rules: str = "",
) -> list[ChatMessage]:
    rows = _expert_rows(expert_results)
    system_parts = [
        str(tail_rules or "").strip(),
        str(collaboration_rules or "").strip(),
        "只合并专家已经说过的内容，不要新增结论，不要再派专家。",
        "有冲突或缺口时明确说明。某位专家失败时，按其余结果作答并说明缺了谁。",
        "用户本轮问题：",
        str(user_question or "").strip() or "（未提供）",
        "专家作答：",
        json.dumps(rows, ensure_ascii=False, indent=2),
        "请直接输出合并后的一段回复正文，不要输出 JSON。",
    ]
    system = "\n\n".join(part for part in system_parts if part)
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content="请合并以上专家回答。"),
    ]


def run_tail_merge(
    client: TailLLMClient,
    *,
    tail_rules: str = "",
    expert_results: Any,
    user_question: str = "",
    collaboration_rules: str = "",
) -> str:
    prompt = build_tail_prompt(
        tail_rules=tail_rules,
        expert_results=expert_results,
        user_question=user_question,
        collaboration_rules=collaboration_rules,
    )
    return str(client.complete(prompt, request_purpose="orchestrator_tail") or "").strip()


def simple_concat_merge(expert_results: Any) -> str:
    """Default merge when no LLM / call_tail is provided."""
    parts: list[str] = []
    failed: list[str] = []
    for row in _expert_rows(expert_results):
        name = row["mention_name"] or row["expert_id"] or "专家"
        if row["status"] != "completed" or not row["text"]:
            failed.append(name)
            continue
        parts.append(f"【{name}】\n{row['text']}")
    text = "\n\n".join(parts).strip()
    if failed:
        missing = "、".join(failed)
        note = f"（未收到：{missing}）"
        text = f"{text}\n\n{note}".strip() if text else note
    return text
