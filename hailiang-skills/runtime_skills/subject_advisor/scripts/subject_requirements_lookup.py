from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ASSET_PATH = Path(__file__).resolve().parents[1] / "assets" / "subject_selection" / "subject_requirements.json"
SUBJECTS = ("物理", "历史", "化学", "生物", "政治", "地理", "技术")
SUBJECT_ABBREVIATIONS = {
    "物": "物理",
    "史": "历史",
    "化": "化学",
    "生": "生物",
    "政": "政治",
    "地": "地理",
    "技": "技术",
}
DEFAULT_LIMIT = 12


def main() -> None:
    parser = argparse.ArgumentParser(description="Lookup major subject requirements for subject advisor.")
    parser.add_argument("--query", default="", help="Natural language query.")
    parser.add_argument("--major", default="", help="Major name or major category.")
    parser.add_argument("--career", default="", help="Career target, for example 医生/程序员/律师/警察.")
    parser.add_argument(
        "--subjects",
        "--selected-subjects",
        dest="subjects",
        default="",
        help="Selected subjects, separated by comma or Chinese punctuation.",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    args = parser.parse_args()

    stdin_payload = _read_stdin_payload()
    payload = {
        "query": args.query,
        "major": args.major,
        "career": args.career,
        "subjects": args.subjects,
        "limit": args.limit,
    }
    payload.update({key: value for key, value in stdin_payload.items() if value not in (None, "")})
    result = lookup(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def lookup(payload: dict[str, Any]) -> dict[str, Any]:
    asset = _load_asset()
    query = str(payload.get("query") or "").strip()
    major = str(payload.get("major") or "").strip()
    career = str(payload.get("career") or "").strip()
    limit = _positive_int(payload.get("limit"), DEFAULT_LIMIT)
    subject_combo = _extract_subjects(payload.get("subjects"), query)

    target = _infer_target(asset, query=query, major=major, career=career)
    records = asset.get("records", [])
    matched_records = _match_records(records, target)
    query_type = _query_type(target=target, selected_subjects=subject_combo)

    if query_type == "subject_combo_coverage":
        return _build_combo_coverage_result(asset, query=query, selected_subjects=subject_combo, limit=limit)

    if not matched_records and subject_combo:
        return _build_combo_coverage_result(asset, query=query, selected_subjects=subject_combo, limit=limit)

    result_records = [
        _format_record(record, selected_subjects=subject_combo)
        for record in matched_records[: max(limit, 1)]
    ]
    compatibility_counter = Counter(item["compatibility"] for item in result_records)
    return {
        "ok": True,
        "asset_id": asset.get("asset_id"),
        "query_type": query_type,
        "input": {
            "query": query,
            "major": major,
            "career": career,
            "selected_subjects": subject_combo,
        },
        "matched_target": target,
        "total_matches": len(matched_records),
        "returned": len(result_records),
        "compatibility_summary": dict(compatibility_counter),
        "results": result_records,
        "warnings": _warnings(asset),
        "interpretation_hints": _interpretation_hints(query_type, selected_subjects=subject_combo),
    }


def _read_stdin_payload() -> dict[str, Any]:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"query": raw, "_stdin_error": f"stdin JSON parse failed: {exc.msg}"}
    return payload if isinstance(payload, dict) else {}


def _load_asset() -> dict[str, Any]:
    if not ASSET_PATH.is_file():
        raise FileNotFoundError(f"缺少结构化选科资产，请先运行 build_subject_requirement_asset.py: {ASSET_PATH}")
    return json.loads(ASSET_PATH.read_text(encoding="utf-8"))


def _infer_target(asset: dict[str, Any], *, query: str, major: str, career: str) -> dict[str, Any]:
    text = " ".join(item for item in (query, major, career) if item)
    categories: list[str] = []
    keywords: list[str] = []
    careers: list[str] = []
    directions: list[str] = []

    if major:
        keywords.append(major)
        categories.append(major if major.endswith("类") else f"{major}类")

    for item in asset.get("career_aliases", []):
        aliases = [str(value) for value in item.get("aliases", [])]
        if career == item.get("career") or any(alias and alias in text for alias in aliases):
            careers.append(str(item.get("career") or ""))
            categories.extend(str(value) for value in item.get("major_categories", []))
            keywords.extend(str(value) for value in item.get("major_keywords", []))

    for item in asset.get("broad_direction_aliases", []):
        aliases = [str(value) for value in item.get("aliases", [])]
        if any(alias and alias in text for alias in aliases):
            directions.append(str(item.get("direction") or ""))
            categories.extend(str(value) for value in item.get("major_categories", []))

    records = asset.get("records", [])
    for record in records:
        category = str(record.get("major_category") or "")
        name = str(record.get("major_name") or "")
        if category and category in text:
            categories.append(category)
        if category.endswith("类") and category[:-1] and category[:-1] in text:
            categories.append(category)
        if name and name in text:
            keywords.append(name)

    return {
        "careers": _unique(careers),
        "directions": _unique(directions),
        "major_categories": _unique(categories),
        "major_keywords": _unique(keywords),
    }


def _match_records(records: list[dict[str, Any]], target: dict[str, Any]) -> list[dict[str, Any]]:
    categories = tuple(target.get("major_categories", []))
    keywords = tuple(target.get("major_keywords", []))
    if not categories and not keywords:
        return []

    scored: list[tuple[int, str, dict[str, Any]]] = []
    for record in records:
        category = str(record.get("major_category") or "")
        name = str(record.get("major_name") or "")
        score = 0
        if category in categories:
            score += 100
            if categories and category == categories[0]:
                score += 25
        if any(category and (category in item or item in category) for item in categories):
            score += 30
        for keyword in keywords:
            if not keyword:
                continue
            if keyword == name:
                score += 120
            elif keyword in name or keyword in category:
                score += 45
        if score <= 0:
            continue
        scored.append((score, f"{category}/{name}", record))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored]


