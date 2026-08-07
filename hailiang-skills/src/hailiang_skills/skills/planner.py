from __future__ import annotations

from hailiang_skills.core.facts_config import get_enabled_fact_keys, get_fact_label
from hailiang_skills.core.fact_prompt_builder import (
    build_missing_fact_form_block,
    sort_fact_keys_for_form,
)
from hailiang_skills.core.logging import make_event
from hailiang_skills.core.scenario_engine import ScenarioEngine
from hailiang_skills.llm.service import build_context_snapshot, safe_complete_json
from hailiang_skills.skills.convergence import suggest_high_value_missing_facts
from hailiang_skills.skills.common import (
    build_prompt_record,
    has_alternative_exploration_intent,
)
from hailiang_skills.skills.base import BaseSkill, SkillResult


def _is_missing(value) -> bool:
    return value in (None, "", [], {})


def _build_high_value_missing_facts(context, router_state: dict) -> list[str]:
    target_skill = router_state.get("target_skill", "chat")
    current_scenario = context.interaction_state.get("current_scenario", "admission_simulation")
    current_phase = context.interaction_state.get("current_phase", "collect_info")
    if current_scenario != "multi_path_planning":
        return []
    if current_phase not in {"collect_info", "match_paths"}:
        return []
    if target_skill != "convergence":
        return []
    facts = {
        key: value.value for key, value in context.known_facts.facts.items()
    }
    hard_required = {"student_province", "subject_group", "score_total"}
    return sort_fact_keys_for_form([
        fact_key
        for fact_key in suggest_high_value_missing_facts(facts, limit=4)
        if fact_key not in hard_required and _is_missing(context.known_facts.get_value(fact_key))
    ])


def _phase_aware_fallback(context, router_state: dict) -> dict:
    scenario_engine = ScenarioEngine()
    scenario_id = (
        router_state.get("target_scenario")
        or context.interaction_state.get("current_scenario")
        or "admission_simulation"
    )
    phase_id = context.interaction_state.get("current_phase", "collect_info")
    scenario_meta = scenario_engine.get_scenario_meta(scenario_id)
    required_facts = scenario_meta.get("required_facts", [])
    missing_facts = [
        fact_key
        for fact_key in required_facts
        if _is_missing(context.known_facts.get_value(fact_key))
    ]

    target_skill = router_state.get("target_skill", "chat")
    excluded_path_ids = context.known_facts.get_value("excluded_path_ids", []) or []
    excluded_primary_categories = (
        context.known_facts.get_value("excluded_primary_categories", []) or []
    )
    focus_path_ids = context.known_facts.get_value("focus_path_ids", []) or []
    latest_user_message = ""
    if context.messages:
        latest_user_message = next(
            (item.get("content", "") for item in reversed(context.messages) if item.get("role") == "user"),
            "",
        )
    if (
        target_skill == "path_drilldown"
        and not focus_path_ids
        and (excluded_path_ids or excluded_primary_categories)
        and has_alternative_exploration_intent(latest_user_message)
    ):
        target_skill = "convergence"
        phase_id = "match_paths"
    if target_skill == "school_intro":
        phase_id = "school_lookup"
    elif target_skill == "path_drilldown":
        phase_id = "deep_drill"
    elif target_skill == "terminate_or_recommend":
        phase_id = "final_recommend"
    elif target_skill == "admission" and scenario_id == "admission_simulation" and not missing_facts:
        phase_id = "admission_analysis"
    elif target_skill == "convergence" and not missing_facts:
        phase_id = "match_paths"

    response_mode = "answer"
    should_ask_question = False
    question_hint = ""
    goal = f"执行 {target_skill} 并给出自然回复"
    focus_points = ["优先利用当前 facts", "避免重复追问"]

    if phase_id == "collect_info" and missing_facts:
        response_mode = "ask_followup"
        should_ask_question = True
        question_hint = f"优先补充：{get_fact_label(missing_facts[0])}"
        goal = "先补齐升学判断所需的关键信息"
        focus_points = ["只追问最关键的一个事实", "避免一次问太多"]
    elif phase_id == "admission_analysis":
        target_skill = "admission"
        goal = "基于已知 facts 做学校层次、可报学校与模拟升学判断"
        focus_points = ["优先输出学校层次判断", "说明仍缺哪些信息会影响学校推荐"]
    elif phase_id == "match_paths":
        target_skill = "convergence" if target_skill not in {"path_drilldown", "school_intro", "terminate_or_recommend"} else target_skill
        if excluded_path_ids or excluded_primary_categories:
            goal = "排除用户暂不考虑的路径后，收敛其他可选路径并说明筛选依据"
            focus_points = ["不要再展开被排除路径", "优先输出替代路径", "说明仍缺哪些信息会影响个性化判断"]
        else:
            goal = "基于已知 facts 做路径收敛和可行性判断"
            focus_points = ["优先输出可行路径", "说明仍缺哪些信息会影响判断"]
    elif phase_id == "deep_drill":
        target_skill = "path_drilldown"
        goal = "围绕用户点名的路径做深入解释和适配分析"
        focus_points = ["解释规则和门槛", "指出适配性和风险"]
    elif phase_id == "school_lookup":
        target_skill = "school_intro"
        goal = "围绕用户点名学校做信息介绍"
        focus_points = ["严格基于学校资产", "不混入路径结论"]
    elif phase_id == "final_recommend":
        target_skill = "terminate_or_recommend"
        goal = "给出阶段性总结或直接推荐"
        focus_points = ["总结当前已知结论", "标明结论依据和限制"]

    return {
        "target_skill": target_skill,
        "goal": goal,
        "response_mode": response_mode,
        "missing_facts": missing_facts,
        "focus_points": focus_points,
        "should_ask_question": should_ask_question,
        "question_hint": question_hint,
        "scenario_id": scenario_id,
        "phase_id": phase_id,
    }


