from __future__ import annotations

from hailiang_skills.core.logging import make_event
from hailiang_skills.skills.common import build_skill_prompt_record, llm_compose_reply
from hailiang_skills.skills.base import BaseSkill, SkillResult


class ChatSkill(BaseSkill):
    skill_name = "chat"

    def __init__(self, llm_client=None) -> None:
        self.llm_client = llm_client

    def run(self, user_input: str, context) -> SkillResult:
        planner_state = context.skill_states.get("planner", {})
        reply = llm_compose_reply(
            self.llm_client,
            self.skill_name,
            user_input,
            context,
            planner_state,
            {"mode": "chat_transition"},
        )
        self._last_prompt_info = build_skill_prompt_record(
            self.skill_name,
            "chat_response",
            user_input,
            context,
            planner_state,
            {"mode": "chat_transition"},
            llm_response=reply,
        )
        if not reply:
            reply = (
                "我可以和你自然聊，也可以随时切到模拟升学或多元路径规划。"
                "如果你愿意，可以直接告诉我省份、分数，或者说想看看哪些升学路径。"
            )
        return SkillResult(
            assistant_message=reply,
            events=[make_event("chat_reply", {"user_input": user_input})],
        )
