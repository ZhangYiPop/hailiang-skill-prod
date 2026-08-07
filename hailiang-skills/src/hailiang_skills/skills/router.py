from __future__ import annotations

from hailiang_skills.core.logging import make_event
from hailiang_skills.core.routing_config import (
    get_routing_keywords,
    get_scenario_keyword_match,
)
from hailiang_skills.llm.service import build_context_snapshot, safe_complete_json
from hailiang_skills.skills.common import build_prompt_record
from hailiang_skills.skills.base import BaseSkill, SkillResult
from hailiang_skills.skills.assets import load_json
from hailiang_skills.skills.common import (
    extract_focus_school_names,
    extract_focus_targets,
    extract_score,
    extract_subject_group,
)

DEFAULT_ACTIVE_SCENARIO = "admission_simulation"


def _resolve_target_scenario_for_skill(
    skill_name: str,
    current_scenario: str,
) -> str:
    if skill_name == "admission":
        return "admission_simulation"
    if skill_name in {"convergence", "path_drilldown", "terminate_or_recommend"}:
        return "multi_path_planning"
    if skill_name == "school_intro":
        return current_scenario or DEFAULT_ACTIVE_SCENARIO
    return current_scenario or DEFAULT_ACTIVE_SCENARIO


def _has_explicit_terminate_intent(text: str) -> bool:
    routing_keywords = get_routing_keywords()
    terminate_kw = routing_keywords.get("terminate", {})
    keywords = terminate_kw.get("keywords", ["直接推荐", "先这样", "别问了", "不补充", "先别问"])
    return any(keyword in text for keyword in keywords)


def _looks_like_followup_fact_answer(text: str, extracted_facts: dict) -> bool:
    if any(
        extracted_facts.get(key) not in (None, "", [], {})
        for key in ["score_total", "subject_group", "student_province", "student_region"]
    ):
        return True
    normalized_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]
    if normalized_lines and all(("：" in line or ":" in line) for line in normalized_lines):
        return True
    return any(
        keyword in text
        for keyword in [
            "我是",
            "预算",
            "民族",
            "学考",
            "合格",
            "选科",
            "预估",
            "估计",
            "大概",
            "历史",
            "物理",
            "政",
            "地",
            "化",
            "生",
            "户籍",
            "建档立卡",
            "少数民族",
        ]
    )


def _should_continue_previous_skill(context, text: str, extracted_facts: dict) -> bool:
    previous_plan = context.skill_states.get("planner", {})
    previous_target = previous_plan.get("target_skill") or context.skill_states.get("router", {}).get(
        "target_skill"
    )
    if previous_target not in {"convergence", "admission", "path_drilldown", "school_intro"}:
        return False

    if any(keyword in text for keyword in ["还有什么路", "多元路径", "展开讲", "详细讲", "先这样", "直接推荐"]):
        return False

    previous_missing_facts = previous_plan.get("missing_facts") or []
    previous_response_mode = previous_plan.get("response_mode")
    if previous_response_mode in {"recommend", "ask_followup"} or previous_missing_facts:
        return _looks_like_followup_fact_answer(text, extracted_facts)

    return previous_target in {"convergence", "school_intro"} and _looks_like_followup_fact_answer(text, extracted_facts)


def _should_apply_continuity_fallback(
    target: str,
    confidence: float,
    text: str,
    extracted_facts: dict,
    explicit_path_interest_route: tuple[str, str, str] | None,
) -> bool:
    if explicit_path_interest_route:
        return False
    if _has_explicit_terminate_intent(text):
        return False
    if target in {"path_drilldown", "convergence", "school_intro", "terminate_or_recommend"}:
        return False
    if confidence >= 0.85:
        return False
    return _looks_like_followup_fact_answer(text, extracted_facts)


def _fallback_resume_skill(context, extracted_facts: dict) -> str:
    last_non_terminal = context.interaction_state.get("last_non_terminal_skill")
    if last_non_terminal in {"admission", "convergence", "path_drilldown", "school_intro"}:
        return last_non_terminal
    if any(
        extracted_facts.get(key) not in (None, "", [], {})
        for key in ["score_total", "subject_group", "student_province"]
    ):
        return "admission"
    return "convergence"


