"""Controlled bridge from an Expert tool call to the existing native runtime."""

from __future__ import annotations

from typing import Any, Callable

from hailiang_skills.runtime_bridge.expert_models import SkillObservation


class NativeSkillExecutor:
    """Validate the handoff boundary before Hailiang executes a Runtime Skill.

    The current native executor remains MSAgentRuntimeAdapter.  This small
    adapter deliberately exposes only a summary/facts/form contract, never a
    filesystem path, reference content, shell or arbitrary tool surface.
    """

    def __init__(self, runtime_registry, execute: Callable[..., Any] | None = None) -> None:
        self.runtime_registry = runtime_registry
        self._execute = execute

    def observe(self, skill_id: str, task: str, handoff_context: dict[str, Any] | None = None) -> SkillObservation:
        bundle = self.runtime_registry.get(skill_id)
        if bundle is None:
            raise ValueError(f"Skill 不可用: {skill_id}")
        if self._execute is None:
            return SkillObservation(
                skill_id=skill_id,
                summary=f"已授权并安排 {skill_id} 处理当前问题。",
                handoff_summary=_safe_handoff(handoff_context),
                trace=({"skill_id": skill_id, "task_chars": len(task or "")},),
            )
        result = self._execute(skill_id=skill_id, task=task, handoff_context=handoff_context or {})
        if isinstance(result, SkillObservation):
            return result
        return SkillObservation(skill_id=skill_id, summary=str(result or ""), handoff_summary=_safe_handoff(handoff_context))


def _safe_handoff(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    facts = value.get("facts") if isinstance(value, dict) else None
    if isinstance(facts, dict):
        return f"已携带 {len(facts)} 项有效事实。"
    return "已携带受控交接上下文。"