class PlannerSkill(BaseSkill):
    skill_name = "planner"

    def __init__(self, llm_client=None) -> None:
        self.llm_client = llm_client

    def run(self, user_input: str, context) -> SkillResult:
        router_state = context.skill_states.get("router", {})
        _llm_payload = {
            "user_input": user_input,
            "router_state": router_state,
            "context": build_context_snapshot(context),
        }
        llm_result = safe_complete_json(self.llm_client, "planner", _llm_payload)
        self._last_prompt_info = build_prompt_record(
            self.skill_name,
            "planner",
            "planner",
            _llm_payload,
            llm_response=llm_result,
        )

        fallback = _phase_aware_fallback(context, router_state)
        plan = llm_result or fallback
        allowed_fact_keys = set(get_enabled_fact_keys())
        plan.setdefault("scenario_id", fallback["scenario_id"])
        plan.setdefault("phase_id", fallback["phase_id"])
        plan["missing_facts"] = [
            fact_key
            for fact_key in (plan.get("missing_facts") or [])
            if fact_key in allowed_fact_keys
        ]
        plan["missing_facts"] = sort_fact_keys_for_form(plan["missing_facts"])
        if not plan["missing_facts"]:
            high_value_missing_facts = _build_high_value_missing_facts(context, router_state)
            if high_value_missing_facts:
                plan["missing_facts"] = high_value_missing_facts
                plan["should_ask_question"] = True
                plan["question_hint"] = (
                    f"若想进一步收敛“部分条件满足”的路径，可优先补充："
                    f"{'、'.join(get_fact_label(fact_key) for fact_key in plan['missing_facts'][:3])}"
                )
                if plan.get("response_mode") == "answer":
                    plan["focus_points"] = [
                        *(plan.get("focus_points") or []),
                        "结合部分条件满足路径，引导用户补充最有价值的缺失事实",
                    ]
        if router_state.get("resume_from_terminate") and router_state.get("resume_target_skill"):
            plan = {
                **plan,
                "target_skill": router_state["resume_target_skill"],
                "goal": f"用户补充了新的关键事实，退出 terminate，恢复到 {router_state['resume_target_skill']} 继续分析",
                "response_mode": "answer",
                "should_ask_question": False,
                "question_hint": "",
            }
        target_skill = plan.get("target_skill") or fallback["target_skill"]

        known_score_total = context.known_facts.get_value("score_total")
        if known_score_total is not None:
            missing_facts = [fact for fact in (plan.get("missing_facts") or []) if fact != "score_recent_avg"]
            if missing_facts != (plan.get("missing_facts") or []):
                plan["missing_facts"] = missing_facts
                if not missing_facts and plan.get("should_ask_question"):
                    plan["should_ask_question"] = False
                    plan["question_hint"] = ""
        plan["missing_fact_form"] = build_missing_fact_form_block(
            plan.get("missing_facts") or []
        )

        return SkillResult(
            assistant_message="",
            next_skill=target_skill,
            state_patch=plan,
            events=[
                make_event(
                    "planner_decision",
                    {
                        "target_skill": target_skill,
                        "scenario_id": plan.get("scenario_id"),
                        "phase_id": plan.get("phase_id"),
                        "response_mode": plan.get("response_mode"),
                        "goal": plan.get("goal"),
                    },
                )
            ],
        )
