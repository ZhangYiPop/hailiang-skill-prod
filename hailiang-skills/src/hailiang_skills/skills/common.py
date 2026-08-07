from __future__ import annotations

import json
import os
import re
from typing import Any

from hailiang_skills.llm.prompt_registry import get_prompt_spec, resolve_skill_response_prompt_key
from hailiang_skills.llm.service import safe_complete_text, safe_stream_text


def build_prompt_record(
    skill_name: str,
    phase: str,
    prompt_key: str,
    variables: dict[str, Any],
    llm_response: Any | None = None,
) -> dict[str, Any]:
    spec = get_prompt_spec(prompt_key)
    record = {
        "phase": phase,
        "skill_name": skill_name,
        "prompt_key": prompt_key,
        "prompt_content": spec.content if spec else "",
        "variables": variables,
        "prompt_title": spec.title if spec else "",
        "assembled_from": ["base_template", "skill_config", "facts_context"],
    }
    if llm_response is not None:
        record["llm_response"] = llm_response
    return record


def build_skill_prompt_record(
    skill_name: str,
    phase: str,
    user_input: str,
    context,
    planner_state: dict[str, Any],
    structured_result: dict[str, Any],
    context_candidate_paths: list[dict[str, Any]] | None = None,
    llm_response: Any | None = None,
) -> dict[str, Any]:
    prompt_key = resolve_skill_response_prompt_key(skill_name, None)
    spec = get_prompt_spec(prompt_key)
    context_payload = {
        "known_facts": {key: value.model_dump() for key, value in context.known_facts.facts.items()}
    }
    if context_candidate_paths is not None:
        context_payload["candidate_paths"] = context_candidate_paths
    record = {
        "phase": phase,
        "skill_name": skill_name,
        "prompt_key": prompt_key,
        "prompt_content": spec.content if spec else "",
        "variables": {
            "skill_name": skill_name,
            "prompt_key": prompt_key,
            "user_input": user_input,
            "context": context_payload,
            "planner_state": planner_state,
            "structured_result": structured_result,
        },
        "prompt_title": spec.title if spec else "",
        "assembled_from": ["base_template", "skill_config", "facts_context"],
    }
    if llm_response is not None:
        record["llm_response"] = llm_response
    return record


def extract_score(text: str) -> int | None:
    match = re.search(r"(\d{3})\s*分", text)
    return int(match.group(1)) if match else None


def extract_score_numbers(text: str) -> list[int]:
    return [int(item) for item in re.findall(r"(?<!\d)(\d{3})(?!\d)", text)]


def infer_score_source(text: str) -> str | None:
    if any(
        keyword in text
        for keyword in [
            "最近三次大考",
            "最近三次考试",
            "三次大考",
            "三次模考",
            "模考均分",
            "联考均分",
            "均分",
        ]
    ):
        return "recent_exam_avg"
    if any(
        keyword in text
        for keyword in [
            "预估高考",
            "预估分",
            "高考预估",
            "预计高考",
            "预计分数",
            "估计",
        ]
    ):
        return "estimated_total"
    if "高考总分" in text or "分数" in text or "考了" in text:
        return "reported_total"
    return None


def extract_score_payload(text: str) -> dict[str, Any]:
    score_numbers = extract_score_numbers(text)
    score_source = infer_score_source(text)
    score_total = extract_score(text)
    score_recent_avg = None
    if score_source == "recent_exam_avg":
        if len(score_numbers) >= 3:
            score_recent_avg = round(sum(score_numbers[:3]) / 3)
            score_total = score_recent_avg
        elif score_numbers:
            score_recent_avg = score_numbers[0]
            score_total = score_recent_avg
    elif score_total is None and score_numbers:
        score_total = score_numbers[0]
    return {
        "score_total": score_total,
        "score_recent_avg": score_recent_avg,
        "score_source": score_source,
    }


def extract_grade(text: str) -> str | None:
    grade_patterns = (
        ("小学", "小学"),
        ("初一", "初一"),
        ("七年级", "初一"),
        ("初二", "初二"),
        ("八年级", "初二"),
        ("初三", "初三"),
        ("九年级", "初三"),
        ("初中", "初中"),
        ("高一", "高一"),
        ("高二", "高二"),
        ("高三", "高三"),
        ("高中", "高中"),
    )
    normalized = text.strip()
    if not normalized:
        return None
    for keyword, normalized_grade in grade_patterns:
        if keyword in normalized:
            return normalized_grade
    return None


