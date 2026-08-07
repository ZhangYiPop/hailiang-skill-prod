from __future__ import annotations

import re

from hailiang_skills.core.logging import make_event
from hailiang_skills.core.fact_prompt_builder import sort_fact_keys_for_form
from hailiang_skills.llm.service import safe_complete_json
from hailiang_skills.skills.asset_support import (
    build_asset_support,
    format_fact_label,
    format_missing_slot_prompts,
)
from hailiang_skills.skills.assets import feature_flag_enabled, load_json
from hailiang_skills.skills.base import BaseSkill, SkillResult
from hailiang_skills.skills.common import (
    build_path_name_variants,
    build_prompt_record,
    llm_polish_structured_reply,
    normalize_budget_level,
    normalize_career_orientation_values,
    normalize_exam_qualification_status,
    normalize_province,
    resolve_summary_style,
)
from hailiang_skills.schemas.facts import is_explicit_unknown_value


ENABLE_SCORE_BAND_EXPOSURE_RULES = "HAILIANG_ENABLE_SCORE_BAND_EXPOSURE_RULES"


CONDITIONAL_SLOT_RULES = {
    "student_region": ["学生户籍地", "学生户籍所在地", "户籍所在地", "户籍地", "户籍"],
    "family_type": ["家庭类型", "建档立卡"],
    "ethnicity": ["民族", "少数民族", "非汉族"],
    "hukou_years": ["连续3年户籍"],
    "guardian_hukou_match": ["监护人户籍"],
    "school_status_years": ["学籍", "连续3年学籍"],
    "exam_qualification_status": ["学考", "学考是否合格"],
    "special_identity_tags": ["竞赛", "奖项", "国家集训队"],
    "career_orientation": ["职业兴趣"],
    "physical_requirements": ["身高", "身长", "高度"],
}

STATUS_LABELS = {
    "feasible": "可行",
    "partial": "部分条件满足",
    "infeasible": "当前不满足",
}


CAREER_ORIENTATION_PATTERN = r"职业兴趣[：:]\s*([^\n；;，,（）() ]+)"
BUDGET_HIGH_THRESHOLD_KEYWORDS = ("5万", "5w", "五万")


def _build_text_blob(item: dict, reason: dict) -> str:
    variant_blob = " ".join(
        variant.get("rule_text_raw", "") for variant in item.get("rule_variants", []) if variant.get("rule_text_raw")
    )
    return " ".join(
        [
            item.get("primary_category", ""),
            item.get("description", ""),
            item.get("target_users", ""),
            item.get("rule_text_raw", ""),
            variant_blob,
            reason.get("rule_text_raw", ""),
        ]
    )


def _required_career_orientations(item: dict, facts: dict) -> set[str]:
    effective_context = _build_effective_rule_context(item, facts)
    if "career_orientation" not in set(effective_context.get("required_fact_keys", []) or []):
        return set()
    rule_blob = effective_context.get("rule_blob", item.get("rule_text_raw", "")) or ""
    matched = re.findall(CAREER_ORIENTATION_PATTERN, rule_blob)
    return set(normalize_career_orientation_values(matched))


def _selected_career_orientations(facts: dict) -> set[str]:
    return set(normalize_career_orientation_values(facts.get("career_orientation")))


def _path_requires_high_budget(item: dict, facts: dict) -> bool:
    effective_context = _build_effective_rule_context(item, facts)
    if "budget_level" not in set(effective_context.get("required_fact_keys", []) or []):
        return False
    text_blob = " ".join(
        [
            effective_context.get("rule_blob", "") or "",
            item.get("target_users", "") or "",
            item.get("description", "") or "",
        ]
    )
    return any(keyword in text_blob.lower() for keyword in BUDGET_HIGH_THRESHOLD_KEYWORDS)


def _should_exclude_for_selected_facts(item: dict, facts: dict) -> bool:
    selected_orientations = _selected_career_orientations(facts)
    required_orientations = _required_career_orientations(item, facts)
    if selected_orientations and required_orientations and not (selected_orientations & required_orientations):
        return True

    budget_level = normalize_budget_level(facts.get("budget_level"))
    if budget_level == "<5万" and _path_requires_high_budget(item, facts):
        return True

    return False


