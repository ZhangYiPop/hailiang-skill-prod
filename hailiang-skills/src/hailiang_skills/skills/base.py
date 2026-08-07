from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from hailiang_skills.schemas.facts import FactRecord


@dataclass
class SkillResult:
    assistant_message: str
    next_skill: str | None = None
    updated_facts: dict[str, FactRecord] = field(default_factory=dict)
    state_patch: dict[str, Any] = field(default_factory=dict)
    candidate_paths: list[dict[str, Any]] | None = None
    suggested_paths: list[str] = field(default_factory=list)
    risk_alerts: list[str] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)


class BaseSkill(ABC):
    skill_name: str
    _last_prompt_info: dict | None = None

    @abstractmethod
    def run(self, user_input: str, context: Any) -> SkillResult:
        raise NotImplementedError

    def get_prompt_for_llm(self) -> dict | None:
        return getattr(self, "_last_prompt_info", None)
