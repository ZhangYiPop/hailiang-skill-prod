from __future__ import annotations

from hailiang_skills.core.logging import make_event
from hailiang_skills.skills.base import BaseSkill, SkillResult


class LoggingSkill(BaseSkill):
    skill_name = "logging_skill"

    def run(self, user_input: str, context) -> SkillResult:
        return SkillResult(
            assistant_message="",
            events=[
                make_event(
                    "logging_snapshot",
                    {
                        "message_count": len(context.messages),
                        "candidate_count": len(context.candidate_paths),
                    },
                )
            ],
        )
