"""Orchestrator tail: unify outbound reply (single passthrough or multi merge)."""

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


def _successful_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["status"] == "completed" and row["text"]]


def build_tail_prompt(
    *,
    tail_rules: str,
    expert_results: Any,
    user_question: str = "",
    collaboration_rules: str = "",
) -> list[ChatMessage]:
    rows = _expert_rows(expert_results)
    successful = _successful_rows(rows)
    single = len(successful) == 1

    if single:
        guidance = [
            "本轮只有一位专家成功作答。",
            "只做底线判断后直接输出该专家原文：不要总结、不要改写、不要补充结论、不要加「专家说」类前缀。",
            "最终正文必须以顾问/专家口吻直接回答用户。",
            "禁止出现上下文预算、压缩失败、专家任务失败、工具错误等系统元叙述。",
            "去掉专家原文开头的自我介绍（如\"家长您好，我是xxx\"\"您好，我是xxx师\"等），直接从业务回答正文开始。",
            "若原文含明显内部错误话术或元叙述，可删掉这些句子后原样保留其余正文；否则逐字输出。",
        ]
        user_line = "请按编排尾规则输出给用户的最终正文（单专家：底线后直出原文，无系统元叙述）。"
    else:
        guidance = [
            "本轮有多位专家成功作答：把各专家原文合并成一段给用户的回复。",
            "只合并专家已经说过的内容，不要新增结论，不要再派专家。",
            "有冲突或缺口时用业务语言说明。某位专家失败时，按其余结果作答，不要解释系统失败原因。",
            "去掉各专家原文开头的自我介绍（如\"家长您好，我是xxx\"\"您好，我是xxx师\"等），直接从业务内容开始合并。",
            "最终正文必须以顾问/专家口吻直接回答；禁止提及预算、压缩、任务失败等内部状态。",
        ]
        user_line = "请合并以上专家回答，直接输出一段用户可见正文（专家口吻，无系统元叙述）。"

    system_parts = [
        str(tail_rules or "").strip(),
        str(collaboration_rules or "").strip(),
        *guidance,
        "用户本轮问题：",
        str(user_question or "").strip() or "（未提供）",
        "专家作答：",
        json.dumps(rows, ensure_ascii=False, indent=2),
        "直接输出用户可见正文，不要输出 JSON。",
    ]
    system = "\n\n".join(part for part in system_parts if part)
    return [
        ChatMessage(role="system", content=system),
        ChatMessage(role="user", content=user_line),
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
    """Fallback when no LLM / call_tail is provided.

    Single success → raw text (same as passthrough). Multi → labeled concat.
    """
    rows = _expert_rows(expert_results)
    successful = _successful_rows(rows)
    if len(successful) == 1:
        return successful[0]["text"]

    parts: list[str] = []
    failed: list[str] = []
    for row in rows:
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
