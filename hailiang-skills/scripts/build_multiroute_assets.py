from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from xlsx_utils import load_workbook_rows


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_XLSX = PROJECT_ROOT / "docs" / "多元升学路径" / "最新人路规则（13+内蒙古）更至20260520.xlsx"
TARGET_DIR = PROJECT_ROOT / "assets" / "generated" / "multiroute"

SUBJECT_LABELS = {"物理", "历史"}
FACT_KEYWORDS = {
    "student_province": ["省份", "所在省份"],
    "student_region": ["户籍", "户籍地", "户籍所在地", "学生户籍地", "学生户籍所在地"],
    "subject_group": ["首选科目", "物理", "历史", "物理组", "历史组", "物理类", "历史类"],
    "score_total": ["成绩", "分数", "总成绩", "均分"],
    "exam_qualification_status": ["学考", "学考是否合格"],
    "family_type": ["建档立卡", "脱贫家庭"],
    "ethnicity": ["少数民族", "民族"],
    "budget_level": ["教育预期投入", "学费", "预算"],
    "special_identity_tags": ["竞赛", "国家集训队", "省级及以上奖项", "奖项", "英烈子女", "退役运动员"],
    "school_status_years": ["学籍"],
    "career_orientation": ["职业兴趣", "专业倾向", "体制内"],
    "physical_requirements": ["身高", "身长", "高度"],
}


def _pad_row(row: list[str], size: int) -> list[str]:
    return row + [""] * max(0, size - len(row))


def _extract_required_fact_keys(text: str) -> list[str]:
    required: list[str] = []
    for fact_key, keywords in FACT_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            required.append(fact_key)
    return sorted(set(required))


def _extract_subject_constraints(text: str) -> list[str]:
    subjects: list[str] = []
    if "首选科目：物理" in text or "物理组" in text or "物理类" in text:
        subjects.append("物理")
    if "首选科目：历史" in text or "历史组" in text or "历史类" in text:
        subjects.append("历史")
    return sorted(set(subjects))


def _extract_geo_constraints(text: str, province_names: list[str]) -> dict:
    provinces = sorted({province for province in province_names if province and province in text})
    regions = sorted(
        {
            match.group(1)
            for match in re.finditer(r"([一-龥]{2,20}(?:市|州|县|区|旗))", text)
        }
    )
    return {
        "provinces": provinces,
        "regions": regions,
    }


def _build_rule_variant(
    path_id: str,
    primary_category: str,
    record: dict[str, str],
    sheet_name: str,
    province_names: list[str],
    variant_index: int,
) -> dict:
    rule_text_merged = record.get("路径规则（合并）", "").strip()
    rule_text_by_province = record.get("路径规则（含分省 — 具体分数为2025年公布数据）", "").strip()
    rule_text_raw = (
        rule_text_by_province
        or rule_text_merged
        or record.get("路径规则", "").strip()
    )
    remark = record.get("备注", "").strip()
    geo_constraints = _extract_geo_constraints(rule_text_raw, province_names)
    return {
        "variant_id": f"{path_id}:{variant_index}",
        "path_id": path_id,
        "primary_category": primary_category,
        "sheet_group": sheet_name,
        "remark": remark,
        "rule_text_raw": rule_text_raw,
        "rule_text_merged": rule_text_merged,
        "rule_text_by_province": rule_text_by_province,
        "required_fact_keys": _extract_required_fact_keys(rule_text_raw),
        "subject_constraints": _extract_subject_constraints(rule_text_raw),
        "geo_constraints": geo_constraints,
    }


