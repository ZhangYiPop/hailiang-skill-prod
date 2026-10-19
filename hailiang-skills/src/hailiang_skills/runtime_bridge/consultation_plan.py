"""ConsultationPlan contract for the platform orchestrator head.

The model is produced by the head LLM and validated by platform code before any
expert tasks are created. Validation covers structure, member membership,
school-stage fit, capacity limits, and depends_on DAG integrity.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hailiang_skills.runtime_bridge.consultation_policy import ConsultationPolicy


PlanAction = Literal["consult", "clarify", "reject_out_of_scope"]


class ConsultationPlanError(ValueError):
    """Structured validation failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code or "PLAN_INVALID").strip() or "PLAN_INVALID"
        self.message = str(message or "").strip() or self.code
        super().__init__(f"{self.code}: {self.message}")


class Subtask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subtask_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    target_expert_id: str = Field(min_length=1)
    priority: int = 1
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("subtask_id", "question", "target_expert_id", mode="before")
    @classmethod
    def _strip_required(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("字段不能为空")
        return text

    @field_validator("depends_on", mode="before")
    @classmethod
    def _normalize_depends(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ConsultationPlanError("PLAN_INVALID_DEPENDS_ON", "depends_on 必须是数组")
        items: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in items:
                items.append(text)
        return items

    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_priority(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("priority 必须是整数")
        return value


class ConsultationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: PlanAction
    clarification_question: str = ""
    subtasks: list[Subtask] = Field(default_factory=list)
    reason: str = ""

    @field_validator("clarification_question", "reason", mode="before")
    @classmethod
    def _strip_optional(cls, value: Any) -> str:
        return str(value or "").strip()


def _assert_action_shape(plan: ConsultationPlan) -> None:
    if plan.action == "consult":
        if not plan.subtasks:
            raise ConsultationPlanError("CONSULT_REQUIRES_SUBTASKS", "consult 至少需要一个子任务")
        if plan.clarification_question:
            raise ConsultationPlanError("CONSULT_NO_CLARIFY", "consult 时 clarification_question 必须为空")
        return
    if plan.subtasks:
        raise ConsultationPlanError("CLARIFY_NO_SUBTASKS", f"{plan.action} 不得带 subtasks")
    if not plan.clarification_question:
        raise ConsultationPlanError(
            "CLARIFY_REQUIRES_QUESTION",
            f"{plan.action} 需要 clarification_question",
        )


def _format_pydantic_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(item) for item in error.get("loc", ()))
        msg = str(error.get("msg") or "校验失败")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "；".join(parts) or "ConsultationPlan 结构非法"


def _build_plan(payload: dict[str, Any]) -> ConsultationPlan:
    try:
        plan = ConsultationPlan.model_validate(payload)
    except ConsultationPlanError:
        raise
    except ValidationError as exc:
        raise ConsultationPlanError("INVALID_PLAN", _format_pydantic_error(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ConsultationPlanError("INVALID_PLAN", str(exc)) from exc
    _assert_action_shape(plan)
    return plan


def _extract_json_object(raw: str | dict[str, Any] | ConsultationPlan) -> dict[str, Any]:
    if isinstance(raw, ConsultationPlan):
        return raw.model_dump()
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        raise ConsultationPlanError("EMPTY_PLAN", "编排首输出为空")
    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConsultationPlanError("INVALID_JSON", f"无法解析 ConsultationPlan JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConsultationPlanError("INVALID_JSON", "ConsultationPlan 必须是对象")
    return payload


def _policy_max_subtasks(policy: ConsultationPolicy | dict[str, Any] | None | int) -> int:
    if isinstance(policy, int) and not isinstance(policy, bool):
        return max(1, policy)
    if policy is None:
        return 3
    if isinstance(policy, dict):
        try:
            return max(1, int(policy.get("max_subtasks") or 3))
        except (TypeError, ValueError):
            return 3
    return max(1, int(getattr(policy, "max_subtasks", 3) or 3))


def _member_ids(members: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(members, dict):
        members = members.values()
    for item in members or []:
        if isinstance(item, str):
            value = item.strip()
            if value:
                ids.add(value)
            continue
        if isinstance(item, dict):
            value = str(item.get("expert_id") or item.get("object_key") or item.get("member_id") or "").strip()
            if value:
                ids.add(value)
            continue
        value = str(
            getattr(item, "expert_id", None)
            or getattr(item, "object_key", None)
            or getattr(item, "member_id", None)
            or ""
        ).strip()
        if value:
            ids.add(value)
    return ids


def _member_stages(members: Any) -> dict[str, tuple[str, ...]]:
    mapping: dict[str, tuple[str, ...]] = {}
    if isinstance(members, dict):
        members = members.values()
    for item in members or []:
        if not isinstance(item, dict) and not hasattr(item, "applicable_stages"):
            continue
        if isinstance(item, dict):
            expert_id = str(item.get("expert_id") or item.get("object_key") or "").strip()
            stages = item.get("applicable_stages") or ()
        else:
            expert_id = str(getattr(item, "expert_id", "") or getattr(item, "object_key", "") or "").strip()
            stages = getattr(item, "applicable_stages", ()) or ()
        if not expert_id:
            continue
        mapping[expert_id] = tuple(str(stage).strip() for stage in stages if str(stage).strip())
    return mapping


def _assert_dag(subtasks: list[Subtask]) -> None:
    ids = {item.subtask_id for item in subtasks}
    if len(ids) != len(subtasks):
        raise ConsultationPlanError("DUPLICATE_SUBTASK_ID", "subtask_id 本轮内不得重复")
    graph = {item.subtask_id: list(item.depends_on) for item in subtasks}
    for subtask_id, deps in graph.items():
        for dep in deps:
            if dep == subtask_id:
                raise ConsultationPlanError("DEPENDS_ON_SELF", f"{subtask_id} 不能依赖自身")
            if dep not in ids:
                raise ConsultationPlanError("UNKNOWN_DEPENDENCY", f"{subtask_id} 依赖未知子任务 {dep}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ConsultationPlanError("DEPENDENCY_CYCLE", "depends_on 存在环")
        visiting.add(node)
        for dep in graph.get(node, ()):
            walk(dep)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        walk(node)


def validate_consultation_plan(
    plan: ConsultationPlan | dict[str, Any] | str,
    *,
    members: Any = None,
    member_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    policy: ConsultationPolicy | dict[str, Any] | None = None,
    school_stage: str = "",
    max_subtasks: int | None = None,
) -> ConsultationPlan:
    if not isinstance(plan, ConsultationPlan):
        plan = _build_plan(_extract_json_object(plan))
    else:
        _assert_action_shape(plan)

    if plan.action != "consult":
        return plan

    allowed = {str(item).strip() for item in (member_ids or ()) if str(item).strip()} | _member_ids(members)
    if not allowed:
        raise ConsultationPlanError("NO_MEMBERS", "当前专家团没有可分发成员")
    limit = max_subtasks if max_subtasks is not None else _policy_max_subtasks(policy)
    if len(plan.subtasks) > limit:
        raise ConsultationPlanError("MAX_SUBTASKS", f"子任务数超过 max_subtasks={limit}")
    stages = _member_stages(members)
    stage = str(school_stage or "").strip()
    for subtask in plan.subtasks:
        if subtask.target_expert_id not in allowed:
            raise ConsultationPlanError(
                "UNKNOWN_EXPERT",
                f"target_expert_id 不在本轮成员中: {subtask.target_expert_id}",
            )
        allowed_stages = stages.get(subtask.target_expert_id) or ()
        if stage and allowed_stages and stage not in allowed_stages:
            raise ConsultationPlanError(
                "STAGE_MISMATCH",
                f"专家 {subtask.target_expert_id} 不适用学段 {stage}",
            )
    _assert_dag(plan.subtasks)
    return plan


def parse_consultation_plan(
    raw: str | dict[str, Any] | ConsultationPlan,
    *,
    members: Any = None,
    member_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    policy: ConsultationPolicy | dict[str, Any] | None = None,
    school_stage: str = "",
    max_subtasks: int | None = None,
) -> ConsultationPlan:
    plan = _build_plan(_extract_json_object(raw))
    # Structure-only parse when callers omit membership context (rare).
    if members is None and member_ids is None and policy is None and max_subtasks is None and not school_stage:
        return plan
    return validate_consultation_plan(
        plan,
        members=members,
        member_ids=member_ids,
        policy=policy,
        school_stage=school_stage,
        max_subtasks=max_subtasks,
    )


def parse_and_validate_consultation_plan(
    raw: str | dict[str, Any] | ConsultationPlan,
    *,
    members: Any = None,
    member_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    policy: ConsultationPolicy | dict[str, Any] | None = None,
    school_stage: str = "",
    max_subtasks: int = 3,
) -> ConsultationPlan:
    return parse_consultation_plan(
        raw,
        members=members,
        member_ids=member_ids,
        policy=policy or {"max_subtasks": max_subtasks},
        school_stage=school_stage,
        max_subtasks=max_subtasks,
    )


def fallback_consult_plan(
    *,
    fallback_expert_id: str,
    question: str,
    reason: str = "consultation_plan_validation_failed",
) -> ConsultationPlan:
    expert_id = str(fallback_expert_id or "").strip()
    if not expert_id:
        raise ConsultationPlanError("MISSING_FALLBACK", "缺少兜底专家")
    return ConsultationPlan(
        action="consult",
        clarification_question="",
        subtasks=[
            Subtask(
                subtask_id="st_1",
                question=str(question or "请根据用户本轮问题继续协助。").strip()
                or "请根据用户本轮问题继续协助。",
                target_expert_id=expert_id,
                priority=1,
                depends_on=[],
            )
        ],
        reason=reason,
    )


def clarify_plan(*, question: str, reason: str = "consultation_plan_validation_failed") -> ConsultationPlan:
    text = str(question or "").strip() or "还需要一点信息才能继续，请补充你想解决的问题。"
    return ConsultationPlan(
        action="clarify",
        clarification_question=text,
        subtasks=[],
        reason=reason,
    )
