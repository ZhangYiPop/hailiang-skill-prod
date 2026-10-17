"""Orchestrator head: soul + head_rules → ConsultationPlan (no tools)."""

from __future__ import annotations

import json
from typing import Any, Protocol

from hailiang_skills.runtime_bridge.consultation_plan import (
    ConsultationPlan,
    ConsultationPlanError,
    Subtask,
    clarify_plan,
    fallback_consult_plan,
    parse_consultation_plan,
)
from hailiang_skills.runtime_bridge.consultation_policy import ConsultationPolicy
from hailiang_skills.skill_runtime.models import ChatMessage


class HeadLLMClient(Protocol):
    def complete(self, messages: list[ChatMessage], **kwargs: Any) -> str: ...


def _policy_dict(policy: ConsultationPolicy | dict[str, Any] | None) -> dict[str, Any]:
    if policy is None:
        return ConsultationPolicy().as_dict()
    if isinstance(policy, dict):
        return dict(policy)
    as_dict = getattr(policy, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    return {
        "max_subtasks": int(getattr(policy, "max_subtasks", 3) or 3),
        "max_parallel": int(
            getattr(policy, "max_parallel", None)
            or (
                int(getattr(policy, "max_subtasks", 3) or 3)
                if bool(getattr(policy, "allow_parallel", True))
                else 1
            )
        ),
        "max_depth": int(getattr(policy, "max_depth", 1) or 1),
    }


def _member_rows(members: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(members, dict):
        members = members.values()
    for item in members or []:
        if isinstance(item, str):
            rows.append({"expert_id": item, "mention_name": item, "routing_brief": "", "applicable_stages": []})
            continue
        if isinstance(item, dict):
            rows.append(
                {
                    "expert_id": str(item.get("expert_id") or item.get("object_key") or "").strip(),
                    "mention_name": str(item.get("mention_name") or item.get("name") or "").strip(),
                    "routing_brief": str(item.get("routing_brief") or item.get("responsibility") or "").strip(),
                    "applicable_stages": list(item.get("applicable_stages") or []),
                }
            )
            continue
        rows.append(
            {
                "expert_id": str(getattr(item, "expert_id", "") or getattr(item, "object_key", "") or "").strip(),
                "mention_name": str(getattr(item, "mention_name", "") or getattr(item, "name", "") or "").strip(),
                "routing_brief": str(getattr(item, "routing_brief", "") or "").strip(),
                "applicable_stages": list(getattr(item, "applicable_stages", ()) or ()),
            }
        )
    return [row for row in rows if row.get("expert_id")]


def _profile_dict(profile: Any) -> dict[str, Any]:
    if profile is None:
        return {}
    if isinstance(profile, dict):
        return dict(profile)
    if hasattr(profile, "items"):
        try:
            return dict(profile)
        except Exception:  # noqa: BLE001
            pass
    return {
        key: getattr(profile, key)
        for key in ("user_role", "school_stage", "grade", "student_province", "score_total", "subject_group")
        if hasattr(profile, key)
    }


def _as_chat_messages(messages: Any) -> list[ChatMessage]:
    result: list[ChatMessage] = []
    for item in messages or []:
        if isinstance(item, ChatMessage):
            result.append(item)
            continue
        if isinstance(item, dict):
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "")
            if role in {"user", "assistant", "system"} and content.strip():
                result.append(ChatMessage(role=role, content=content))
    return result


def build_orchestrator_head_prompt(
    soul: str,
    head_rules: str,
    members: Any,
    policy: ConsultationPolicy | dict[str, Any] | None,
    profile: Any,
    messages: Any,
) -> list[ChatMessage]:
    """Compose the tool-free orchestrator-head prompt messages."""
    member_rows = _member_rows(members)
    policy_payload = _policy_dict(policy)
    profile_payload = _profile_dict(profile)
    system_parts = [
        str(soul or "").strip(),
        str(head_rules or "").strip(),
        "本步不调用任何工具。只输出一个合法的 ConsultationPlan JSON 对象。",
        "当前专家团已发布成员（只能从中选人）：",
        json.dumps(member_rows, ensure_ascii=False, indent=2),
        "会诊策略（只读）：",
        json.dumps(policy_payload, ensure_ascii=False, indent=2),
        "已回填画像：",
        json.dumps(profile_payload, ensure_ascii=False, indent=2),
        "输出格式：",
        json.dumps(
            {
                "action": "consult|clarify|reject_out_of_scope",
                "clarification_question": "",
                "subtasks": [
                    {
                        "subtask_id": "st_1",
                        "question": "交给专家的子问题",
                        "target_expert_id": "expert_id",
                        "priority": 1,
                        "depends_on": [],
                    }
                ],
                "reason": "内部选人依据",
            },
            ensure_ascii=False,
            indent=2,
        ),
    ]
    system = "\n\n".join(part for part in system_parts if part)
    prompt = [ChatMessage(role="system", content=system)]
    for message in _as_chat_messages(messages):
        if message.role == "system":
            continue
        prompt.append(message)
    if not any(item.role == "user" for item in prompt):
        prompt.append(ChatMessage(role="user", content="请根据以上上下文产出 ConsultationPlan。"))
    return prompt


def _latest_user_question(messages: Any) -> str:
    for message in reversed(_as_chat_messages(messages)):
        if message.role == "user" and message.content.strip():
            return message.content.strip()
    return "请根据用户本轮问题继续协助。"


def _school_stage(profile: Any) -> str:
    payload = _profile_dict(profile)
    return str(payload.get("school_stage") or "").strip()


def run_orchestrator_head(
    client: HeadLLMClient,
    *,
    soul: str = "",
    head_rules: str = "",
    members: Any,
    policy: ConsultationPolicy | dict[str, Any] | None = None,
    profile: Any = None,
    messages: Any = None,
    fallback_expert_id: str = "",
    retry_on_invalid: int = 1,
) -> ConsultationPlan:
    """Call the model (no tools), parse ConsultationPlan, retry once, then fallback/clarify."""
    prompt = build_orchestrator_head_prompt(soul, head_rules, members, policy, profile, messages)
    stage = _school_stage(profile)
    attempts = max(0, int(retry_on_invalid)) + 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        request_messages = list(prompt)
        if attempt > 0 and last_error is not None:
            request_messages = [
                *prompt,
                ChatMessage(
                    role="user",
                    content=(
                        "上一轮输出不是合法 ConsultationPlan。"
                        f"错误：{last_error}。"
                        "请只输出修正后的 JSON 对象，不要解释。"
                    ),
                ),
            ]
        raw = client.complete(request_messages, request_purpose="orchestrator_head")
        try:
            return parse_consultation_plan(
                raw,
                members=members,
                policy=policy,
                school_stage=stage,
            )
        except ConsultationPlanError as exc:
            last_error = exc
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = ConsultationPlanError("INVALID_PLAN", str(exc))
            continue

    member_ids = {row["expert_id"] for row in _member_rows(members)}
    fallback = str(fallback_expert_id or "").strip()
    question = _latest_user_question(messages)
    reason = f"consultation_plan_validation_failed:{getattr(last_error, 'code', last_error)}"
    if fallback and fallback in member_ids:
        return fallback_consult_plan(
            fallback_expert_id=fallback,
            question=question,
            reason=reason,
        )
    return clarify_plan(
        question="还需要一点信息才能帮你匹配专家，请用一句话说明你想解决的问题。",
        reason=reason,
    )


__all__ = [
    "HeadLLMClient",
    "Subtask",
    "build_orchestrator_head_prompt",
    "run_orchestrator_head",
]
