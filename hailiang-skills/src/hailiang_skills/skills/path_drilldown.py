from __future__ import annotations

from hailiang_skills.core.logging import make_event
from hailiang_skills.skills.asset_support import build_asset_support, format_fact_label
from hailiang_skills.skills.assets import load_json
from hailiang_skills.skills.base import BaseSkill, SkillResult
from hailiang_skills.skills.common import (
    build_prompt_record,
    find_catalog_path_matches,
    has_alternative_exploration_intent,
    llm_polish_structured_reply,
    normalize_subject_group,
    path_name_matches,
    resolve_summary_style,
)
from hailiang_skills.skills.convergence import (
    STATUS_LABELS,
    _build_next_step_plan,
    _build_text_blob,
    _extract_blocking_reasons,
    _extract_condition_slots,
    _build_effective_rule_context,
    _resolve_feasibility_status,
    build_path_timelines,
)


def _format_rule_lines(item: dict) -> list[str]:
    variants = item.get("rule_variants", []) or []
    if not variants:
        return ["- 规则说明：该维度的信息正在整理中，后续版本会提供详细的分析。"]
    lines = []
    for variant in variants[:2]:
        rule_text = (variant.get("rule_text_raw") or "").strip()
        if rule_text:
            lines.append(f"- 命中规则：{rule_text}")
    return lines or ["- 规则说明：该维度的信息正在整理中，后续版本会提供详细的分析。"]


def _format_asset_section(title: str, value: str | None) -> str:
    if value:
        return f"- {title}：{value}"
    return f"- {title}：该维度的信息正在整理中，后续版本会提供详细的分析。"


def _build_path_drilldown_reply(
    analyzed_targets: list[dict],
    planner_state: dict,
    facts: dict,
    followup_context: dict,
) -> str:
    if not analyzed_targets:
        return "路径详情资产尚未生成，暂时无法展开说明。"

    sections: list[str] = []
    score_source = facts.get("score_source")
    score_recent_avg = facts.get("score_recent_avg")
    score_total = facts.get("score_total")
    if score_recent_avg is not None:
        score_note = f"最近三次大考均值按 `{score_recent_avg}` 分参与当前判断。"
    elif score_total is not None and score_source == "estimated_total":
        score_note = f"当前先按你提供的 `{score_total}` 分参与判断；若后续能补充最近三次大考均值，结论会更精确。"
    elif score_total is not None:
        score_note = f"当前按你提供的 `{score_total}` 分参与判断。"
    else:
        score_note = ""

    if followup_context.get("same_targets_followup"):
        changed_fact_keys = followup_context.get("changed_fact_keys") or []
        header = "这轮继续围绕同一路径做增量判断。"
        if changed_fact_keys:
            header = f"这轮继续围绕同一路径做增量判断，新增事实：{', '.join(changed_fact_keys)}。"
        sections.append(header)
    else:
        sections.append("下面按当前命中的路径资产，给你做结构化说明。")
    if score_note:
        sections.append(score_note)

    for item in analyzed_targets:
        part: list[str] = [f"### {item.get('primary_category')}"]
        part.append(f"- 当前判断：{item.get('feasibility_label')}")
        part.append(_format_asset_section("路径介绍", item.get("description")))
        part.append(_format_asset_section("路径特色", item.get("features")))
        part.extend(_format_rule_lines(item))

        reasons = item.get("reasons", []) or []
        if reasons and reasons[0]:
            part.append(f"- 资产依据：{reasons[0]}")
        else:
            part.append("- 资产依据：该维度的信息正在整理中，后续版本会提供详细的分析。")

        risk_hint = item.get("risk_hint")
        if risk_hint:
            part.append(f"- 风险提示：{risk_hint}")

        if item.get("missing_slots"):
            part.append(
                f"- 还缺信息：{', '.join(format_fact_label(slot) for slot in item.get('missing_slots', []))}"
            )
        if item.get("blocking_reasons"):
            part.append(f"- 当前不满足：{', '.join(item.get('blocking_reasons', []))}")

        timeline = item.get("action_timeline", []) or []
        if item.get("feasibility_status") != "infeasible" and timeline:
            part.append("- 关键时间线：")
            for step in timeline[:3]:
                part.append(
                    f"  - {' / '.join(filter(None, [step.get('stage_label'), step.get('time_label'), step.get('action')]))}"
                )
        elif item.get("feasibility_status") != "infeasible":
            part.append("- 关键时间线：该维度的信息正在整理中，后续版本会提供详细的分析。")

        next_steps = item.get("next_step_plan", []) or []
        if next_steps:
            part.append("- 下一步建议：")
            for step in next_steps[:3]:
                part.append(f"  - {step}")

        sections.append("\n".join(part))

    question_hint = planner_state.get("question_hint")
    if planner_state.get("should_ask_question") and question_hint:
        sections.append(question_hint)

    return "\n\n".join(sections)