def _resolve_applicable_rule_variants(item: dict, facts: dict) -> list[dict]:
    variants = item.get("rule_variants", []) or []
    if not variants:
        return []

    student_province = facts.get("student_province")
    subject_group = facts.get("subject_group")
    student_region = facts.get("student_region")
    normalized_student_province = normalize_province(student_province) if student_province else None

    current = variants
    if normalized_student_province:
        matched = [
            variant
            for variant in current
            if not (variant.get("geo_constraints", {}) or {}).get("provinces")
            or normalized_student_province
            in [
                normalize_province(item)
                for item in (variant.get("geo_constraints", {}) or {}).get("provinces", [])
            ]
        ]
        if matched:
            current = matched

    if subject_group:
        matched = [
            variant
            for variant in current
            if not variant.get("subject_constraints")
            or subject_group in (variant.get("subject_constraints") or [])
        ]
        if matched:
            current = matched

    if student_region:
        matched = [
            variant
            for variant in current
            if not (variant.get("geo_constraints", {}) or {}).get("regions")
            or student_region in (variant.get("geo_constraints", {}) or {}).get("regions", [])
        ]
        if matched:
            current = matched

    return current


def _build_effective_rule_context(item: dict, facts: dict) -> dict:
    variants = _resolve_applicable_rule_variants(item, facts)
    if not variants:
        return {
            "rule_variants": [],
            "rule_blob": item.get("rule_text_raw", ""),
            "required_fact_keys": item.get("required_fact_keys", []),
            "geo_constraints": item.get("geo_constraints", {}),
            "subject_constraints": item.get("subject_constraints", []),
        }

    rule_blob = " ".join(variant.get("rule_text_raw", "") for variant in variants if variant.get("rule_text_raw"))
    return {
        "rule_variants": variants,
        "rule_blob": rule_blob,
        "required_fact_keys": sorted(
            {
                fact_key
                for variant in variants
                for fact_key in (variant.get("required_fact_keys", []) or [])
            }
        ),
        "geo_constraints": {
            "provinces": sorted(
                {
                    province
                    for variant in variants
                    for province in ((variant.get("geo_constraints", {}) or {}).get("provinces", []))
                }
            ),
            "regions": sorted(
                {
                    region
                    for variant in variants
                    for region in ((variant.get("geo_constraints", {}) or {}).get("regions", []))
                }
            ),
        },
        "subject_constraints": sorted(
            {
                subject
                for variant in variants
                for subject in (variant.get("subject_constraints", []) or [])
            }
        ),
    }


def _extract_condition_slots(text_blob: str, facts: dict, item: dict | None = None) -> list[str]:
    missing_slots: list[str] = []
    effective_context = _build_effective_rule_context(item, facts) if item else {
        "required_fact_keys": [],
        "rule_blob": text_blob,
    }
    required_fact_keys = effective_context.get("required_fact_keys", [])
    for fact_key in required_fact_keys:
        if not facts.get(fact_key) or is_explicit_unknown_value(fact_key, facts.get(fact_key)):
            missing_slots.append(fact_key)
    rule_blob = effective_context.get("rule_blob", text_blob)
    for slot, keywords in CONDITIONAL_SLOT_RULES.items():
        if any(keyword in rule_blob for keyword in keywords) and (
            not facts.get(slot) or is_explicit_unknown_value(slot, facts.get(slot))
        ):
            missing_slots.append(slot)
    return sorted(set(missing_slots))


def _is_conditional_candidate(item: dict, text_blob: str, missing_slots: list[str]) -> bool:
    if item.get("sheet_group") in {"7.专项政策", "8.地区特色"} and missing_slots:
        return True
    return any(keyword in text_blob for keyword in ["专项", "定向", "保送", "军警", "招飞"]) and bool(
        missing_slots
    )


