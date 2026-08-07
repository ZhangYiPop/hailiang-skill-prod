from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hailiang_skills.skills.base import BaseSkill


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {}

    def register(self, skill: BaseSkill) -> None:
        self._skills[skill.skill_name] = skill

    def get(self, skill_name: str) -> BaseSkill:
        if skill_name not in self._skills:
            raise KeyError(f"Skill not registered: {skill_name}")
        return self._skills[skill_name]

    def names(self) -> list[str]:
        return sorted(self._skills.keys())