def _should_jump_from_admission_to_convergence(context, text: str) -> bool:
    admission_state = context.skill_states.get("admission", {})
    if not admission_state.get("recommended_path_ids"):
        return False

    routing_keywords = get_routing_keywords()
    school_keywords = ["学校", "院校", "专业"]
    if any(keyword in text for keyword in school_keywords):
        return False

    convergence_keywords = routing_keywords.get("convergence", {}).get("keywords", [])
    path_keywords = convergence_keywords + ["推荐路径", "具体路径", "这些路径", "还有什么路径", "路径可以推荐", "想了解路径"]
    return any(keyword in text for keyword in path_keywords)


def _is_school_recommendation_query(text: str) -> bool:
    school_keywords = ["什么学校", "哪些学校", "学校可以上", "能上什么学校", "有哪些学校可以上"]
    return any(keyword in text for keyword in school_keywords)


def _is_school_intro_query(text: str) -> bool:
    school_intro_keywords = ["学校介绍", "学校信息", "学校怎么样", "这个学校怎么样", "介绍一下学校"]
    return any(keyword in text for keyword in school_intro_keywords)


def _match_keyword_fallback(text: str) -> tuple[str, str, str, float] | None:
    routing_keywords = get_routing_keywords()

    categories = [
        ("terminate", ["不补充"]),
        ("path_drilldown", ["详细讲", "展开讲", "适合吗", "风险", "强基", "少年班"]),
        ("convergence", ["路径", "升学路", "还有什么路", "多元"]),
        ("admission", ["高考", "分数", "物理", "历史", "浙江", "江苏", "广东"]),
    ]

    for category, extra_keywords in categories:
        kw_section = routing_keywords.get(category, {})
        all_keywords = kw_section.get("keywords", []) + extra_keywords
        for kw in all_keywords:
            if kw in text:
                return (
                    category,
                    kw_section.get("target_skill", category),
                    f"命中{category}类关键词",
                    kw_section.get("confidence", 0.6),
                )
    return None


def _resolve_path_interest_route(
    focus_path_ids: list[str],
    focus_primary_categories: list[str],
) -> tuple[str, str, str] | None:
    unique_targets = []
    seen = set()
    for item in [*focus_path_ids, *focus_primary_categories]:
        if item and item not in seen:
            seen.add(item)
            unique_targets.append(item)

    if not unique_targets:
        return None
    return (
        "path_drilldown",
        "drill_down",
        "用户明确点名了一条或多条路径，进入 path_drilldown 做路径展开与缺失信息判断",
    )