def _extract_blocking_reasons(
    text_blob: str, rule_row: dict | None, facts: dict, item: dict | None = None
) -> list[str]:
    reasons: list[str] = []
    student_province = facts.get("student_province")
    student_region = facts.get("student_region")
    subject_group = facts.get("subject_group")
    family_type = facts.get("family_type")
    ethnicity = facts.get("ethnicity")
    score_band_tag = facts.get("score_band_tag")
    budget_level = facts.get("budget_level")
    exam_qualification_status = facts.get("exam_qualification_status")

    effective_context = _build_effective_rule_context(item, facts) if item else {
        "geo_constraints": {},
        "subject_constraints": [],
        "rule_blob": text_blob,
    }
    geo_constraints = effective_context.get("geo_constraints", {})
    province_constraints = geo_constraints.get("provinces", [])
    normalized_student_province = normalize_province(student_province) if student_province else None
    normalized_province_constraints = [
        normalize_province(item) for item in province_constraints if item
    ]
    region_constraints = geo_constraints.get("regions", [])
    subject_constraints = effective_context.get("subject_constraints", [])
    rule_blob = effective_context.get("rule_blob", text_blob)

    if normalized_student_province and normalized_province_constraints:
        if normalized_student_province not in normalized_province_constraints:
            reasons.append("province_mismatch")

    if student_region and region_constraints:
        if student_region not in region_constraints:
            reasons.append("region_mismatch")

    if subject_group == "历史" and (
        ("首选科目：物理" in rule_blob and "首选科目：历史" not in rule_blob)
        or ("物理" in subject_constraints and "历史" not in subject_constraints)
    ):
        reasons.append("subject_mismatch")
    if subject_group == "物理" and (
        ("首选科目：历史" in rule_blob and "首选科目：物理" not in rule_blob)
        or ("历史" in subject_constraints and "物理" not in subject_constraints)
    ):
        reasons.append("subject_mismatch")

    if rule_row and score_band_tag and not _band_matches(rule_row, score_band_tag):
        reasons.append("score_band_mismatch")

    if ethnicity and ethnicity == "汉族" and any(keyword in rule_blob for keyword in ["非汉族", "少数民族"]):
        reasons.append("ethnicity_mismatch")

    if family_type and family_type != "原建档立卡户" and "建档立卡" in rule_blob:
        reasons.append("family_type_mismatch")

    if normalize_budget_level(budget_level) == "<5万" and any(keyword in rule_blob for keyword in ["中外合作", "海外", "学费"]):
        reasons.append("budget_mismatch")

    if (
        normalize_exam_qualification_status(exam_qualification_status) == "不合格"
        and any(keyword in rule_blob for keyword in ["学考", "学考是否合格"])
        and "≠否" in rule_blob
    ):
        reasons.append("exam_qualification_mismatch")

    return sorted(set(reasons))


def _resolve_feasibility_status(
    missing_slots: list[str], blocking_reasons: list[str]
) -> str:
    if blocking_reasons:
        return "infeasible"
    if missing_slots:
        return "partial"
    return "feasible"


def _normalize_score_band(score: int | None, province_line: dict | None) -> str | None:
    if score is None or not province_line:
        return None
    special = int(province_line.get("特控线/一本线") or 0)
    undergraduate = int(province_line.get("一段线/本科线") or 0)
    junior = int(province_line.get("二段线/专科线") or 0)
    if special and score >= special - 20:
        return "特控线上"
    if undergraduate and score >= undergraduate - 20:
        return "一段线/本科线上"
    if junior and score >= junior - 20:
        return "二段线/专科线上"
    return "二段线/专科线下"


def _find_province_line(province_score_lines: list[dict], province: str | None, subject_group: str | None) -> dict | None:
    if not province:
        return None
    normalized_province = normalize_province(province)
    normalized_subject = subject_group or None
    for row in province_score_lines:
        if normalize_province(row.get("省份", "")) != normalized_province:
            continue
        row_subject = row.get("选科") or None
        if row_subject and normalized_subject and row_subject != normalized_subject:
            continue
        if row_subject == normalized_subject:
            return row
    return next(
        (
            row
            for row in province_score_lines
            if normalize_province(row.get("省份", "")) == normalized_province and not row.get("选科")
        ),
        None,
    )


def build_path_timelines(path_names: list[str]) -> dict[str, list[dict]]:
    templates = load_json("assets/generated/multiroute/action_timeline_templates.json", [])
    target_names = {path_name for path_name in path_names if path_name}
    timelines: dict[str, list[dict]] = {path_name: [] for path_name in sorted(target_names)}
    target_variants = {
        path_name: set(build_path_name_variants(path_name))
        for path_name in target_names
    }
    for item in templates:
        stage_label = item.get("grade")
        time_label = item.get("time_label")
        actions = item.get("actions") or {}
        for action_name, action_text in actions.items():
            action_variants = set(build_path_name_variants(action_name))
            for path_name, variants in target_variants.items():
                if variants and action_variants and variants & action_variants:
                    timelines.setdefault(path_name, []).append(
                        {
                            "stage_label": stage_label or None,
                            "time_label": time_label,
                            "action": action_text,
                        }
                    )
    return timelines


