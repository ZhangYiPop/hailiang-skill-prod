from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

TraceStatus = Literal["success", "warning", "error", "skipped"]
RuntimeStatus = Literal["available", "degraded", "missing"]


@dataclass(slots=True)
class MSAgentRuntimeProbe:
    status: RuntimeStatus
    error: str | None = None
    imports: dict[str, Any] | None = None

    @property
    def available(self) -> bool:
        return self.status == "available"


@dataclass(slots=True, frozen=True)
class CoreTraceStep:
    name: str
    status: TraceStatus
    detail: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LoadedSkillContext:
    skill_key: str
    skill_path: Path
    skill_md: str
    tools_yaml: str
    references: list[dict[str, str]]
    scripts: list[dict[str, str]]
    resources: list[dict[str, str]]
    plan: dict[str, Any]
    execution_outputs: list[dict[str, Any]]
    raw_trace: dict[str, Any]
    # Optional user-facing response produced alongside the MS-Agent plan.
    # Hailiang consumes this only when no later tool result is required.
    combined_response: str = ""
