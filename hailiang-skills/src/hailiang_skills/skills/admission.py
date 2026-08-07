from __future__ import annotations

from hailiang_skills.core.logging import make_event
from hailiang_skills.skills.asset_support import build_asset_support
from hailiang_skills.skills.assets import feature_flag_enabled, load_json
from hailiang_skills.skills.base import BaseSkill, SkillResult
from hailiang_skills.skills.common import (
    build_skill_prompt_record,
    collect_known_provinces,
    extract_province,
    extract_score,
    extract_subject_group,
    find_catalog_path_matches,
    llm_polish_structured_reply,
    llm_compose_reply,
    normalize_subject_group,
    normalize_path_name,
    resolve_summary_style,
)
from hailiang_skills.skills.convergence import (
    STATUS_LABELS,
    _build_text_blob,
    _build_next_step_plan,
    _extract_blocking_reasons,
    _extract_condition_slots,
    _resolve_feasibility_status,
    build_path_timelines,
)


ENABLE_SCORE_BAND_EXPOSURE_RULES = "HAILIANG_ENABLE_SCORE_BAND_EXPOSURE_RULES"


def _build_admission_candidate_paths(matched_items: list[dict], context) -> list[dict]:
    catalog = load_json("assets/generated/multiroute/path_catalog.json", [])
    reason_templates = load_json("assets/generated/multiroute/path_reason_templates.json", [])
    use_score_band_rules = feature_flag_enabled(
        ENABLE_SCORE_BAND_EXPOSURE_RULES, default=False
    )
    score_rules = (
        load_json("assets/generated/multiroute/score_band_exposure_rules.json", [])
        if use_score_band_rules
        else []
    )

    facts = {
        key: context.known_facts.get_value(key)
        for key in [
            "student_province",
            "student_region",
            "subject_group",
            "score_total",
            "score_band_tag",
            "budget_level",
            "family_type",
            "ethnicity",
        ]
    }
    facts["subject_group"] = normalize_subject_group(facts.get("subject_group"))

    reason_map = {item.get("path_id"): item for item in reason_templates}
    score_rule_map = {item.get("一级升学大类"): item for item in score_rules if item.get("一级升学大类")}
    timelines_by_path = build_path_timelines([item.get("primary_category") for item in catalog])

    candidate_paths: list[dict] = []
    seen: set[str] = set()
    for matched_item in matched_items:
        for path_name in matched_item.get("recommended_paths", []):
            normalized_path_name = normalize_path_name(path_name) or path_name
            if normalized_path_name in seen:
                continue
            seen.add(normalized_path_name)

            matched_catalog_items = find_catalog_path_matches(
                normalized_path_name,
                catalog,
                student_province=facts.get("student_province"),
                limit=3,
            )
            catalog_item = matched_catalog_items[0] if matched_catalog_items else None
            if catalog_item:
                reason = reason_map.get(catalog_item.get("path_id"), {})
                rule_row = score_rule_map.get(catalog_item.get("primary_category"))
                text_blob = _build_text_blob(catalog_item, reason)
                missing_slots = _extract_condition_slots(text_blob, facts, catalog_item)
                blocking_reasons = _extract_blocking_reasons(text_blob, rule_row, facts, catalog_item)
                feasibility_status = _resolve_feasibility_status(
                    missing_slots, blocking_reasons
                )
                candidate_paths.append(
                    {
                        "path_id": catalog_item.get("path_id"),
                        "primary_category": normalized_path_name,
                        "source_label": path_name,
                        "match_score": 0.82 if feasibility_status == "feasible" else 0.64,
                        "eligibility_status": (
                            "recommended" if feasibility_status == "feasible" else "conditional"
                        ),
                        "feasibility_status": feasibility_status,
                        "feasibility_label": STATUS_LABELS[feasibility_status],
                        "risk_level": "medium" if feasibility_status != "infeasible" else "high",
                        "missing_slots": missing_slots,
                        "blocking_reasons": blocking_reasons,
                        "reasons": [
                            f"{matched_item.get('region_variant') or matched_item.get('province')} {matched_item.get('tier_name')} 档位推荐路径"
                        ],
                        "action_timeline": timelines_by_path.get(normalized_path_name, []),
                        "next_step_plan": _build_next_step_plan(
                            {"primary_category": normalized_path_name},
                            missing_slots,
                            feasibility_status,
                            timelines_by_path,
                        ),
                        "description": catalog_item.get("description", ""),
                        "sheet_group": catalog_item.get("sheet_group", "模拟升学推荐"),
                        "target_users": catalog_item.get("target_users", ""),
                    }
                )
                continue

            candidate_paths.append(
                {
                    "path_id": f"admission:{path_name}",
                    "primary_category": normalized_path_name,
                    "source_label": path_name,
                    "match_score": 0.76,
                    "eligibility_status": "recommended",
                    "feasibility_status": "feasible",
                    "feasibility_label": STATUS_LABELS["feasible"],
                    "risk_level": "medium",
                    "missing_slots": [],
                    "blocking_reasons": [],
                    "reasons": [
                        f"{matched_item.get('region_variant') or matched_item.get('province')} {matched_item.get('tier_name')} 档位推荐路径"
                    ],
                    "action_timeline": timelines_by_path.get(normalized_path_name, []),
                    "next_step_plan": timelines_by_path.get(normalized_path_name, [])[:3],
                    "description": matched_item.get("tier_name", ""),
                    "sheet_group": "模拟升学推荐",
                    "target_users": matched_item.get("tier_name", ""),
                }
            )

    return candidate_paths[:8]