def _build_next_step_plan(
    item: dict,
    missing_slots: list[str],
    feasibility_status: str,
    timelines_by_path: dict[str, list[dict]],
) -> list[str]:
    if feasibility_status == "infeasible":
        return []
    if missing_slots:
        return format_missing_slot_prompts(missing_slots)
    timeline = timelines_by_path.get(item.get("primary_category"), [])
    if timeline:
        return [
        " / ".join(filter(None, [step.get("stage_label"), step.get("time_label"), step.get("action")]))
        for step in timeline[:3]
        ]
    return ["该路径的行动计划资产暂未覆盖，后续版本会提供详细的分析。"]


def _build_convergence_reply(
    feasible_candidates: list[dict],
    partial_candidates: list[dict],
    infeasible_candidates: list[dict],
    planner_state: dict,
    asset_support: dict,
    unknown_fact_keys: list[str],
) -> str:
    sections: list[str] = []
    planner_missing_facts = sort_fact_keys_for_form(planner_state.get("missing_facts") or [])
    normalized_unknown_fact_keys = sort_fact_keys_for_form(unknown_fact_keys)

    supported_dimensions = asset_support.get("supported_dimensions", []) or []
    if supported_dimensions:
        sections.append(f"当前判断基于这些已整理资产维度：{'、'.join(supported_dimensions)}。")
    if normalized_unknown_fact_keys:
        sections.append(
            "已明确暂缺的信息："
            + "、".join(format_fact_label(item) for item in normalized_unknown_fact_keys)
            + "。本轮先不把这些条件当作硬筛选条件；后续补充后，可进一步确认相关路径的适配性。"
        )

    if feasible_candidates:
        lines = ["## 可行路径"]
        for item in feasible_candidates[:5]:
            lines.append(f"- `{item.get('primary_category')}`：{item.get('reasons', ['当前已满足主要规则条件'])[0]}")
            next_steps = item.get("next_step_plan", []) or []
            if next_steps:
                lines.append(f"  下一步：{'；'.join(next_steps[:2])}")
        sections.append("\n".join(lines))

    if partial_candidates:
        lines = ["## 部分条件满足"]
        for item in partial_candidates[:4]:
            visible_missing_slots = item.get("missing_slots", []) or []
            if planner_missing_facts:
                visible_missing_slots = [
                    slot for slot in visible_missing_slots if slot in planner_missing_facts
                ]
            visible_missing_slots = sort_fact_keys_for_form(visible_missing_slots)
            missing_labels = [format_fact_label(slot) for slot in visible_missing_slots]
            lines.append(
                f"- `{item.get('primary_category')}`：还需补充 {('、'.join(missing_labels) if missing_labels else '关键信息')}"
            )
            next_steps = item.get("next_step_plan", []) or []
            if next_steps:
                lines.append(f"  下一步：{'；'.join(next_steps[:2])}")
        sections.append("\n".join(lines))

    if infeasible_candidates:
        lines = ["## 当前不满足"]
        for item in infeasible_candidates[:4]:
            lines.append(
                f"- `{item.get('primary_category')}`：{('、'.join(item.get('blocking_reasons', [])) or '当前规则不满足')}"
            )
        sections.append("\n".join(lines))

    unavailable = asset_support.get("dynamic_unavailable_dimensions", []) or []
    if unavailable:
        sections.append(
            f"暂未展开的维度：{'、'.join(unavailable)}。该维度的信息正在整理中，后续版本会提供详细的分析。"
        )

    question_hint = planner_state.get("question_hint")
    if planner_state.get("should_ask_question") and question_hint:
        sections.append(question_hint)

    return "\n\n".join(sections) if sections else "当前还没有足够资产生成路径收敛结果。"


def _band_matches(rule_row: dict, score_band_tag: str | None) -> bool:
    if not score_band_tag:
        return True
    allowed = (rule_row.get("标签法（成绩下限所在区间）") or "").replace("，", ",")
    if not allowed or allowed == "单独成绩匹配，≥450":
        return True
    return score_band_tag in allowed