def normalize_province(value: str) -> str:
    return (
        value.replace("壮族自治区", "")
        .replace("回族自治区", "")
        .replace("维吾尔自治区", "")
        .replace("自治区", "")
        .replace("省", "")
        .replace("市", "")
        .strip()
    )


def collect_known_provinces(admission_assets: list[dict[str, Any]]) -> list[str]:
    provinces = {normalize_province(item.get("province", "")) for item in admission_assets}
    return sorted(item for item in provinces if item)


def extract_province(text: str, known_provinces: list[str]) -> str | None:
    normalized_text = normalize_province(text)
    for province in sorted(known_provinces, key=len, reverse=True):
        if province and province in normalized_text:
            return province
    return None


def extract_explicit_exam_province(text: str, known_provinces: list[str]) -> str | None:
    """Return a province only when the text clearly describes the exam province."""
    normalized_text = normalize_province(text)
    compact_text = re.sub(r"[\s，。！？、；;：:,]", "", normalized_text)
    for province in sorted(known_provinces, key=len, reverse=True):
        normalized_province = normalize_province(province)
        if not normalized_province or normalized_province not in normalized_text:
            continue
        escaped = re.escape(normalized_province)
        explicit_patterns = (
            rf"(?:高考省份|报名省份|高考报名地|参加高考的省份).{{0,12}}{escaped}",
            rf"{escaped}.{{0,6}}(?:高考考生|考生)",
            rf"(?:在|去){escaped}.{{0,6}}(?:参加)?高考",
            rf"高考.{{0,8}}(?:在|去)?{escaped}",
        )
        if any(re.search(pattern, normalized_text) for pattern in explicit_patterns):
            return normalized_province
        if compact_text == normalized_province:
            return normalized_province

        # Keep common compact profile input such as "浙江物理类580分".
        score_values = [int(value) for value in re.findall(r"(?<!\d)(\d{3})(?!\d)", normalized_text)]
        has_total_score = any(value > 150 for value in score_values)
        has_subject_group = any(value in normalized_text for value in ("物理", "历史", "理科", "文科"))
        if has_total_score and has_subject_group:
            return normalized_province
    return None


def normalize_subject_group(value: str | None) -> str | None:
    if not value:
        return None

    text = value.strip()
    physics_keywords = [
        "物理",
        "理科",
        "物理类",
        "物化生",
        "物化地",
        "物化政",
        "物生地",
        "物生政",
    ]
    history_keywords = [
        "历史",
        "文科",
        "历史类",
        "史政地",
        "史政生",
        "史地政",
        "史地化",
        "史化生",
    ]

    if any(keyword in text for keyword in physics_keywords):
        return "物理"
    if any(keyword in text for keyword in history_keywords):
        return "历史"
    return None


def extract_subject_group(text: str) -> str | None:
    return normalize_subject_group(text)


PATH_NAME_ALIASES: dict[str, str] = {
    "综合评价招生": "综合评价",
    "普通高考/e生涯提醒": "普通高考",
    "普通高考路径": "普通高考",
    "综合评价录取": "高职单招",
}
PATH_NAME_DECORATION_PATTERN = re.compile(r"[（(][^）)]*[）)]")