def _query_type(*, target: dict[str, Any], selected_subjects: list[str]) -> str:
    if selected_subjects and not target.get("major_categories") and not target.get("major_keywords"):
        return "subject_combo_coverage"
    if target.get("careers"):
        return "career_requirement"
    if target.get("major_categories") or target.get("major_keywords"):
        return "major_requirement"
    return "unknown"


def _build_combo_coverage_result(
    asset: dict[str, Any],
    *,
    query: str,
    selected_subjects: list[str],
    limit: int,
) -> dict[str, Any]:
    records = asset.get("records", [])
    eligible = []
    unknown = []
    for record in records:
        compatibility = _compatibility(record, selected_subjects)
        if compatibility["status"] == "eligible":
            eligible.append(record)
        elif compatibility["status"] == "unknown_no_source":
            unknown.append(record)

    category_counter = Counter(str(item.get("major_category") or "") for item in eligible)
    requirement_counter = Counter(str(item.get("minimum_requirement") or "") for item in eligible)
    examples_by_category: dict[str, list[str]] = defaultdict(list)
    for record in eligible:
        category = str(record.get("major_category") or "")
        if len(examples_by_category[category]) < 5:
            examples_by_category[category].append(str(record.get("major_name") or ""))

    top_categories = [
        {
            "major_category": category,
            "eligible_count": count,
            "sample_majors": examples_by_category.get(category, []),
        }
        for category, count in category_counter.most_common(max(limit, 1))
    ]
    return {
        "ok": True,
        "asset_id": asset.get("asset_id"),
        "query_type": "subject_combo_coverage",
        "input": {
            "query": query,
            "selected_subjects": selected_subjects,
        },
        "eligible_count": len(eligible),
        "unknown_requirement_count": len(unknown),
        "top_major_categories": top_categories,
        "requirement_summary": dict(requirement_counter.most_common()),
        "warnings": _warnings(asset),
        "interpretation_hints": _interpretation_hints("subject_combo_coverage", selected_subjects=selected_subjects),
    }