def _score_candidate(
    item: dict,
    reason: dict,
    rule_row: dict | None,
    facts: dict,
) -> tuple[float, list[str], list[str], list[str], str]:
    score = 0.25
    reasons: list[str] = []
    risk_level = "medium"

    text_blob = _build_text_blob(item, reason)
    effective_context = _build_effective_rule_context(item, facts)
    rule_blob = effective_context.get("rule_blob", text_blob)

    focus_categories = set(facts.get("focus_primary_categories") or [])
    focus_path_ids = set(facts.get("focus_path_ids") or [])
    student_province = facts.get("student_province")
    normalized_student_province = normalize_province(student_province) if student_province else None
    score_band_tag = facts.get("score_band_tag")
    budget_level = facts.get("budget_level")
    special_tags = facts.get("special_identity_tags") or []
    missing_slots = _extract_condition_slots(text_blob, facts, item)
    blocking_reasons = _extract_blocking_reasons(text_blob, rule_row, facts, item)

    if item.get("path_id") in focus_path_ids:
        score += 0.45
        reasons.append("用户明确提到了这条路径")
    if item.get("primary_category") in focus_categories:
        score += 0.35
        reasons.append("用户当前重点关注这一类路径")

    if normalized_student_province and any(
        keyword in rule_blob for keyword in ["省份", "所在省份", "学生户籍地", "学生户籍所在地", "户籍"]
    ):
        if normalized_student_province in normalize_province(rule_blob):
            score += 0.12
            reasons.append("规则中出现了当前省份")
        else:
            score -= 0.1
            reasons.append("规则里没有明显命中当前省份")

    if rule_row and _band_matches(rule_row, score_band_tag):
        score += 0.12
        reasons.append("成绩带与该路径露出区间相符")
    elif rule_row and score_band_tag:
        score -= 0.18
        risk_level = "high"
        reasons.append("当前成绩带与该路径露出区间不一致")

    if any(keyword in rule_blob for keyword in ["竞赛", "奖项", "国家集训队"]):
        if special_tags:
            score += 0.18
            reasons.append("用户具备竞赛/奖项相关特征")
        else:
            score -= 0.08

    if normalize_budget_level(budget_level) == "<5万" and any(keyword in text_blob for keyword in ["中外合作", "海外", "学费"]):
        score -= 0.2
        risk_level = "high"
        reasons.append("预算偏低，与费用型路径存在冲突")

    if any(keyword in item.get("primary_category", "") for keyword in ["普通高考"]):
        score += 0.08
        reasons.append("普通高考可作为基础保底路径")

    if "特控线" in text_blob and score_band_tag in {"二段线/专科线下", "二段线/专科线上"}:
        score -= 0.15
        risk_level = "high"
        reasons.append("该路径通常更依赖较高成绩带")

    if "高职单招" in item.get("primary_category", "") and score_band_tag in {"二段线/专科线上", "二段线/专科线下"}:
        score += 0.18
        reasons.append("当前成绩带与高职路径更匹配")

    if blocking_reasons:
        score -= min(0.36, 0.12 * len(blocking_reasons))
        risk_level = "high"

    score = max(0.0, min(score, 1.0))
    return score, reasons, sorted(set(missing_slots)), blocking_reasons, risk_level


def _rerank_with_llm(
    llm_client, user_input: str, context, scored_candidates: list[dict]
) -> tuple[list[dict], dict | None]:
    llm_result = safe_complete_json(
        llm_client,
        "convergence_ranking",
        {
            "user_input": user_input,
            "facts": {
                key: value.model_dump() for key, value in context.known_facts.facts.items()
            },
            "candidates": [
                {
                    "path_id": item["path_id"],
                    "primary_category": item["primary_category"],
                    "description": item.get("description", ""),
                    "rule_text_raw": item.get("rule_text_raw", ""),
                    "base_score": item["match_score"],
                }
                for item in scored_candidates[:8]
            ],
        },
    )
    if not llm_result:
        return scored_candidates, None

    ranked_path_ids = llm_result.get("ranked_path_ids") or []
    reason_by_path = llm_result.get("reason_by_path") or {}
    order_map = {path_id: index for index, path_id in enumerate(ranked_path_ids)}
    reordered = sorted(
        scored_candidates,
        key=lambda item: (order_map.get(item["path_id"], 999), -item["match_score"]),
    )
    for item in reordered:
        llm_reason = reason_by_path.get(item["path_id"])
        if llm_reason:
            item["reasons"] = [llm_reason, *item.get("reasons", [])][:3]
    return reordered, llm_result


