from __future__ import annotations

from hailiang_skills.core.logging import make_event
from hailiang_skills.skills.asset_support import build_asset_support
from hailiang_skills.skills.base import BaseSkill, SkillResult
from hailiang_skills.skills.common import (
    build_skill_prompt_record,
    llm_compose_reply,
    llm_polish_structured_reply,
    resolve_summary_style,
)


class TerminateOrRecommendSkill(BaseSkill):
    skill_name = "terminate_or_recommend"

    def __init__(self, llm_client=None) -> None:
        self.llm_client = llm_client

    def run(self, user_input: str, context) -> SkillResult:
        planner_state = context.skill_states.get("planner", {})
        structured_result = {
            "candidate_paths": context.candidate_paths[:5],
            "known_facts": {
                key: value.model_dump() for key, value in context.known_facts.facts.items()
            },
            "planner_state": planner_state,
            "asset_support": build_asset_support(
                self.skill_name,
                candidate_paths=context.candidate_paths[:5],
            ),
        }
        draft_reply = llm_compose_reply(
            self.llm_client,
            self.skill_name,
            user_input,
            context,
            planner_state,
            structured_result,
            context_candidate_paths=context.candidate_paths[:5],
            candidate_paths_source="historical_convergence",
        )
        self._last_prompt_info = build_skill_prompt_record(
            self.skill_name,
            "terminate_response",
            user_input,
            context,
            planner_state,
            structured_result,
            context_candidate_paths=context.candidate_paths[:5],
            llm_response=draft_reply,
        )

        if not draft_reply and context.candidate_paths:
            lines = [
                f"- {item.get('path_id')} {item.get('primary_category')} ({item.get('risk_level')})"
                for item in context.candidate_paths[:3]
            ]
            draft_reply = "基于当前信息，先给你一个收敛后的建议：\n" + "\n".join(lines)
        elif not draft_reply:
            draft_reply = "我可以直接给建议，但当前候选路径还不够完整，建议至少补充省份和分数。"

        polish_style = resolve_summary_style(self.skill_name)
        polished_reply = llm_polish_structured_reply(
            self.llm_client,
            skill_name=self.skill_name,
            user_input=user_input,
            draft_reply=draft_reply,
            context=context,
            planner_state=planner_state,
            structured_result=structured_result,
            style=polish_style,
        )
        reply = polished_reply or draft_reply

        return SkillResult(
            assistant_message=reply,
            state_patch={
                "asset_support": structured_result["asset_support"],
            },
            events=[
                make_event(
                    "terminate_reply",
                    {
                        "candidate_count": len(context.candidate_paths),
                        "asset_support": structured_result["asset_support"],
                        "polish_applied": bool(polished_reply),
                        "polish_style": polish_style,
                    },
                )
            ],
        )