def _format_record(record: dict[str, Any], *, selected_subjects: list[str]) -> dict[str, Any]:
    compatibility = _compatibility(record, selected_subjects)
    return {
        "id": record.get("id"),
        "major_category": record.get("major_category"),
        "major_name": record.get("major_name"),
        "minimum_requirement": record.get("minimum_requirement"),
        "required_subject_groups": record.get("required_subject_groups", []),
        "recommended_subjects": record.get("recommended_subjects", []),
        "subject_scope_one": record.get("subject_scope_one"),
        "subject_scope_two_required": record.get("subject_scope_two_required"),
        "subject_scope_two_recommended": record.get("subject_scope_two_recommended"),
        "compatibility": compatibility["status"],
        "missing_subjects": compatibility["missing_subjects"],
        "source_row_number": record.get("source_row_number"),
    }


def _compatibility(record: dict[str, Any], selected_subjects: list[str]) -> dict[str, Any]:
    status = str(record.get("requirement_status") or "")
    groups = record.get("required_subject_groups", [])
    if status == "missing_in_source":
        return {"status": "unknown_no_source", "missing_subjects": []}
    if not selected_subjects:
        return {"status": "no_subject_combo", "missing_subjects": []}
    if status == "no_limit":
        return {"status": "eligible", "missing_subjects": []}

    selected = set(selected_subjects)
    missing_options = []
    for group in groups:
        required = set(str(item) for item in group)
        missing = sorted(required - selected)
        if not missing:
            return {"status": "eligible", "missing_subjects": []}
        missing_options.append(missing)
    best_missing = min(missing_options, key=len) if missing_options else []
    return {"status": "not_eligible", "missing_subjects": best_missing}


def _extract_subjects(raw_subjects: Any, query: str) -> list[str]:
    subjects: list[str] = []
    if isinstance(raw_subjects, list):
        source_text = "、".join(str(item) for item in raw_subjects)
    else:
        source_text = str(raw_subjects or "")
    text = f"{source_text} {query}"

    compact = re.sub(r"\s+", "", text)
    for pattern in re.findall(r"[物史化生政地技]{2,6}", compact):
        for char in pattern:
            subject = SUBJECT_ABBREVIATIONS.get(char)
            if subject and subject not in subjects:
                subjects.append(subject)

    for subject in SUBJECTS:
        if subject in text and subject not in subjects:
            subjects.append(subject)

    # “只选物理”这类表达表示用户当前组合只有物理；如果没有其他科目线索，保留单科判断。
    if "只选物理" in text or "只有物理" in text:
        return ["物理"]
    return subjects


def _warnings(asset: dict[str, Any]) -> list[str]:
    boundaries = asset.get("usage_boundaries", [])
    return [str(item) for item in boundaries[:5]]


def _interpretation_hints(query_type: str, *, selected_subjects: list[str]) -> list[str]:
    hints = [
        "模型负责顾问解释，结构化资产只提供最低选科要求依据。",
        "面向用户输出时不要展示内部 id；可以说“按当前通用指引”。",
    ]
    if query_type == "career_requirement":
        hints.append("职业目标需要先转成专业或专业类，再解释对应选科要求。")
    if query_type == "subject_combo_coverage":
        hints.append("组合覆盖结果适合概括专业大类和风险，不适合一次性罗列全部专业。")
    if selected_subjects:
        hints.append("如 compatibility=not_eligible，应明确说明缺少哪些科目。")
    return hints


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _unique(items: list[str]) -> list[str]:
    result = []
    for item in items:
        stripped = str(item or "").strip()
        if stripped and stripped not in result:
            result.append(stripped)
    return result


if __name__ == "__main__":
    main()