def suggest_high_value_missing_facts(
    facts: dict[str, object],
    *,
    limit: int = 4,
) -> list[str]:
    catalog = load_json("assets/generated/multiroute/path_catalog.json", [])
    use_score_band_rules = feature_flag_enabled(
        ENABLE_SCORE_BAND_EXPOSURE_RULES, default=False
    )
    score_band_rules = (
        load_json("assets/generated/multiroute/score_band_exposure_rules.json", [])
        if use_score_band_rules
        else []
    )
    province_score_lines = load_json(
        "assets/generated/multiroute/province_score_lines.json", []
    )
    reason_map = {
        item.get("path_id"): item
        for item in load_json("assets/generated/multiroute/path_reason_templates.json", [])
    }
    score_rule_map = {
        item.get("一级升学大类"): item for item in score_band_rules if item.get("一级升学大类")
    }

    working_facts = dict(facts)
    excluded_path_ids = set(working_facts.get("excluded_path_ids") or [])
    excluded_primary_categories = set(working_facts.get("excluded_primary_categories") or [])
    province_line = _find_province_line(
        province_score_lines,
        working_facts.get("student_province"),
        working_facts.get("subject_group"),
    )
    working_facts["score_band_tag"] = working_facts.get("score_band_tag") or _normalize_score_band(
        working_facts.get("score_total"),
        province_line,
    )

    partial_candidates: list[dict[str, object]] = []
    for item in catalog:
        if item.get("path_id") in excluded_path_ids:
            continue
        if item.get("primary_category") in excluded_primary_categories:
            continue
        if _should_exclude_for_selected_facts(item, working_facts):
            continue
        reason = reason_map.get(item.get("path_id"), {})
        rule_row = score_rule_map.get(item.get("primary_category"))
        match_score, _, missing_slots, blocking_reasons, _ = _score_candidate(
            item,
            reason,
            rule_row,
            working_facts,
        )
        feasibility_status = _resolve_feasibility_status(missing_slots, blocking_reasons)
        if feasibility_status != "partial" or not missing_slots:
            continue
        partial_candidates.append(
            {
                "path_id": item.get("path_id"),
                "match_score": match_score,
                "missing_slots": missing_slots,
            }
        )

    partial_candidates.sort(key=lambda item: (-float(item["match_score"]), str(item["path_id"])))
    slot_stats: dict[str, dict[str, float]] = {}
    for item in partial_candidates[:6]:
        for slot in item.get("missing_slots", []):
            stats = slot_stats.setdefault(slot, {"count": 0, "best_score": 0.0})
            stats["count"] += 1
            stats["best_score"] = max(stats["best_score"], float(item["match_score"]))

    ranked_slots = sorted(
        slot_stats.items(),
        key=lambda item: (-int(item[1]["count"]), -float(item[1]["best_score"]), item[0]),
    )
    return [slot for slot, _ in ranked_slots[:limit]]