def _build_admission_matches_brief(matched_items: list[dict]) -> list[dict]:
    matches: list[dict] = []
    for item in matched_items:
        matches.append(
            {
                "region_variant": item.get("region_variant"),
                "tier_name": item.get("tier_name"),
                "subject_group": item.get("subject_group"),
                "score_range": {
                    "min_score": item.get("min_score"),
                    "max_score": item.get("max_score"),
                },
                "sample_schools": item.get("sample_schools", []),
                "recommended_paths": item.get("recommended_paths", []),
            }
        )
    return matches


def _build_institution_tier_summary(
    *,
    score: int | None,
    province: str | None,
    subject_group: str | None,
    matched_items: list[dict],
) -> dict:
    if score is None or not province or not matched_items:
        return {}
    primary = matched_items[0]
    sample_schools = [school for school in primary.get("sample_schools", []) if school]
    recommended_paths = [path for path in primary.get("recommended_paths", []) if path]
    return {
        "score": score,
        "province": province,
        "subject_group": subject_group,
        "region_variant": primary.get("region_variant") or primary.get("province"),
        "tier_name": primary.get("tier_name"),
        "score_range": {
            "min_score": primary.get("min_score"),
            "max_score": primary.get("max_score"),
        },
        "sample_schools": sample_schools,
        "recommended_paths": recommended_paths,
    }


def _format_institution_tier_summary(summary: dict) -> str:
    tier_name = summary.get("tier_name")
    if not tier_name:
        return ""
    score = summary.get("score")
    province = summary.get("province")
    subject_group = summary.get("subject_group")
    region_variant = summary.get("region_variant") or province
    score_range = summary.get("score_range") or {}
    min_score = score_range.get("min_score")
    max_score = score_range.get("max_score")
    sample_schools = summary.get("sample_schools") or []
    recommended_paths = summary.get("recommended_paths") or []

    basis_parts = [str(province), f"{score}分" if score is not None else ""]
    if subject_group:
        basis_parts.insert(1, str(subject_group))
    basis = " / ".join(part for part in basis_parts if part)
    range_text = f"{min_score}-{max_score}分" if min_score is not None and max_score is not None else ""

    lines = [f"院校层次定位：{basis}，当前命中 `{region_variant}` 的 `{tier_name}` 档。"]
    if range_text:
        lines.append(f"这个档位在资产中的分数范围是 {range_text}。")
    if sample_schools:
        lines.append(f"代表院校可参考：{'、'.join(sample_schools[:4])}。")
    if recommended_paths:
        lines.append(f"可优先关注路径：{'、'.join(recommended_paths[:4])}。")
    return "\n".join(lines)


