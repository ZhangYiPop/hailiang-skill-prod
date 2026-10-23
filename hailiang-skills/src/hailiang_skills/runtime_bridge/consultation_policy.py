"""Team consultation limits shared by the runtime loader and the workbench.

This module stays outside the workbench package so expert-team loading does not
import the workbench service.

``max_parallel`` is a capacity ceiling (1..max_subtasks). Task ordering comes
from ConsultationPlan.depends_on, not from this policy. Legacy
``allow_parallel`` is still accepted when reading old snapshots:
true → max_parallel=max_subtasks, false → 1.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


APPLICABLE_STAGES = ("小学", "初中", "高中")
MAX_SUBTASKS = 8
MAX_DEPTH = 3


class OrchestrationConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ConsultationPolicy:
    """Team-level consultation limits. The orchestrator reads them and does not rewrite them."""

    max_subtasks: int = 3
    max_parallel: int = 3
    max_depth: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_applicable_stages(raw: Any, *, strict: bool = True) -> tuple[str, ...]:
    if raw is None or raw == "":
        return ()
    if not isinstance(raw, list):
        if strict:
            raise OrchestrationConfigError("适用学段必须是数组")
        return ()
    stages: list[str] = []
    for item in raw:
        stage = str(item or "").strip()
        if not stage:
            continue
        if stage not in APPLICABLE_STAGES:
            if strict:
                raise OrchestrationConfigError(f"适用学段只能是 {'、'.join(APPLICABLE_STAGES)}")
            continue
        if stage not in stages:
            stages.append(stage)
    return tuple(stages)


def _bounded_int(raw: dict[str, Any], key: str, low: int, high: int, errors: list[str]) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{key} 必须是整数")
        return low
    if not low <= value <= high:
        errors.append(f"{key} 必须在 {low}-{high} 之间")
        return low
    return value


def _resolve_max_parallel(raw: dict[str, Any], max_subtasks: int, errors: list[str]) -> int:
    if "max_parallel" in raw:
        return _bounded_int(raw, "max_parallel", 1, max_subtasks, errors)
    if "allow_parallel" in raw:
        value = raw.get("allow_parallel")
        if not isinstance(value, bool):
            errors.append("allow_parallel 必须是布尔值")
            return 1
        return max_subtasks if value else 1
    return max_subtasks


def parse_consultation_policy(raw: Any, *, strict: bool = True) -> ConsultationPolicy:
    if raw is None:
        return ConsultationPolicy()
    if not isinstance(raw, dict):
        if strict:
            raise OrchestrationConfigError("consultation_policy 必须是对象")
        return ConsultationPolicy()
    errors: list[str] = []
    has_parallel = "max_parallel" in raw or "allow_parallel" in raw
    missing = [key for key in ("max_subtasks", "max_depth") if key not in raw]
    if not has_parallel:
        missing.append("max_parallel")
    if missing:
        errors.append("consultation_policy 必须包含 " + "、".join(missing))
    max_subtasks = _bounded_int(raw, "max_subtasks", 1, MAX_SUBTASKS, errors) if "max_subtasks" in raw else 3
    max_depth = _bounded_int(raw, "max_depth", 1, MAX_DEPTH, errors) if "max_depth" in raw else 1
    max_parallel = _resolve_max_parallel(raw, max_subtasks, errors) if has_parallel or not missing else max_subtasks
    if "max_parallel" in raw and isinstance(raw.get("max_parallel"), int) and not isinstance(raw.get("max_parallel"), bool):
        if raw["max_parallel"] > max_subtasks:
            errors.append("max_parallel 不能大于 max_subtasks")
    if errors:
        if strict:
            raise OrchestrationConfigError("；".join(errors))
        return ConsultationPolicy()
    return ConsultationPolicy(
        max_subtasks=max_subtasks,
        max_parallel=max_parallel,
        max_depth=max_depth,
    )


def resolve_member_key(value: str, locks: list[dict[str, Any]]) -> str:
    target = str(value or "").strip()
    if not target:
        return ""
    for lock in locks:
        if not isinstance(lock, dict):
            continue
        if target in {str(lock.get("object_id") or ""), str(lock.get("object_key") or "")}:
            return str(lock.get("object_key") or "")
    return target