def parse_path_catalog(
    rows_by_sheet: dict[str, list[list[str]]], province_names: list[str]
) -> list[dict]:
    items: list[dict] = []
    for sheet_name, rows in rows_by_sheet.items():
        if not re.match(r"^\d+\.", sheet_name):
            continue
        headers = rows[0] if rows else []
        current_item: dict | None = None
        current_variant_index = 0
        for row in rows[1:]:
            if not row or not any(cell.strip() for cell in row):
                continue
            record = dict(zip(headers, _pad_row(row, len(headers))))
            explicit_path_id = record.get("路径ID", "").strip()
            explicit_category = record.get("一级升学大类", "").strip()
            path_id = explicit_path_id or (current_item or {}).get("path_id", "")
            primary_category = explicit_category or (current_item or {}).get("primary_category", "")
            if not path_id or not primary_category:
                continue

            should_start_new_item = (
                current_item is None
                or bool(explicit_path_id)
                or bool(explicit_category and explicit_category != current_item.get("primary_category"))
            )
            if should_start_new_item:
                current_variant_index = 0
                current_item = {
                    "path_id": path_id,
                    "primary_category": primary_category,
                    "sheet_group": sheet_name,
                    "rule_text_raw": "",
                    "rule_text_merged": "",
                    "rule_text_by_province": "",
                    "rule_expr_normalized": "",
                    "description": record.get("路径介绍", "").strip(),
                    "features": record.get("路径特色", "").strip(),
                    "target_users": record.get("适用对象", "").strip(),
                    "process_flow": record.get("相关流程", "").strip(),
                    "raw_payload": record,
                    "rule_variants": [],
                    "required_fact_keys": [],
                    "subject_constraints": [],
                    "geo_constraints": {"provinces": [], "regions": []},
                }
                items.append(current_item)

            current_variant_index += 1
            variant = _build_rule_variant(
                path_id, primary_category, record, sheet_name, province_names, current_variant_index
            )
            current_item["rule_variants"].append(variant)

            if variant["rule_text_raw"]:
                if current_item["rule_text_raw"]:
                    current_item["rule_text_raw"] += "\n---\n" + variant["rule_text_raw"]
                else:
                    current_item["rule_text_raw"] = variant["rule_text_raw"]
            if variant["rule_text_merged"] and not current_item["rule_text_merged"]:
                current_item["rule_text_merged"] = variant["rule_text_merged"]
            if variant["rule_text_by_province"]:
                current_item["rule_text_by_province"] = (
                    f"{current_item['rule_text_by_province']}\n---\n{variant['rule_text_by_province']}".strip("-\n ")
                    if current_item["rule_text_by_province"]
                    else variant["rule_text_by_province"]
                )

            current_item["required_fact_keys"] = sorted(
                set(current_item["required_fact_keys"]) | set(variant["required_fact_keys"])
            )
            current_item["subject_constraints"] = sorted(
                set(current_item["subject_constraints"]) | set(variant["subject_constraints"])
            )
            current_item["geo_constraints"]["provinces"] = sorted(
                set(current_item["geo_constraints"]["provinces"])
                | set(variant["geo_constraints"]["provinces"])
            )
            current_item["geo_constraints"]["regions"] = sorted(
                set(current_item["geo_constraints"]["regions"])
                | set(variant["geo_constraints"]["regions"])
            )

    return [item for item in items if item["path_id"]]


def parse_reason_templates(rows: list[list[str]]) -> list[dict]:
    items: list[dict] = []
    for row in rows:
        if len(row) < 9 or not re.match(r"^\d{4}$", row[0]):
            continue
        items.append(
            {
                "path_id": row[0],
                "primary_category": row[1],
                "match_reason": row[2],
                "mismatch_reason": row[3],
                "risk_hint": row[4],
                "recommended_visibility": " / ".join(item for item in row[5:8] if item),
                "rule_text_raw": row[8] if len(row) > 8 else "",
            }
        )
    return items


def parse_score_band_rows(rows: list[list[str]]) -> list[dict]:
    header = rows[0] if rows else []
    items = []
    for row in rows[1:]:
        if not row or not any(cell.strip() for cell in row):
            continue
        items.append(dict(zip(header, row)))
    return items


def parse_action_timeline(rows: list[list[str]]) -> list[dict]:
    if not rows:
        return []
    header = rows[0]
    items = []
    current_grade = ""
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        time_label = row[0].strip()
        if time_label in {"高一", "高二", "高三"}:
            current_grade = time_label
            continue
        actions = {}
        for idx, value in enumerate(row[1:], start=1):
            if idx < len(header) and value.strip():
                actions[header[idx]] = value.strip()
        items.append(
            {
                "grade": current_grade or None,
                "time_label": time_label,
                "actions": actions,
            }
        )
    return items