class RouterSkill(BaseSkill):
    skill_name = "router"

    def __init__(self, llm_client=None) -> None:
        self.llm_client = llm_client

    def run(self, user_input: str, context) -> SkillResult:
        text = user_input.strip()
        current_scenario = context.interaction_state.get("current_scenario", DEFAULT_ACTIVE_SCENARIO)
        path_catalog = load_json("assets/generated/multiroute/path_catalog.json", [])
        school_catalog = load_json("assets/generated/school_intro/schools.json", [])
        llm_result = safe_complete_json(
            self.llm_client,
            "router",
            {
                "user_input": user_input,
                "context": build_context_snapshot(context),
            },
        )
        self._last_prompt_info = build_prompt_record(
            self.skill_name,
            "router",
            "router",
            {"user_input": user_input, "context": build_context_snapshot(context)},
            llm_response=llm_result,
        )

        if llm_result:
            target = llm_result.get("target_skill", "chat")
            intent = llm_result.get("intent", "chat")
            extracted_facts = llm_result.get("extracted_facts", {})
            confidence = llm_result.get("confidence", 0.0)
            reason = llm_result.get("reason", "LLM 路由")
            target_scenario = llm_result.get("target_scenario", current_scenario)
        else:
            fallback_match = _match_keyword_fallback(text)
            if fallback_match:
                category, target, reason, confidence = fallback_match
                intent = category
                extracted_facts = {}
            else:
                target = "chat"
                intent = "chat"
                extracted_facts = {}
                confidence = 0.51
                reason = "默认闲聊兜底"
            target_scenario = current_scenario

        requested_scenario = None
        scenario_match = get_scenario_keyword_match(text)
        if scenario_match:
            requested_scenario = scenario_match["scenario_id"]
            if scenario_match.get("status") == "active":
                target_scenario = requested_scenario
                reason = f"{reason}；检测到场景意图，准备切换到 {requested_scenario}"
            else:
                reason = (
                    f"{reason}；检测到用户想进入 {requested_scenario}，"
                    "但该场景仍处于规划中，暂保持当前场景继续处理"
                )

        if not extracted_facts.get("score_total"):
            score = extract_score(text)
            if score is not None:
                extracted_facts["score_total"] = score
        if not extracted_facts.get("subject_group"):
            subject_group = extract_subject_group(text)
            if subject_group:
                extracted_facts["subject_group"] = subject_group
        fallback_focus_path_ids, fallback_focus_categories = extract_focus_targets(text, path_catalog)
        if not extracted_facts.get("focus_path_ids") and fallback_focus_path_ids:
            extracted_facts["focus_path_ids"] = fallback_focus_path_ids
        if not extracted_facts.get("focus_primary_categories") and fallback_focus_categories:
            extracted_facts["focus_primary_categories"] = fallback_focus_categories
        fallback_focus_schools = extract_focus_school_names(text, school_catalog)
        if not extracted_facts.get("focus_school_names") and fallback_focus_schools:
            extracted_facts["focus_school_names"] = fallback_focus_schools

        if extracted_facts.get("focus_school_names"):
            if _is_school_recommendation_query(text):
                target = "admission"
                intent = "admission"
                confidence = max(confidence, 0.95)
                reason = "用户在问可上哪些学校，属于 admission 的院校推荐问题"
            elif _is_school_intro_query(text) or any(keyword in text for keyword in ["学校", "院校"]):
                target = "school_intro"
                intent = "school_intro"
                confidence = max(confidence, 0.96)
                reason = "用户明确点名学校并询问学校信息，进入 school_intro"

        explicit_path_interest_route = None
        if target != "school_intro" and (
            fallback_focus_path_ids or fallback_focus_categories
        ):
            explicit_path_interest_route = _resolve_path_interest_route(
                fallback_focus_path_ids,
                fallback_focus_categories,
            )
        if explicit_path_interest_route:
            target, intent, reason = explicit_path_interest_route
            confidence = max(confidence, 0.94)

        if target == "admission" and _should_jump_from_admission_to_convergence(context, text):
            target = "convergence"
            intent = "convergence"
            confidence = max(confidence, 0.92)
            reason = "Admission 已完成学校层次与推荐路径匹配，本轮用户转而追问具体路径，切换到 convergence 做路径信息收敛"

        resume_from_terminate = False
        resume_target_skill = None
        if (
            target == "terminate_or_recommend"
            and not _has_explicit_terminate_intent(text)
            and _looks_like_followup_fact_answer(text, extracted_facts)
        ):
            resume_target_skill = _fallback_resume_skill(context, extracted_facts)
            target = resume_target_skill
            intent = (
                "convergence"
                if resume_target_skill == "convergence"
                else "admission"
                if resume_target_skill == "admission"
                else "school_intro"
                if resume_target_skill == "school_intro"
                else "drill_down"
            )
            confidence = max(confidence, 0.9)
            reason = f"本轮检测到新的关键事实补充，退出 terminate，恢复到 {resume_target_skill}"
            resume_from_terminate = True

        previous_target = context.skill_states.get("planner", {}).get("target_skill")
        if (
            previous_target
            and target != previous_target
            and _should_apply_continuity_fallback(
                target,
                confidence,
                text,
                extracted_facts,
                explicit_path_interest_route,
            )
            and _should_continue_previous_skill(
                context, text, extracted_facts
            )
        ):
            target = previous_target
            intent = (
                "convergence"
                if previous_target == "convergence"
                else "admission"
                if previous_target == "admission"
                else "school_intro"
                if previous_target == "school_intro"
                else "drill_down"
            )
            confidence = max(confidence, 0.9)
            reason = f"识别为上一轮 {previous_target} 的补充回答，沿用当前 skill 连续处理"
            resume_from_terminate = previous_target != "terminate_or_recommend"
            resume_target_skill = previous_target

        if target in {"admission", "convergence", "path_drilldown", "school_intro", "terminate_or_recommend"}:
            target_scenario = _resolve_target_scenario_for_skill(target, current_scenario)

        return SkillResult(
            assistant_message="",
            next_skill=target,
            state_patch={
                "target_skill": target,
                "target_scenario": target_scenario,
                "requested_scenario": requested_scenario,
                "intent": intent,
                "confidence": confidence,
                "reason": reason,
                "extracted_facts": extracted_facts,
                "resume_from_terminate": resume_from_terminate,
                "resume_target_skill": resume_target_skill,
            },
            events=[
                make_event(
                    "router_decision",
                    {
                        "intent": intent,
                        "target_skill": target,
                        "target_scenario": target_scenario,
                        "requested_scenario": requested_scenario,
                        "confidence": confidence,
                        "reason": reason,
                    },
                )
            ],
        )
