"""Materialize ConsultationPlan into DAG-schedulable expert task specs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hailiang_skills.runtime_bridge.consultation_plan import ConsultationPlan
from hailiang_skills.runtime_bridge.consultation_policy import ConsultationPolicy


@dataclass(slots=True)
class ExpertTaskSpec:
    task_id: str
    expert_id: str
    question: str
    depends_on: list[str] = field(default_factory=list)
    priority: int = 1
    status: str = "pending"

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "expert_id": self.expert_id,
            "question": self.question,
            "depends_on": list(self.depends_on),
            "priority": self.priority,
            "status": self.status,
        }


def resolve_max_parallel(policy: ConsultationPolicy | dict[str, Any] | None) -> int:
    """Read max_parallel; compat-map legacy allow_parallel."""
    if policy is None:
        return 3
    if isinstance(policy, dict):
        max_subtasks = max(1, int(policy.get("max_subtasks") or 3))
        if "max_parallel" in policy and policy.get("max_parallel") is not None:
            try:
                return max(1, min(max_subtasks, int(policy["max_parallel"])))
            except (TypeError, ValueError):
                return 1
        if "allow_parallel" in policy:
            return max_subtasks if bool(policy.get("allow_parallel")) else 1
        return max_subtasks
    max_subtasks = max(1, int(getattr(policy, "max_subtasks", 3) or 3))
    if hasattr(policy, "max_parallel"):
        try:
            return max(1, min(max_subtasks, int(getattr(policy, "max_parallel"))))
        except (TypeError, ValueError):
            return 1
    allow = bool(getattr(policy, "allow_parallel", True))
    return max_subtasks if allow else 1


def materialize_expert_tasks(
    plan: ConsultationPlan | dict[str, Any],
    policy: ConsultationPolicy | dict[str, Any] | None = None,
) -> list[ExpertTaskSpec]:
    """Turn a consult plan into task specs with depends_on for DAG scheduling."""
    if isinstance(plan, dict):
        action = str(plan.get("action") or "").strip()
        subtasks = plan.get("subtasks") or []
    else:
        action = plan.action
        subtasks = plan.subtasks
    if action != "consult":
        return []

    max_subtasks = 3
    if isinstance(policy, dict):
        try:
            max_subtasks = max(1, int(policy.get("max_subtasks") or 3))
        except (TypeError, ValueError):
            max_subtasks = 3
    elif policy is not None:
        max_subtasks = max(1, int(getattr(policy, "max_subtasks", 3) or 3))

    tasks: list[ExpertTaskSpec] = []
    for index, item in enumerate(subtasks):
        if index >= max_subtasks:
            break
        if isinstance(item, dict):
            task_id = str(item.get("subtask_id") or f"st_{index + 1}").strip()
            expert_id = str(item.get("target_expert_id") or item.get("expert_id") or "").strip()
            question = str(item.get("question") or "").strip()
            depends_on = [str(dep).strip() for dep in (item.get("depends_on") or []) if str(dep).strip()]
            priority = int(item.get("priority") or index + 1)
        else:
            task_id = str(getattr(item, "subtask_id", "") or f"st_{index + 1}").strip()
            expert_id = str(getattr(item, "target_expert_id", "") or "").strip()
            question = str(getattr(item, "question", "") or "").strip()
            depends_on = [str(dep).strip() for dep in (getattr(item, "depends_on", []) or []) if str(dep).strip()]
            priority = int(getattr(item, "priority", index + 1) or index + 1)
        if not task_id or not expert_id or not question:
            continue
        tasks.append(
            ExpertTaskSpec(
                task_id=task_id,
                expert_id=expert_id,
                question=question,
                depends_on=depends_on,
                priority=priority,
                status="pending",
            )
        )
    return tasks


def ready_tasks(
    tasks: list[ExpertTaskSpec] | list[dict[str, Any]],
    *,
    completed: set[str] | frozenset[str] | list[str] | None = None,
    running: set[str] | frozenset[str] | list[str] | None = None,
    max_parallel: int | None = None,
    policy: ConsultationPolicy | dict[str, Any] | None = None,
) -> list[ExpertTaskSpec]:
    """Return pending tasks whose depends_on are satisfied, capped by capacity."""
    completed_ids = {str(item) for item in (completed or [])}
    running_ids = {str(item) for item in (running or [])}
    capacity = resolve_max_parallel(policy) if max_parallel is None else max(1, int(max_parallel))
    slots = max(0, capacity - len(running_ids))

    normalized: list[ExpertTaskSpec] = []
    for item in tasks:
        if isinstance(item, ExpertTaskSpec):
            normalized.append(item)
            continue
        normalized.append(
            ExpertTaskSpec(
                task_id=str(item.get("task_id") or ""),
                expert_id=str(item.get("expert_id") or ""),
                question=str(item.get("question") or ""),
                depends_on=[str(dep) for dep in (item.get("depends_on") or []) if str(dep)],
                priority=int(item.get("priority") or 1),
                status=str(item.get("status") or "pending"),
            )
        )

    candidates: list[ExpertTaskSpec] = []
    for task in normalized:
        if task.status not in {"pending", "ready"}:
            continue
        if task.task_id in completed_ids or task.task_id in running_ids:
            continue
        deps = set(task.depends_on)
        if deps - completed_ids:
            continue
        candidates.append(task)

    candidates.sort(key=lambda item: (item.priority, item.task_id))
    selected = candidates[:slots]
    for task in selected:
        task.status = "ready"
    return selected


def mark_complete(
    tasks: list[ExpertTaskSpec] | list[dict[str, Any]],
    task_id: str,
    *,
    success: bool = True,
) -> ExpertTaskSpec | None:
    """Mark a task completed/failed for unit-testable DAG progression."""
    target = str(task_id or "").strip()
    if not target:
        return None
    for index, item in enumerate(tasks):
        current_id = item.task_id if isinstance(item, ExpertTaskSpec) else str(item.get("task_id") or "")
        if current_id != target:
            continue
        status = "completed" if success else "failed"
        if isinstance(item, ExpertTaskSpec):
            item.status = status
            return item
        item["status"] = status
        return ExpertTaskSpec(
            task_id=current_id,
            expert_id=str(item.get("expert_id") or ""),
            question=str(item.get("question") or ""),
            depends_on=[str(dep) for dep in (item.get("depends_on") or []) if str(dep)],
            priority=int(item.get("priority") or 1),
            status=status,
        )
    return None