def _ensure_institution_tier_visible(reply: str, summary: dict) -> str:
    summary_text = _format_institution_tier_summary(summary)
    if not summary_text:
        return reply
    tier_name = str(summary.get("tier_name") or "")
    if tier_name and tier_name in reply and ("院校层次" in reply or "档" in reply):
        return reply
    if not reply:
        return summary_text
    return f"{summary_text}\n\n{reply}"


def _build_tier_intro_map() -> dict[str, str]:
    return {
        item.get("tier_name"): item.get("intro", "")
        for item in load_json("assets/generated/admission/tier_copywriting.json", [])
        if item.get("tier_name")
    }


def _build_recommended_path_timelines(path_names: list[str]) -> list[dict]:
    templates = load_json("assets/generated/multiroute/action_timeline_templates.json", [])
    target_names = {normalize_path_name(path_name) or path_name for path_name in path_names}
    timelines: dict[str, list[dict]] = {path_name: [] for path_name in sorted(target_names)}

    for item in templates:
        stage_label = item.get("grade")
        time_label = item.get("time_label")
        actions = item.get("actions") or {}
        for action_name, action_text in actions.items():
            normalized_action_name = normalize_path_name(action_name) or action_name
            if normalized_action_name in target_names:
                timelines.setdefault(normalized_action_name, []).append(
                    {
                        "stage_label": stage_label or None,
                        "time_label": time_label,
                        "action": action_text,
                    }
                )

    return [
        {
            "path_name": path_name,
            "timeline": timelines.get(path_name, []),
        }
        for path_name in sorted(target_names)
    ]


