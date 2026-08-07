from __future__ import annotations

import importlib.util

from agent_skill_runtime_core.models import MSAgentRuntimeProbe


def probe_ms_agent_runtime() -> MSAgentRuntimeProbe:
    if importlib.util.find_spec("ms_agent") is None:
        return MSAgentRuntimeProbe(status="missing", error="ms_agent package is not installed")
    try:
        from ms_agent.skill.auto_skills import SkillAnalyzer
        from ms_agent.skill.container import ExecutionInput, SkillContainer
        from ms_agent.skill.loader import SkillLoader
        from ms_agent.skill.schema import SkillContext

        return MSAgentRuntimeProbe(
            status="available",
            imports={
                "SkillLoader": SkillLoader,
                "SkillAnalyzer": SkillAnalyzer,
                "SkillContainer": SkillContainer,
                "ExecutionInput": ExecutionInput,
                "SkillContext": SkillContext,
            },
        )
    except Exception as exc:  # pragma: no cover - environment-specific import failures
        return MSAgentRuntimeProbe(status="degraded", error=f"{type(exc).__name__}: {exc}")
