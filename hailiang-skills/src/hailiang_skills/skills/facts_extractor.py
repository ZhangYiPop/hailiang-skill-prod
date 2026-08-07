from __future__ import annotations

import re

from hailiang_skills.core.facts_config import get_enabled_fact_keys
from hailiang_skills.core.logging import make_event
from hailiang_skills.llm.service import build_context_snapshot, safe_complete_json
from hailiang_skills.skills.assets import load_json
from hailiang_skills.skills.base import BaseSkill, SkillResult
from hailiang_skills.schemas.facts import normalize_fact_value
from hailiang_skills.skills.common import (
    build_prompt_record,
    collect_known_provinces,
    extract_career_orientation,
    extract_excluded_targets,
    extract_ethnicity,
    extract_explicit_exam_province,
    extract_focus_targets,
    extract_focus_school_names,
    extract_grade,
    extract_score_payload,
    extract_special_identity_tags,
    extract_student_region,
    extract_subject_group,
    infer_family_type,
    infer_guardian_hukou_match,
    infer_budget_level,
    infer_exam_qualification_status,
    infer_hukou_years,
    infer_school_status_years,
    infer_termination_preference,
)


def _infer_focus_from_admission_recommendations(context, user_input: str) -> tuple[list[str], list[str]]:
    admission_state = context.skill_states.get("admission", {})
    recommended_path_ids = admission_state.get("recommended_path_ids") or []
    recommended_path_names = admission_state.get("recommended_path_names") or []
    if not recommended_path_ids:
        return [], []

    if not any(
        keyword in user_input
        for keyword in [
            "推荐路径",
            "具体路径",
            "这些路径",
            "这些推荐",
            "想了解路径",
            "对哪些路径感兴趣",
            "展开讲路径",
        ]
    ):
        return [], []

    return recommended_path_ids[:5], recommended_path_names[:5]


def _build_fallback_updates(
    user_input: str,
    *,
    known_provinces: list[str],
    focus_path_ids: list[str],
    focus_primary_categories: list[str],
    excluded_path_ids: list[str],
    excluded_primary_categories: list[str],
    focus_school_names: list[str],
) -> dict[str, object]:
    score_payload = extract_score_payload(user_input)
    extractor_outputs: dict[str, object] = {
        "grade": extract_grade(user_input),
        "student_province": extract_explicit_exam_province(user_input, known_provinces),
        "student_region": extract_student_region(user_input),
        "subject_group": extract_subject_group(user_input),
        "foreign_language": _extract_foreign_language(user_input),
        "english_exam_score": _extract_english_exam_score(user_input),
        "score_total": score_payload.get("score_total"),
        "score_recent_avg": score_payload.get("score_recent_avg"),
        "score_source": score_payload.get("score_source"),
        "score_band_tag": None,
        "budget_level": infer_budget_level(user_input),
        "family_type": infer_family_type(user_input),
        "ethnicity": extract_ethnicity(user_input),
        "hukou_years": infer_hukou_years(user_input),
        "guardian_hukou_match": infer_guardian_hukou_match(user_input),
        "school_status_years": infer_school_status_years(user_input),
        "exam_qualification_status": infer_exam_qualification_status(user_input),
        "special_identity_tags": extract_special_identity_tags(user_input),
        "career_orientation": extract_career_orientation(user_input),
        "termination_preference": infer_termination_preference(user_input),
        "focus_path_ids": focus_path_ids,
        "focus_primary_categories": focus_primary_categories,
        "excluded_path_ids": excluded_path_ids,
        "excluded_primary_categories": excluded_primary_categories,
        "focus_school_names": focus_school_names,
    }
    enabled_fact_keys = set(get_enabled_fact_keys())
    return {
        key: value
        for key, value in extractor_outputs.items()
        if key in enabled_fact_keys
    }


def _extract_foreign_language(user_input: str) -> str | None:
    languages = ("英语", "日语", "俄语", "德语", "法语", "西班牙语")
    return next((language for language in languages if language in user_input), None)


def _extract_english_exam_score(user_input: str) -> int | None:
    pattern = (
        r"英语(?:大考|考试|模考)?(?:成绩|分数)?"
        r"(?:能考|考了|考到|考|是|为|大概|约|[:：])?\s*(\d{1,3})(?:\s*分)?"
    )
    match = re.search(pattern, user_input)
    if match:
        score = int(match.group(1))
        return score if 0 <= score <= 150 else None
    return None


def _extract_explicit_unknown_fact_keys(user_input: str) -> list[str]:
    unknown_markers = ("目前未知", "未知", "暂时未知", "暂未知", "暂不清楚", "不清楚", "不知道", "未确定", "待确认")
    labeled_fields = {
        "score_recent_avg": ("最近三次大考均分", "最近三次大考平均分", "最近三次模考均分", "最近三次考试均分"),
        "score_total": ("当前分数", "总分", "最近一次模拟考试总分", "高考预估分"),
        "student_province": ("高考省份", "所在省份", "省份"),
        "subject_group": ("选科组合", "选科", "科目组合"),
    }
    normalized = user_input.replace("：", ":")
    detected: list[str] = []
    for fact_key, labels in labeled_fields.items():
        if any(f"{label}:" in normalized for label in labels) and any(marker in normalized for marker in unknown_markers):
            for label in labels:
                segment = normalized.split(f"{label}:", 1)
                if len(segment) != 2:
                    continue
                trailing = segment[1].split("；", 1)[0].split(";", 1)[0].split("\n", 1)[0].strip()
                if any(marker in trailing for marker in unknown_markers):
                    detected.append(fact_key)
                    break
    return sorted(set(detected))