def normalize_path_name(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    return PATH_NAME_ALIASES.get(text, text)


def build_path_name_variants(value: str | None) -> list[str]:
    normalized = normalize_path_name(value)
    if not normalized:
        return []
    candidates = {
        normalized.strip(),
        normalized.replace(" ", "").replace("　", ""),
    }
    stripped = PATH_NAME_DECORATION_PATTERN.sub("", normalized).strip()
    if stripped:
        candidates.add(stripped)
        candidates.add(stripped.replace(" ", "").replace("　", ""))
        alias_stripped = PATH_NAME_ALIASES.get(stripped)
        if alias_stripped:
            candidates.add(alias_stripped)
            candidates.add(alias_stripped.replace(" ", "").replace("　", ""))
    return sorted(candidate for candidate in candidates if candidate)


def path_name_matches(query: str | None, candidate: str | None) -> bool:
    query_variants = build_path_name_variants(query)
    candidate_variants = build_path_name_variants(candidate)
    if not query_variants or not candidate_variants:
        return False
    if set(query_variants) & set(candidate_variants):
        return True
    for left in query_variants:
        for right in candidate_variants:
            if len(left) >= 2 and len(right) >= 2 and (left in right or right in left):
                return True
    return False


def find_catalog_path_matches(
    query: str | None,
    path_catalog: list[dict[str, Any]],
    student_province: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    if not query:
        return []
    normalized_query = normalize_path_name(query) or query
    query_variants = set(build_path_name_variants(normalized_query))
    scored_matches: list[tuple[int, dict[str, Any]]] = []
    for item in path_catalog:
        category = item.get("primary_category", "")
        category_variants = set(build_path_name_variants(category))
        if not category_variants:
            continue
        overlap = query_variants & category_variants
        contains = any(
            len(left) >= 2 and len(right) >= 2 and (left in right or right in left)
            for left in query_variants
            for right in category_variants
        )
        if not overlap and not contains:
            continue
        score = 0
        if normalized_query == category:
            score += 6
        if overlap:
            score += 4
        if contains:
            score += 2
        province_candidates = item.get("geo_constraints", {}).get("provinces", []) or []
        if student_province and (
            student_province in province_candidates or student_province in category
        ):
            score += 3
        scored_matches.append((score, item))
    scored_matches.sort(
        key=lambda pair: (
            -pair[0],
            pair[1].get("path_id", ""),
            pair[1].get("primary_category", ""),
        )
    )
    return [item for _, item in scored_matches[:limit]]


def extract_student_region(text: str) -> str | None:
    patterns = [
        r"(?:户籍(?:在|地|所在地)?|来自|是)([一-龥]{2,20}(?:市|州|县|区|旗))",
        r"([一-龥]{2,20}(?:市|州|县|区|旗))考生",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def infer_family_type(text: str) -> str | None:
    if any(keyword in text for keyword in ["不是建档立卡", "非建档立卡", "不属于建档立卡"]):
        return "非建档立卡户"
    if "建档立卡" in text:
        return "原建档立卡户"
    return None


def extract_ethnicity(text: str) -> str | None:
    match = re.search(r"([一-龥]{1,8}族)", text)
    if match:
        return match.group(1)
    if "少数民族" in text:
        return "少数民族"
    return None


def normalize_budget_level(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = (
        text.lower()
        .replace(" ", "")
        .replace("元", "")
        .replace("/年", "")
        .replace("每年", "")
        .replace("每学年", "")
        .replace("／年", "")
    )
    if lowered in {
        "high",
        ">5万",
        "大于5万",
        "5万以上",
        "5万元以上",
        "高于5万",
        ">=5万",
        "＞5万",
        "5w以上",
    }:
        return ">5万"
    if lowered in {
        "low",
        "<5万",
        "小于5万",
        "5万以下",
        "5万元以下",
        "低于5万",
        "<=5万",
        "＜5万",
        "5w以下",
    }:
        return "<5万"
    return text


def normalize_exam_qualification_status(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower().replace(" ", "")
    if lowered in {"qualified", "合格", "通过", "已通过"}:
        return "合格"
    if lowered in {"unqualified", "不合格", "未通过", "没过"}:
        return "不合格"
    return text


def normalize_career_orientation_value(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("职业兴趣", "").replace("方向", "").replace("职业", "").strip("：:；;,， ")
    if any(keyword in normalized for keyword in ["军警", "军校", "警校", "公安", "司法警官"]):
        return "军警类"
    if any(keyword in normalized for keyword in ["师范", "教师", "从教", "教育"]):
        return "师范类"
    if any(keyword in normalized for keyword in ["飞行", "招飞", "飞行员", "民航", "空军", "海军招飞"]):
        return "飞行员"
    if normalized in {"其他", "其它"}:
        return "其他"
    return normalized


def normalize_career_orientation_values(values: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if values is None:
        return []
    items = values if isinstance(values, (list, tuple)) else [values]
    normalized: list[str] = []
    for item in items:
        canonical = normalize_career_orientation_value(str(item))
        if canonical and canonical not in normalized:
            normalized.append(canonical)
    return normalized


def infer_budget_level(text: str) -> str | None:
    if any(
        keyword in text
        for keyword in [
            "预算高",
            "预算充足",
            "学费没问题",
            "20万",
            "10万",
            "15万",
            "民办",
            "大于5万",
            ">5万",
            "5万以上",
            "5万元以上",
            ">5万/年",
            "5万/年以上",
        ]
    ):
        return ">5万"
    if any(
        keyword in text
        for keyword in [
            "预算有限",
            "便宜",
            "性价比",
            "公办优先",
            "小于5万",
            "<5万",
            "5万以下",
            "5万元以下",
            "<5万/年",
            "5万/年以下",
        ]
    ):
        return "<5万"
    return None


def infer_termination_preference(text: str) -> str | None:
    if any(keyword in text for keyword in ["直接推荐", "先这样", "别问了", "不补充"]):
        return "direct_recommend"
    return None


def infer_hukou_years(text: str) -> str | None:
    if "连续3年户籍" in text or "三年户籍" in text:
        return "3_years"
    return None


def infer_guardian_hukou_match(text: str) -> bool | None:
    if any(keyword in text for keyword in ["监护人户籍一致", "父母户籍一致", "监护人同户籍"]):
        return True
    if any(keyword in text for keyword in ["监护人户籍不一致", "父母户籍不一致"]):
        return False
    return None


def infer_school_status_years(text: str) -> str | None:
    if "连续3年学籍" in text or "三年学籍" in text:
        return "3_years"
    return None


def infer_exam_qualification_status(text: str) -> str | None:
    if any(keyword in text for keyword in ["学考不合格", "学考有不合格", "学考没过", "学考未通过"]):
        return "不合格"
    if any(
        keyword in text
        for keyword in ["学考合格", "学考都合格", "学考全部合格", "学考通过", "学考已通过"]
    ):
        return "合格"
    return None


def extract_special_identity_tags(text: str) -> list[str]:
    keywords = [
        "国家集训队",
        "学科竞赛",
        "省级奖项",
        "竞赛奖项",
        "外国语中学",
        "公安英烈子女",
        "退役运动员",
    ]
    return [keyword for keyword in keywords if keyword in text]


def extract_career_orientation(text: str) -> list[str]:
    orientation_map = {
        "师范类": ["师范", "教师", "从教", "教育"],
        "医学": ["医学", "临床", "医生", "医护"],
        "工科": ["工科", "工程", "制造"],
        "军警类": ["军警", "军校", "警校", "公安", "司法警官"],
        "飞行员": ["飞行员", "飞行", "招飞", "民航", "空军招飞", "海军招飞"],
        "体制内": ["体制内", "考编", "编制"],
        "财经": ["财经", "金融", "会计"],
        "语言": ["语言", "外语", "小语种"],
        "计算机": ["计算机", "编程", "软件"],
        "文旅": ["文旅", "旅游", "文化产业"],
    }
    detected: list[str] = []
    for canonical, keywords in orientation_map.items():
        if any(keyword in text for keyword in keywords) and canonical not in detected:
            detected.append(canonical)
    return detected


def extract_focus_school_names(
    text: str, school_catalog: list[dict[str, Any]]
) -> list[str]:
    school_names: list[str] = []
    lowered = text.strip()
    for item in school_catalog:
        school_name = item.get("school_name", "")
        if school_name and school_name in lowered:
            school_names.append(school_name)
    return sorted(set(school_names))


def extract_focus_targets(
    text: str, path_catalog: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    focus_path_ids: list[str] = []
    focus_primary_categories: list[str] = []
    lowered = text.strip()
    for item in path_catalog:
        path_id = item.get("path_id", "")
        category = item.get("primary_category", "")
        if path_id and path_id in lowered:
            focus_path_ids.append(path_id)
        if category and any(variant in lowered for variant in build_path_name_variants(category)):
            focus_primary_categories.append(category)
    return sorted(set(focus_path_ids)), sorted(set(focus_primary_categories))


ALTERNATIVE_INTENT_KEYWORDS = [
    "除了",
    "除开",
    "除去",
    "排除",
    "不考虑",
    "先不看",
    "不看",
    "不走",
]

ALTERNATIVE_QUERY_KEYWORDS = [
    "还有什么路",
    "还有哪些路",
    "还有什么路径",
    "还有哪些路径",
    "别的路径",
    "其他路径",
    "其他升学路径",
    "其他升学方式",
    "别的升学方式",
    "别的方案",
    "其他方案",
]


def has_alternative_exploration_intent(text: str) -> bool:
    lowered = text.strip()
    has_exclusion_marker = any(keyword in lowered for keyword in ALTERNATIVE_INTENT_KEYWORDS)
    has_alternative_query = any(keyword in lowered for keyword in ALTERNATIVE_QUERY_KEYWORDS)
    if has_exclusion_marker and has_alternative_query:
        return True
    if "之外" in lowered and has_alternative_query:
        return True
    return False


def extract_excluded_targets(
    text: str, path_catalog: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    if not has_alternative_exploration_intent(text):
        return [], []

    excluded_path_ids: list[str] = []
    excluded_primary_categories: list[str] = []
    lowered = text.strip()
    for item in path_catalog:
        path_id = item.get("path_id", "")
        category = item.get("primary_category", "")
        if path_id and path_id in lowered:
            excluded_path_ids.append(path_id)
        if category and any(variant in lowered for variant in build_path_name_variants(category)):
            excluded_primary_categories.append(category)
    return sorted(set(excluded_path_ids)), sorted(set(excluded_primary_categories))


def llm_compose_reply(
    client,
    skill_name: str,
    user_input: str,
    context,
    planner_state: dict[str, Any],
    structured_result: dict[str, Any],
    context_candidate_paths: list[dict[str, Any]] | None = None,
    candidate_paths_source: str | None = None,
    prompt_key: str | None = None,
) -> str | None:
    context_payload = {
        "known_facts": {
            key: value.model_dump() for key, value in context.known_facts.facts.items()
        },
    }
    if context_candidate_paths is not None:
        context_payload["candidate_paths"] = context_candidate_paths
        context_payload["candidate_paths_source"] = candidate_paths_source or "unspecified"

    resolved_prompt_key = resolve_skill_response_prompt_key(skill_name, prompt_key)
    llm_payload = {
        "skill_name": skill_name,
        "prompt_key": resolved_prompt_key,
        "user_input": user_input,
        "context": context_payload,
        "planner_state": planner_state,
        "structured_result": structured_result,
    }
    stream_enabled = bool((context.session_meta or {}).get("stream_final_reply"))
    delta_callback = (context.session_meta or {}).get("reply_delta_callback")
    if stream_enabled and callable(delta_callback):
        chunks: list[str] = []
        for chunk in safe_stream_text(client, resolved_prompt_key, llm_payload):
            if not chunk:
                continue
            chunks.append(chunk)
            delta_callback(chunk)
        return "".join(chunks) if chunks else None

    return safe_complete_text(client, resolved_prompt_key, llm_payload)


SUMMARY_STYLE_ENUM = {"xiaohongshu", "planner", "counselor", "minimal"}
SUMMARY_STYLE_DEFAULTS = {
    "path_drilldown": "xiaohongshu",
    "convergence": "planner",
    "admission": "counselor",
    "school_intro": "minimal",
    "terminate_or_recommend": "counselor",
}


def resolve_summary_style(skill_name: str, override: str | None = None) -> str:
    def _normalize(raw_value: str | None) -> str | None:
        if not raw_value:
            return None
        value = raw_value.strip().lower()
        aliases = {
            "xhs": "xiaohongshu",
            "consultant": "counselor",
            "advisor": "counselor",
            "plan": "planner",
            "simple": "minimal",
        }
        value = aliases.get(value, value)
        return value if value in SUMMARY_STYLE_ENUM else None

    skill_env_key = f"HAILIANG_SUMMARY_STYLE_{skill_name.upper()}"
    return (
        _normalize(override)
        or _normalize(os.getenv(skill_env_key))
        or _normalize(os.getenv("HAILIANG_SUMMARY_STYLE_DEFAULT"))
        or SUMMARY_STYLE_DEFAULTS.get(skill_name, "counselor")
    )


def llm_polish_structured_reply(
    client,
    *,
    skill_name: str,
    user_input: str,
    draft_reply: str,
    context,
    planner_state: dict[str, Any],
    structured_result: dict[str, Any],
    style: str | None = None,
) -> str | None:
    if not draft_reply.strip():
        return None
    resolved_style = resolve_summary_style(skill_name, style)
    llm_payload = {
        "skill_name": skill_name,
        "style": resolved_style,
        "user_input": user_input,
        "draft_reply": draft_reply,
        "context": {
            "known_facts": {
                key: value.model_dump() for key, value in context.known_facts.facts.items()
            },
        },
        "planner_state": planner_state,
        "structured_result": structured_result,
    }
    stream_enabled = bool((context.session_meta or {}).get("stream_final_reply"))
    delta_callback = (context.session_meta or {}).get("reply_delta_callback")
    if stream_enabled and callable(delta_callback):
        chunks: list[str] = []
        for chunk in safe_stream_text(client, "structured_summary_polish", llm_payload):
            if not chunk:
                continue
            chunks.append(chunk)
            delta_callback(chunk)
        return "".join(chunks) if chunks else None

    return safe_complete_text(client, "structured_summary_polish", llm_payload)


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)