def _build_alternative_guard_reply(
    excluded_primary_categories: list[str],
    excluded_path_ids: list[str],
) -> str:
    excluded_targets = [*excluded_primary_categories, *excluded_path_ids]
    excluded_label = "、".join(item for item in excluded_targets if item) or "当前已提到的路径"
    return (
        f"你这轮更像是在排除“{excluded_label}”之后，想看看还有哪些可选路径，"
        "这属于路径收敛/替代方案探索，不适合继续把单一路径往下深挖。"
        "\n\n建议继续从多元路径里筛选更合适的方向，通常还需要结合省份、选科和当前分数来判断。"
    )


class PathDrillDownSkill(BaseSkill):
    skill_name = "path_drilldown"

    def __init__(self, llm_client=None) -> None:
        self.llm_client = llm_client

    def run(self, user_input: str, context) -> SkillResult:
        planner_state = context.skill_states.get("planner", {})
        previous_state = context.skill_states.get(self.skill_name, {})
        catalog = load_json("assets/generated/multiroute/path_catalog.json", [])
        reason_map = {
            item.get("path_id"): item
            for item in load_json("assets/generated/multiroute/path_reason_templates.json", [])
        }
        focus_path_ids = context.known_facts.get_value("focus_path_ids", []) or []
        focus_primary_categories = context.known_facts.get_value("focus_primary_categories", []) or []
        excluded_path_ids = context.known_facts.get_value("excluded_path_ids", []) or []
        excluded_primary_categories = (
            context.known_facts.get_value("excluded_primary_categories", []) or []
        )
        facts = {
            key: context.known_facts.get_value(key)
            for key in [
                "student_province",
                "student_region",
                "subject_group",
                "score_total",
                "score_recent_avg",
                "score_source",
                "score_band_tag",
                "budget_level",
                "family_type",
                "ethnicity",
                "hukou_years",
                "guardian_hukou_match",
                "school_status_years",
                "exam_qualification_status",
                "special_identity_tags",
                "career_orientation",
            ]
        }
        facts["subject_group"] = normalize_subject_group(facts.get("subject_group"))
        alternative_exploration = has_alternative_exploration_intent(user_input) and bool(
            excluded_path_ids or excluded_primary_categories
        )

        if alternative_exploration and not focus_path_ids:
            guard_reply = _build_alternative_guard_reply(
                excluded_primary_categories,
                excluded_path_ids,
            )
            return SkillResult(
                assistant_message=guard_reply,
                candidate_paths=[],
                state_patch={
                    "target_path_ids": [],
                    "target_path_names": [],
                    "facts_snapshot": facts,
                    "same_targets_followup": False,
                    "changed_fact_keys": [],
                    "candidate_count": 0,
                    "guard_blocked": True,
                    "guard_reason": "alternative_exploration_should_use_convergence",
                    "excluded_primary_categories": excluded_primary_categories,
                    "excluded_path_ids": excluded_path_ids,
                },
                events=[
                    make_event(
                        "path_drilldown_guard_blocked",
                        {
                            "reason": "alternative_exploration_should_use_convergence",
                            "excluded_primary_categories": excluded_primary_categories,
                            "excluded_path_ids": excluded_path_ids,
                            "user_input": user_input,
                        },
                    )
                ],
            )

        targets = []
        if focus_path_ids:
            targets.extend(item for item in catalog if item.get("path_id") in focus_path_ids)
            unresolved_focus_ids = [path_id for path_id in focus_path_ids if not any(item.get("path_id") == path_id for item in targets)]
            for unresolved_id in unresolved_focus_ids:
                if ":" in unresolved_id:
                    fallback_name = unresolved_id.split(":", 1)[1]
                    targets.extend(
                        find_catalog_path_matches(
                            fallback_name,
                            catalog,
                            student_province=facts.get("student_province"),
                            limit=3,
                        )
                    )
        if focus_primary_categories:
            targets.extend(
                item
                for item in catalog
                if any(
                    path_name_matches(category, item.get("primary_category"))
                    for category in focus_primary_categories
                )
            )
        if excluded_path_ids or excluded_primary_categories:
            excluded_path_id_set = set(excluded_path_ids)
            excluded_category_set = set(excluded_primary_categories)
            targets = [
                item
                for item in targets
                if item.get("path_id") not in excluded_path_id_set
                and item.get("primary_category") not in excluded_category_set
            ]
        if not targets:
            for item in catalog:
                if (
                    path_name_matches(user_input, item.get("primary_category", ""))
                    or item.get("path_id", "") in user_input
                ):
                    targets.append(item)
        if excluded_path_ids or excluded_primary_categories:
            excluded_path_id_set = set(excluded_path_ids)
            excluded_category_set = set(excluded_primary_categories)
            targets = [
                item
                for item in targets
                if item.get("path_id") not in excluded_path_id_set
                and item.get("primary_category") not in excluded_category_set
            ]
        unique_targets = []
        seen = set()
        for item in targets:
            key = (item.get("path_id"), item.get("primary_category"))
            if key in seen:
                continue
            seen.add(key)
            unique_targets.append(item)
        targets = unique_targets[:5]
        timelines_by_path = build_path_timelines([item.get("primary_category") for item in targets])

        analyzed_targets = []
        for target in targets:
            reason = reason_map.get(target.get("path_id"), {})
            text_blob = _build_text_blob(target, reason)
            effective_rule_context = _build_effective_rule_context(target, facts)
            missing_slots = _extract_condition_slots(text_blob, facts, target)
            blocking_reasons = _extract_blocking_reasons(text_blob, None, facts, target)
            feasibility_status = _resolve_feasibility_status(missing_slots, blocking_reasons)
            analyzed_targets.append(
                {
                    **target,
                    "rule_variants": effective_rule_context.get("rule_variants", []),
                    "required_fact_keys": effective_rule_context.get("required_fact_keys", []),
                    "geo_constraints": effective_rule_context.get("geo_constraints", {}),
                    "subject_constraints": effective_rule_context.get("subject_constraints", []),
                    "match_score": 0.82 if feasibility_status == "feasible" else 0.66 if feasibility_status == "partial" else 0.28,
                    "feasibility_status": feasibility_status,
                    "feasibility_label": STATUS_LABELS[feasibility_status],
                    "missing_slots": missing_slots,
                    "blocking_reasons": blocking_reasons,
                    "reasons": [
                        reason.get("match_reason") or target.get("description", "待补充")
                    ],
                    "risk_hint": reason.get("risk_hint", ""),
                    "action_timeline": timelines_by_path.get(target.get("primary_category"), []),
                    "next_step_plan": _build_next_step_plan(
                        target, missing_slots, feasibility_status, timelines_by_path
                    ),
                }
            )

        current_target_path_ids = [item.get("path_id") for item in analyzed_targets if item.get("path_id")]
        previous_target_path_ids = previous_state.get("target_path_ids", []) or []
        same_targets_followup = bool(current_target_path_ids) and current_target_path_ids == previous_target_path_ids
        previous_fact_snapshot = previous_state.get("facts_snapshot", {}) or {}
        changed_fact_keys = sorted(
            [
                key
                for key, value in facts.items()
                if value not in (None, "", [], {})
                and previous_fact_snapshot.get(key) != value
            ]
        )

        structured_result = {
            "targets": analyzed_targets,
            "planner_state": planner_state,
            "followup_context": {
                "same_targets_followup": same_targets_followup,
                "previous_target_path_ids": previous_target_path_ids,
                "changed_fact_keys": changed_fact_keys,
                "should_avoid_repeating_intro": same_targets_followup,
            },
            "asset_support": build_asset_support(
                self.skill_name,
                candidate_paths=analyzed_targets,
            ),
        }
        draft_reply = _build_path_drilldown_reply(
            analyzed_targets,
            planner_state,
            facts,
            structured_result["followup_context"],
        )
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
        self._last_prompt_info = build_prompt_record(
            self.skill_name,
            "path_drilldown_response",
            "structured_summary_polish",
            {
                "skill_name": self.skill_name,
                "user_input": user_input,
                "draft_reply": draft_reply,
                "style": polish_style,
                "structured_result": structured_result,
            },
            llm_response=polished_reply,
        )
        reply = polished_reply or draft_reply

        return SkillResult(
            assistant_message=reply,
            candidate_paths=analyzed_targets,
            suggested_paths=[
                item.get("primary_category")
                for item in analyzed_targets
                if item.get("primary_category") and item.get("feasibility_status") != "infeasible"
            ],
            state_patch={
                "target_path_ids": current_target_path_ids,
                "target_path_names": [
                    item.get("primary_category") for item in analyzed_targets if item.get("primary_category")
                ],
                "facts_snapshot": facts,
                "same_targets_followup": same_targets_followup,
                "changed_fact_keys": changed_fact_keys,
                "candidate_count": len(analyzed_targets),
                "asset_support": structured_result["asset_support"],
            },
            events=[
                make_event(
                    "path_drilldown_asset_match",
                    {
                        "stage": "path_drilldown",
                        "assets_used": [
                            "assets/generated/multiroute/path_catalog.json",
                            "assets/generated/multiroute/path_reason_templates.json",
                            "assets/generated/multiroute/action_timeline_templates.json",
                        ],
                        "same_targets_followup": same_targets_followup,
                        "changed_fact_keys": changed_fact_keys,
                        "score_source": facts.get("score_source"),
                        "score_recent_avg": facts.get("score_recent_avg"),
                        "score_total": facts.get("score_total"),
                        "matched_targets": [
                            {
                                "path_id": item.get("path_id"),
                                "primary_category": item.get("primary_category"),
                                "feasibility_status": item.get("feasibility_status"),
                                "missing_slots": item.get("missing_slots", []),
                                "blocking_reasons": item.get("blocking_reasons", []),
                                "required_fact_keys": item.get("required_fact_keys", []),
                                "timeline_step_count": len(item.get("action_timeline", [])),
                            }
                            for item in analyzed_targets
                        ],
                        "asset_support": structured_result["asset_support"],
                        "polish_applied": bool(polished_reply),
                        "polish_style": polish_style,
                    },
                ),
                make_event(
                    "path_drilldown_reply",
                    {
                        "target_path_ids": current_target_path_ids,
                        "same_targets_followup": same_targets_followup,
                        "changed_fact_keys": changed_fact_keys,
                        "candidate_count": len(analyzed_targets),
                    },
                )
            ],
        )