class FactsExtractorSkill(BaseSkill):
    skill_name = "facts_extractor"

    def __init__(self, llm_client=None) -> None:
        self.llm_client = llm_client

    def run(self, user_input: str, context) -> SkillResult:
        admission_assets = load_json("assets/generated/admission/province_score_bands.json", [])
        path_catalog = load_json("assets/generated/multiroute/path_catalog.json", [])
        school_catalog = load_json("assets/generated/school_intro/schools.json", [])
        known_provinces = collect_known_provinces(admission_assets)
        focus_path_ids, focus_primary_categories = extract_focus_targets(
            user_input, path_catalog
        )
        excluded_path_ids, excluded_primary_categories = extract_excluded_targets(
            user_input, path_catalog
        )
        if excluded_path_ids:
            focus_path_ids = [
                path_id for path_id in focus_path_ids if path_id not in set(excluded_path_ids)
            ]
        if excluded_primary_categories:
            excluded_category_set = set(excluded_primary_categories)
            focus_primary_categories = [
                category
                for category in focus_primary_categories
                if category not in excluded_category_set
            ]
        focus_school_names = extract_focus_school_names(user_input, school_catalog)
        if not focus_path_ids and not excluded_path_ids and not excluded_primary_categories:
            recommended_focus_ids, recommended_focus_categories = (
                _infer_focus_from_admission_recommendations(context, user_input)
            )
            if recommended_focus_ids:
                focus_path_ids = recommended_focus_ids
                focus_primary_categories = recommended_focus_categories

        _llm_payload = {
            "user_input": user_input,
            "context": build_context_snapshot(context),
            "known_provinces": known_provinces,
            "path_catalog_brief": [
                {
                    "path_id": item.get("path_id"),
                    "primary_category": item.get("primary_category"),
                    "required_fact_keys": item.get("required_fact_keys", []),
                }
                for item in path_catalog
            ],
            "school_catalog_brief": [
                {"school_name": item.get("school_name")}
                for item in school_catalog[:200]
            ],
        }
        llm_result = safe_complete_json(self.llm_client, "facts_extractor", _llm_payload)
        self._last_prompt_info = build_prompt_record(
            self.skill_name,
            "facts_extractor",
            "facts_extractor",
            _llm_payload,
            llm_response=llm_result,
        )

        fallback_updates = _build_fallback_updates(
            user_input,
            known_provinces=known_provinces,
            focus_path_ids=focus_path_ids,
            focus_primary_categories=focus_primary_categories,
            excluded_path_ids=excluded_path_ids,
            excluded_primary_categories=excluded_primary_categories,
            focus_school_names=focus_school_names,
        )
        explicit_unknown_fact_keys = _extract_explicit_unknown_fact_keys(user_input)
        llm_updates = (llm_result or {}).get("fact_updates", {})
        merged_updates = {
            key: normalize_fact_value(key, value)
            for key, value in llm_updates.items()
        }
        # student_province means the exam-registration province. Do not let a
        # broad model inference turn birthplace or residence into that fact.
        if fallback_updates.get("student_province") in (None, ""):
            merged_updates.pop("student_province", None)
        for key, value in fallback_updates.items():
            if key not in merged_updates or merged_updates.get(key) in (None, "", []):
                merged_updates[key] = normalize_fact_value(key, value)
        excluded_path_id_set = set(merged_updates.get("excluded_path_ids") or [])
        if excluded_path_id_set:
            merged_updates["focus_path_ids"] = [
                path_id
                for path_id in (merged_updates.get("focus_path_ids") or [])
                if path_id not in excluded_path_id_set
            ]
        excluded_category_set = set(merged_updates.get("excluded_primary_categories") or [])
        if excluded_category_set:
            merged_updates["focus_primary_categories"] = [
                category
                for category in (merged_updates.get("focus_primary_categories") or [])
                if category not in excluded_category_set
            ]
        for fact_key in explicit_unknown_fact_keys:
            merged_updates[fact_key] = None

        confidence = (llm_result or {}).get("confidence", 0.72 if any(v for v in fallback_updates.values() if v not in (None, "", [])) else 0.0)
        reason = (llm_result or {}).get("reason", "抽取用户本轮中可写回 context 的事实")

        return SkillResult(
            assistant_message="",
            state_patch={
                "fact_updates": merged_updates,
                "unknown_fact_keys": explicit_unknown_fact_keys,
                "confidence": confidence,
                "reason": reason,
            },
            events=[
                make_event(
                    "facts_extracted",
                    {
                        "fact_keys": [key for key, value in merged_updates.items() if value not in (None, "", [])],
                        "unknown_fact_keys": explicit_unknown_fact_keys,
                        "confidence": confidence,
                    },
                )
            ],
        )