class ConvergenceSkill(BaseSkill):
    skill_name = "convergence"

    def __init__(self, llm_client=None) -> None:
        self.llm_client = llm_client

    def run(self, user_input: str, context) -> SkillResult:
        planner_state = context.skill_states.get("planner", {})
        catalog = load_json("assets/generated/multiroute/path_catalog.json", [])
        use_score_band_rules = feature_flag_enabled(
            ENABLE_SCORE_BAND_EXPOSURE_RULES, default=False
        )
        score_band_rules = (
            load_json("assets/generated/multiroute/score_band_exposure_rules.json", [])
            if use_score_band_rules
            else []
        )
        province_score_lines = load_json(
            "assets/generated/multiroute/province_score_lines.json", []
        )
        reason_map = {
            item.get("path_id"): item
            for item in load_json("assets/generated/multiroute/path_reason_templates.json", [])
        }
        score_rule_map = {
            item.get("一级升学大类"): item for item in score_band_rules if item.get("一级升学大类")
        }

        facts = {
            key: value.value for key, value in context.known_facts.facts.items()
        }
        excluded_path_ids = set(facts.get("excluded_path_ids") or [])
        excluded_primary_categories = set(facts.get("excluded_primary_categories") or [])
        province_line = _find_province_line(
            province_score_lines, facts.get("student_province"), facts.get("subject_group")
        )
        facts["score_band_tag"] = facts.get("score_band_tag") or _normalize_score_band(
            facts.get("score_total"), province_line
        )
        timelines_by_path = build_path_timelines([item.get("primary_category") for item in catalog])

        scored_paths = []
        for item in catalog:
            if item.get("path_id") in excluded_path_ids:
                continue
            if item.get("primary_category") in excluded_primary_categories:
                continue
            if _should_exclude_for_selected_facts(item, facts):
                continue
            reason = reason_map.get(item.get("path_id"), {})
            rule_row = score_rule_map.get(item.get("primary_category"))
            match_score, reason_list, missing_slots, blocking_reasons, risk_level = _score_candidate(
                item, reason, rule_row, facts
            )
            text_blob = _build_text_blob(item, reason)
            effective_rule_context = _build_effective_rule_context(item, facts)
            missing_slots = _extract_condition_slots(text_blob, facts, item)
            blocking_reasons = _extract_blocking_reasons(text_blob, rule_row, facts, item)
            is_conditional = _is_conditional_candidate(item, text_blob, missing_slots)
            feasibility_status = _resolve_feasibility_status(missing_slots, blocking_reasons)
            scored_paths.append(
                {
                    "path_id": item.get("path_id"),
                    "primary_category": item.get("primary_category"),
                    "match_score": round(match_score, 3),
                    "eligibility_status": (
                        "conditional"
                        if is_conditional
                        else "recommended" if match_score >= 0.58 else "consider"
                    ),
                    "feasibility_status": feasibility_status,
                    "feasibility_label": STATUS_LABELS[feasibility_status],
                    "risk_level": risk_level,
                    "missing_slots": missing_slots,
                    "blocking_reasons": blocking_reasons,
                    "reasons": (
                        reason_list
                        or [reason.get("match_reason", item.get("description", ""))[:80]]
                    ),
                    "description": item.get("description", ""),
                    "rule_text_raw": item.get("rule_text_raw", ""),
                    "rule_variants": effective_rule_context.get("rule_variants", []),
                    "required_fact_keys": effective_rule_context.get("required_fact_keys", []),
                    "geo_constraints": effective_rule_context.get("geo_constraints", {}),
                    "subject_constraints": effective_rule_context.get("subject_constraints", []),
                    "action_timeline": timelines_by_path.get(item.get("primary_category"), []),
                    "next_step_plan": _build_next_step_plan(
                        item, missing_slots, feasibility_status, timelines_by_path
                    ),
                    "sheet_group": item.get("sheet_group", ""),
                    "target_users": item.get("target_users", ""),
                    "is_conditional": is_conditional,
                }
            )
        feasible_candidates = sorted(
            [item for item in scored_paths if item["feasibility_status"] == "feasible"],
            key=lambda item: (-item["match_score"], item["path_id"]),
        )
        partial_candidates = sorted(
            [item for item in scored_paths if item["feasibility_status"] == "partial"],
            key=lambda item: (-item["match_score"], item["path_id"]),
        )
        infeasible_candidates = sorted(
            [item for item in scored_paths if item["feasibility_status"] == "infeasible"],
            key=lambda item: (-item["match_score"], item["path_id"]),
        )
        feasible_candidates, ranking_llm_response = _rerank_with_llm(
            self.llm_client, user_input, context, feasible_candidates
        )
        self._last_prompt_info = build_prompt_record(
            self.skill_name,
            "convergence_response",
            "convergence_ranking",
            {
                "user_input": user_input,
                "facts": {key: value.model_dump() for key, value in context.known_facts.facts.items()},
                "candidates": [
                    {
                        "path_id": item["path_id"],
                        "primary_category": item["primary_category"],
                        "description": item.get("description", ""),
                        "rule_text_raw": item.get("rule_text_raw", ""),
                        "base_score": item["match_score"],
                    }
                    for item in scored_paths[:8]
                ],
            },
            llm_response=ranking_llm_response,
        )
        top_feasible_candidates = feasible_candidates[:5]
        top_partial_candidates = partial_candidates[:4]
        top_infeasible_candidates = infeasible_candidates[:4]
        candidate_paths = [
            *top_feasible_candidates,
            *top_partial_candidates,
            *top_infeasible_candidates,
        ]
        asset_support = build_asset_support(
            self.skill_name,
            candidate_paths=candidate_paths,
        )
        unknown_fact_keys = list(
            context.skill_states.get("facts_extractor", {}).get("unknown_fact_keys") or []
        )

        structured_result = {
            "candidate_paths": candidate_paths,
            "feasible_candidates": top_feasible_candidates,
            "partial_candidates": top_partial_candidates,
            "infeasible_candidates": top_infeasible_candidates,
            "planner_state": planner_state,
            "catalog_count": len(catalog),
            "ranking_snapshot": {
                "student_province": facts.get("student_province"),
                "score_total": facts.get("score_total"),
                "score_band_tag": facts.get("score_band_tag"),
                "score_band_exposure_rules_enabled": use_score_band_rules,
                "focus_primary_categories": facts.get("focus_primary_categories"),
                "focus_path_ids": facts.get("focus_path_ids"),
                "excluded_primary_categories": facts.get("excluded_primary_categories"),
                "excluded_path_ids": facts.get("excluded_path_ids"),
                "feasible_candidate_count": len(feasible_candidates),
                "partial_candidate_count": len(partial_candidates),
                "infeasible_candidate_count": len(infeasible_candidates),
            },
            "asset_support": asset_support,
            "unknown_fact_keys": unknown_fact_keys,
        }
        draft_reply = _build_convergence_reply(
            top_feasible_candidates,
            top_partial_candidates,
            top_infeasible_candidates,
            planner_state,
            asset_support,
            unknown_fact_keys,
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
        reply = polished_reply or draft_reply
        if not candidate_paths and not reply:
            reply = "多元路径资产尚未生成，请先运行编译脚本构建 path catalog。"

        return SkillResult(
            assistant_message=reply,
            candidate_paths=candidate_paths,
            suggested_paths=[
                item["primary_category"]
                for item in [*top_feasible_candidates, *top_partial_candidates]
                if item.get("primary_category")
            ],
            state_patch={
                "ranking_snapshot": structured_result["ranking_snapshot"],
                "asset_support": asset_support,
            },
            events=[
                make_event(
                    "convergence_asset_match",
                    {
                        "stage": "convergence",
                        "assets_used": [
                            "assets/generated/multiroute/path_catalog.json",
                            "assets/generated/multiroute/path_reason_templates.json",
                            "assets/generated/multiroute/action_timeline_templates.json",
                            "assets/generated/multiroute/province_score_lines.json",
                            "assets/generated/multiroute/score_band_exposure_rules.json",
                        ],
                        "inputs": {
                            "student_province": facts.get("student_province"),
                            "student_region": facts.get("student_region"),
                            "subject_group": facts.get("subject_group"),
                            "score_total": facts.get("score_total"),
                            "unknown_fact_keys": unknown_fact_keys,
                            "score_band_tag": facts.get("score_band_tag"),
                            "excluded_primary_categories": facts.get("excluded_primary_categories"),
                            "excluded_path_ids": facts.get("excluded_path_ids"),
                        },
                        "filters": {
                            "excluded_primary_categories": sorted(excluded_primary_categories),
                            "excluded_path_ids": sorted(excluded_path_ids),
                        },
                        "province_line": province_line,
                        "top_feasible": [
                            {
                                "path_id": item.get("path_id"),
                                "primary_category": item.get("primary_category"),
                                "missing_slots": item.get("missing_slots", []),
                            }
                            for item in top_feasible_candidates
                        ],
                        "top_partial": [
                            {
                                "path_id": item.get("path_id"),
                                "primary_category": item.get("primary_category"),
                                "missing_slots": item.get("missing_slots", []),
                                "blocking_reasons": item.get("blocking_reasons", []),
                            }
                            for item in top_partial_candidates
                        ],
                        "top_infeasible": [
                            {
                                "path_id": item.get("path_id"),
                                "primary_category": item.get("primary_category"),
                                "blocking_reasons": item.get("blocking_reasons", []),
                            }
                            for item in top_infeasible_candidates
                        ],
                        "asset_support": asset_support,
                        "polish_applied": bool(polished_reply),
                        "polish_style": polish_style,
                    },
                ),
                make_event(
                    "convergence_reply",
                    {
                        "candidate_count": len(candidate_paths),
                        "score_band_exposure_rules_enabled": use_score_band_rules,
                        "feasible_candidate_count": len(top_feasible_candidates),
                        "partial_candidate_count": len(top_partial_candidates),
                        "infeasible_candidate_count": len(top_infeasible_candidates),
                        "response_mode": planner_state.get("response_mode"),
                    },
                )
            ],
        )
