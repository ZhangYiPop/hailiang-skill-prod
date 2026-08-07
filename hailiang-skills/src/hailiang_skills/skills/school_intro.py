from __future__ import annotations

from hailiang_skills.core.logging import make_event
from hailiang_skills.skills.asset_support import build_asset_support
from hailiang_skills.skills.assets import load_json
from hailiang_skills.skills.base import BaseSkill, SkillResult
from hailiang_skills.skills.common import (
    build_skill_prompt_record,
    llm_compose_reply,
    llm_polish_structured_reply,
    resolve_summary_style,
)


class SchoolIntroSkill(BaseSkill):
    skill_name = "school_intro"

    def __init__(self, llm_client=None) -> None:
        self.llm_client = llm_client

    def run(self, user_input: str, context) -> SkillResult:
        planner_state = context.skill_states.get("planner", {})
        school_catalog = load_json("assets/generated/school_intro/schools.json", [])
        focus_school_names = context.known_facts.get_value("focus_school_names", []) or []

        targets = [item for item in school_catalog if item.get("school_name") in focus_school_names]
        if not targets:
            for item in school_catalog:
                school_name = item.get("school_name", "")
                if school_name and school_name in user_input:
                    targets.append(item)
        if not targets and school_catalog:
            targets = school_catalog[:1]

        structured_result = {
            "targets": targets[:5],
            "planner_state": planner_state,
            "known_facts": {
                key: context.known_facts.get_value(key)
                for key in ["student_province", "subject_group", "score_total"]
            },
            "asset_support": build_asset_support(
                self.skill_name,
                targets=targets[:5],
            ),
        }
        draft_reply = llm_compose_reply(
            self.llm_client,
            self.skill_name,
            user_input,
            context,
            planner_state,
            structured_result,
        )
        self._last_prompt_info = build_skill_prompt_record(
            self.skill_name,
            "school_intro_response",
            user_input,
            context,
            planner_state,
            structured_result,
            llm_response=draft_reply,
        )

        if not draft_reply:
            if not targets:
                draft_reply = "当前还没有命中可介绍的学校名称，你可以直接说学校名，我来帮你查。"
            else:
                lines = []
                for item in targets[:3]:
                    lines.append(f"学校：{item.get('school_name')}")
                    if item.get("school_intro"):
                        lines.append(f"简介：{item.get('school_intro')}")
                    else:
                        lines.append("简介：当前资产暂无详细介绍。")
                    if item.get("school_url"):
                        lines.append(f"链接：{item.get('school_url')}")
                draft_reply = "\n".join(lines)
        elif planner_state.get("should_ask_question") and planner_state.get("question_hint"):
            draft_reply = f"{draft_reply}\n\n{planner_state['question_hint']}"

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
                "matched_school_names": [item.get("school_name") for item in targets if item.get("school_name")],
                "matched_count": len(targets),
                "asset_support": structured_result["asset_support"],
            },
            events=[
                make_event(
                    "school_intro_asset_match",
                    {
                        "stage": "school_intro",
                        "assets_used": [
                            "assets/generated/school_intro/schools.json",
                        ],
                        "focus_school_names": focus_school_names,
                        "matched_targets": [
                            {
                                "school_name": item.get("school_name"),
                                "has_intro": bool(item.get("school_intro")),
                                "has_link": bool(item.get("school_url")),
                            }
                            for item in targets
                        ],
                        "asset_support": structured_result["asset_support"],
                        "polish_applied": bool(polished_reply),
                        "polish_style": polish_style,
                    },
                ),
                make_event(
                    "school_intro_reply",
                    {
                        "matched_count": len(targets),
                        "matched_school_names": [
                            item.get("school_name") for item in targets if item.get("school_name")
                        ],
                    },
                )
            ],
        )