class AdmissionSkill(BaseSkill):
    skill_name = "admission"

    def __init__(self, llm_client=None) -> None:
        self.llm_client = llm_client

    def run(self, user_input: str, context) -> SkillResult:
        planner_state = context.skill_states.get("planner", {})
        use_score_band_rules = feature_flag_enabled(
            ENABLE_SCORE_BAND_EXPOSURE_RULES, default=False
        )
        score = extract_score(user_input) or context.known_facts.get_value("score_total")
        assets = load_json("assets/generated/admission/province_score_bands.json", [])
        flow_map = load_json("assets/generated/admission/province_flow_map.json", {})
        known_provinces = collect_known_provinces(assets)
        province = extract_province(user_input, known_provinces) or context.known_facts.get_value(
            "student_province"
        )
        subject_group = normalize_subject_group(
            extract_subject_group(user_input) or context.known_facts.get_value("subject_group")
        )

        matched = []
        for item in assets:
            if province and province not in item.get("province", ""):
                continue
            if subject_group and item.get("subject_group") and item.get("subject_group") != subject_group:
                continue
            if score is None:
                matched.append(item)
                continue
            min_score = item.get("min_score")
            max_score = item.get("max_score")
            if min_score is not None and max_score is not None and min_score <= score <= max_score:
                matched.append(item)

        summary = []
        if matched:
            for item in matched[:3]:
                summary.append(
                    f"{item.get('province')} {item.get('tier_name')} -> {'、'.join(item.get('recommended_paths', []))}"
                )
        else:
            summary.append("已进入模拟升学流程，但当前还没有足够结构化信息命中分数档。")

        matched_brief = _build_admission_matches_brief(matched[:3])
        institution_tier_summary = _build_institution_tier_summary(
            score=score,
            province=province,
            subject_group=subject_group,
            matched_items=matched[:3],
        )
        candidate_paths = _build_admission_candidate_paths(matched[:5], context)
        suggested_paths = [
            item.get("primary_category") for item in candidate_paths if item.get("primary_category")
        ]
        matched_school_names = [
            school_name
            for item in matched_brief
            for school_name in item.get("sample_schools", [])
            if school_name
        ]
        recommended_path_ids = [item.get("path_id") for item in candidate_paths if item.get("path_id")]
        tier_intro_map = _build_tier_intro_map()
        matched_tier_copywriting = [
            {
                "tier_name": item.get("tier_name"),
                "intro": tier_intro_map.get(item.get("tier_name"), ""),
            }
            for item in matched_brief
            if item.get("tier_name")
        ]
        recommended_path_timelines = _build_recommended_path_timelines(suggested_paths)
        asset_support = build_asset_support(
            self.skill_name,
            candidate_paths=candidate_paths,
            matched_items_brief=matched_brief,
            recommended_path_timelines=recommended_path_timelines,
        )

        structured_result = {
            "score": score,
            "province": province,
            "subject_group": subject_group,
            "matched_count": len(matched),
            "matched_items": matched[:5],
            "matched_items_brief": matched_brief,
            "institution_tier_summary": institution_tier_summary,
            "matched_school_names": matched_school_names,
            "matched_tier_copywriting": matched_tier_copywriting,
            "admission_candidate_paths": candidate_paths,
            "recommended_path_timelines": recommended_path_timelines,
            "planner_state": planner_state,
            "flow_map_loaded": bool(flow_map),
            "score_band_exposure_rules_enabled": use_score_band_rules,
            "asset_support": asset_support,
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
            "admission_response",
            user_input,
            context,
            planner_state,
            structured_result,
            llm_response=draft_reply,
        )
        if not draft_reply:
            draft_reply = "模拟升学结果：\n" + "\n".join(f"- {line}" for line in summary)
            if flow_map:
                draft_reply += "\n\n系统已加载省份流程映射，可继续补充省份或选科以缩小推荐范围。"
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
        reply = _ensure_institution_tier_visible(reply, institution_tier_summary)

        updated_facts = {}
        if score is not None:
            updated_facts["score_total"] = context.update_fact(
                "score_total", score, source_skill=self.skill_name
            )
        if province:
            updated_facts["student_province"] = context.update_fact(
                "student_province", province, source_skill=self.skill_name
            )
        if subject_group:
            updated_facts["subject_group"] = context.update_fact(
                "subject_group", subject_group, source_skill=self.skill_name
            )

        return SkillResult(
            assistant_message=reply,
            updated_facts=updated_facts,
            candidate_paths=candidate_paths,
            suggested_paths=suggested_paths,
            state_patch={
                "score": score,
                "province": province,
                "subject_group": subject_group,
                "matched_count": len(matched),
                "matched_items_brief": matched_brief,
                "institution_tier_summary": institution_tier_summary,
                "matched_school_names": matched_school_names,
                "matched_tier_copywriting": matched_tier_copywriting,
                "recommended_path_ids": recommended_path_ids,
                "recommended_path_names": suggested_paths,
                "recommended_path_timelines": recommended_path_timelines,
                "score_band_exposure_rules_enabled": use_score_band_rules,
                "candidate_count": len(candidate_paths),
                "asset_support": asset_support,
            },
            events=[
                make_event(
                    "admission_asset_match",
                    {
                        "stage": "admission",
                        "assets_used": [
                            "assets/generated/admission/province_score_bands.json",
                            "assets/generated/admission/province_flow_map.json",
                            "assets/generated/admission/tier_copywriting.json",
                            "assets/generated/multiroute/path_catalog.json",
                            "assets/generated/multiroute/path_reason_templates.json",
                            "assets/generated/multiroute/action_timeline_templates.json",
                        ],
                        "inputs": {
                            "province": province,
                            "subject_group": subject_group,
                            "score": score,
                        },
                        "matched_items_brief": matched_brief,
                        "institution_tier_summary": institution_tier_summary,
                        "recommended_path_names": suggested_paths,
                        "recommended_path_ids": recommended_path_ids,
                        "asset_support": asset_support,
                        "polish_applied": bool(polished_reply),
                        "polish_style": polish_style,
                    },
                ),
                make_event(
                    "admission_reply",
                    {
                        "matched_count": len(matched),
                        "score": score,
                        "province": province,
                        "subject_group": subject_group,
                        "score_band_exposure_rules_enabled": use_score_band_rules,
                        "candidate_count": len(candidate_paths),
                        "institution_tier_summary": institution_tier_summary,
                    },
                )
            ],
        )