def parse_province_score_lines(rows: list[list[str]]) -> list[dict]:
    if len(rows) < 3:
        return []
    header_top = _pad_row(rows[0], max(len(row) for row in rows[:2]))
    header_sub = _pad_row(rows[1], len(header_top))

    column_info: list[tuple[str, str | None]] = []
    current_top = ""
    for idx, top in enumerate(header_top):
        if top.strip():
            current_top = top.strip()
        sub = header_sub[idx].strip() if idx < len(header_sub) else ""
        subject_group = sub if sub in SUBJECT_LABELS else None
        column_info.append((current_top, subject_group))

    items: list[dict] = []
    for row in rows[2:]:
        padded = _pad_row(row, len(column_info))
        if not any(cell.strip() for cell in padded):
            continue
        province = padded[0].strip()
        mode = padded[2].strip() if len(padded) > 2 else ""
        if not province or not mode:
            continue

        base_payload = {
            "省份": province,
            "高考总分": padded[1].strip() if len(padded) > 1 else "",
            "模式": mode,
        }
        if mode == "3+1+2":
            for subject_group in ["物理", "历史"]:
                entry = {
                    **base_payload,
                    "选科": subject_group,
                    "特控线/一本线": "",
                    "一段线/本科线": "",
                    "二段线/专科线": "",
                    "艺术类本科线": "",
                    "体育类本科线": "",
                }
                for idx, value in enumerate(padded):
                    header_name, header_subject = column_info[idx]
                    if header_name in entry and header_subject == subject_group:
                        entry[header_name] = value.strip()
                if any(entry[key] for key in ["特控线/一本线", "一段线/本科线", "二段线/专科线"]):
                    items.append(entry)
        else:
            entry = {
                **base_payload,
                "选科": None,
                "特控线/一本线": "",
                "一段线/本科线": "",
                "二段线/专科线": "",
                "艺术类本科线": "",
                "体育类本科线": "",
            }
            for idx, value in enumerate(padded):
                header_name, _ = column_info[idx]
                if header_name in entry and header_name not in {"省份", "高考总分", "模式"}:
                    cell_value = value.strip()
                    if cell_value:
                        entry[header_name] = cell_value
            items.append(entry)
    return items


def parse_question_bank(rows: list[list[str]]) -> list[dict]:
    if not rows:
        return []
    header = rows[0]
    items = []
    for row in rows[1:]:
        padded = _pad_row(row, len(header))
        if not any(cell.strip() for cell in padded):
            continue
        items.append(dict(zip(header, padded)))
    return items


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    rows_by_sheet = load_workbook_rows(SOURCE_XLSX)
    province_names = sorted(
        {
            row[0].strip()
            for row in rows_by_sheet.get("全国高考分数线", [])[2:]
            if row and row[0].strip()
        }
    )

    path_catalog = parse_path_catalog(rows_by_sheet, province_names)
    reason_templates = parse_reason_templates(rows_by_sheet.get("推荐理由（记得同步）", []))
    score_band_rules = parse_score_band_rows(rows_by_sheet.get("成绩分类展示", []))
    action_timeline = parse_action_timeline(rows_by_sheet.get("行动计划", []))
    province_score_lines = parse_province_score_lines(rows_by_sheet.get("全国高考分数线", []))
    question_bank = parse_question_bank(rows_by_sheet.get("问题", []))

    source_hash = hashlib.sha256(SOURCE_XLSX.read_bytes()).hexdigest()
    manifest = {
        "source_file": str(SOURCE_XLSX.relative_to(PROJECT_ROOT)),
        "source_hash": source_hash,
        "sheet_names": list(rows_by_sheet.keys()),
        "counts": {
            "path_catalog": len(path_catalog),
            "reason_templates": len(reason_templates),
            "score_band_rules": len(score_band_rules),
            "action_timeline": len(action_timeline),
            "province_score_lines": len(province_score_lines),
            "question_bank": len(question_bank),
        },
    }

    write_json(TARGET_DIR / "path_catalog.json", path_catalog)
    write_json(TARGET_DIR / "path_reason_templates.json", reason_templates)
    write_json(TARGET_DIR / "score_band_exposure_rules.json", score_band_rules)
    write_json(TARGET_DIR / "action_timeline_templates.json", action_timeline)
    write_json(TARGET_DIR / "province_score_lines.json", province_score_lines)
    write_json(TARGET_DIR / "question_bank.json", question_bank)
    write_json(TARGET_DIR / "asset_manifest.json", manifest)


if __name__ == "__main__":
    main()
