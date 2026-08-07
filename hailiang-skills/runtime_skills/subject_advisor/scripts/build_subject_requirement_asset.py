from __future__ import annotations

import argparse
import itertools
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_SOURCE = "docs/选科规划/2021年全国选科通用指引-20260611.xlsx"
DEFAULT_OUTPUT = "runtime_skills/e生涯选科参谋v001/assets/subject_selection/subject_requirements.json"
SUBJECTS = ("物理", "历史", "化学", "生物", "政治", "地理", "技术")
SUBJECT_ALIASES = {
    "思想政治": "政治",
    "政治": "政治",
    "物理": "物理",
    "历史": "历史",
    "化学": "化学",
    "生物": "生物",
    "地理": "地理",
    "技术": "技术",
}


CAREER_ALIASES = [
    {
        "career": "医生",
        "aliases": ["医生", "临床医生", "儿科医生", "外科医生", "内科医生"],
        "major_categories": ["临床医学类", "口腔医学类", "中医学类", "中西医结合类"],
        "major_keywords": ["临床医学", "口腔医学", "麻醉学", "医学影像学", "儿科学", "中医学"],
        "notes": "职业目标需先映射到医学相关专业，再核对专业选科要求。",
    },
    {
        "career": "程序员",
        "aliases": ["程序员", "软件工程师", "开发工程师", "算法工程师", "前端", "后端", "IT"],
        "major_categories": ["计算机类", "电子信息类"],
        "major_keywords": ["计算机", "软件工程", "人工智能", "数据科学", "信息安全", "网络工程"],
        "notes": "职业目标通常对应计算机类、软件工程、人工智能、电子信息等专业方向。",
    },
    {
        "career": "律师",
        "aliases": ["律师", "法官", "检察官", "法律职业"],
        "major_categories": ["法学类"],
        "major_keywords": ["法学", "知识产权", "国际经贸规则"],
        "notes": "法律职业通常先对应法学类专业，再核对选科最低要求和政治建议科目。",
    },
    {
        "career": "警察",
        "aliases": ["警察", "公安", "刑警", "民警", "警务", "侦查"],
        "major_categories": ["公安学类", "公安技术类", "法学类"],
        "major_keywords": ["治安学", "侦查学", "公安", "警务", "司法警察学", "刑事科学技术"],
        "notes": "警察目标可能对应公安学、公安技术或法学相关专业，需结合院校招生章程和体检政审要求。",
    },
]


BROAD_DIRECTION_ALIASES = [
    {
        "direction": "医学",
        "aliases": ["医学", "医药", "临床", "口腔", "中医", "药学", "护理"],
        "major_categories": [
            "基础医学类",
            "临床医学类",
            "口腔医学类",
            "公共卫生与预防医学类",
            "中医学类",
            "中西医结合类",
            "药学类",
            "中药学类",
            "医学技术类",
            "护理学类",
            "法医学类",
        ],
    },
    {
        "direction": "工科",
        "aliases": ["工科", "工程", "智能制造", "电子信息", "机械", "自动化"],
        "major_categories": [
            "机械类",
            "材料类",
            "电气类",
            "电子信息类",
            "自动化类",
            "计算机类",
            "土木类",
            "水利类",
            "测绘类",
            "化工与制药类",
            "交通运输类",
            "航空航天类",
            "兵器类",
            "能源动力类",
            "环境科学与工程类",
        ],
    },
    {
        "direction": "计算机",
        "aliases": ["计算机", "软件", "人工智能", "大数据", "网络安全", "程序开发"],
        "major_categories": ["计算机类", "电子信息类"],
    },
    {
        "direction": "法学",
        "aliases": ["法学", "法律", "政法"],
        "major_categories": ["法学类", "公安学类", "马克思主义理论类"],
    },
    {
        "direction": "文科",
        "aliases": ["文科", "人文社科", "文学", "历史", "新闻", "语言"],
        "major_categories": [
            "哲学类",
            "法学类",
            "教育学类",
            "中国语言文学类",
            "外国语言文学类",
            "新闻传播学类",
            "历史学类",
            "政治学类",
            "社会学类",
        ],
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build subject requirement JSON asset from the source xlsx.")
    parser.add_argument("--input", default=DEFAULT_SOURCE, help="Source xlsx path, relative to repository root by default.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output JSON path, relative to repository root by default.")
    args = parser.parse_args()

    repo_root = _find_repo_root(Path(__file__).resolve())
    source_path = _resolve_path(repo_root, args.input)
    output_path = _resolve_path(repo_root, args.output)
    rows = _read_xlsx_rows(source_path, sheet_name="数据表")
    if len(rows) < 3:
        raise ValueError(f"源表行数不足，无法解析: {source_path}")

    source_note = str(rows[0][0] or "").strip()
    headers = [str(item or "").strip() for item in rows[1][:6]]
    expected_headers = ["专业类", "专业", "选科最低要求", "科目范围一", "科目范围二的必选科目", "科目范围二的其他建议科目"]
    if headers != expected_headers:
        raise ValueError(f"表头不符合预期: actual={headers!r}, expected={expected_headers!r}")

    records = []
    for row_number, row in enumerate(rows[2:], start=3):
        padded_row = list(row[:6]) + [""] * max(0, 6 - len(row))
        values = [str(item or "").strip() for item in padded_row[:6]]
        if not any(values):
            continue
        major_category, major_name, minimum, scope_one, scope_two_required, scope_two_recommended = values
        if not major_category or not major_name:
            continue
        requirement = _parse_requirement(minimum)
        records.append(
            {
                "id": f"SUBJECT_REQ_{len(records) + 1:04d}",
                "source_row_number": row_number,
                "major_category": major_category,
                "major_name": major_name,
                "minimum_requirement": minimum,
                "requirement_status": requirement["status"],
                "required_subject_groups": requirement["groups"],
                "subject_scope_one": scope_one,
                "subject_scope_two_required": scope_two_required,
                "subject_scope_two_recommended": scope_two_recommended,
                "recommended_subjects": _parse_subject_list(scope_two_recommended),
                "match_terms": _build_match_terms(major_category, major_name),
            }
        )

    category_counter = Counter(item["major_category"] for item in records)
    requirement_counter = Counter(item["minimum_requirement"] or "__blank__" for item in records)
    asset = {
        "asset_id": "subject_selection_requirements_2021_generic",
        "asset_name": "2021年全国选科通用指引结构化资产",
        "asset_type": "major_subject_requirement_rules",
        "source_file": DEFAULT_SOURCE,
        "source_sheet": "数据表",
        "source_header_row": 2,
        "source_data_start_row": 3,
        "source_updated_at": "2026-06-11",
        "generated_at": datetime.now(tz=CHINA_TZ).isoformat(timespec="seconds"),
        "row_count": len(records),
        "schema_version": "1.0",
        "schema": {
            "filter_fields": ["major_category", "major_name", "minimum_requirement", "required_subject_groups"],
            "condition_fields": ["subject_scope_one", "subject_scope_two_required", "subject_scope_two_recommended"],
            "conclusion_fields": ["minimum_requirement", "required_subject_groups", "recommended_subjects"],
            "internal_fields": ["id", "source_row_number", "match_terms"],
            "field_semantics": {
                "major_category": "专业类，用于按专业大类筛选，例如计算机类、临床医学类、法学类。",
                "major_name": "专业名称，用于按明确专业筛选，例如软件工程、临床医学、法学。",
                "minimum_requirement": "选科最低要求；高校可在此基础上增加科目要求，不能当作最终院校要求。",
                "required_subject_groups": "从最低要求解析出的可满足科目组；外层表示任选一组，内层科目必须全部选择。",
                "subject_scope_one": "科目范围一，主要对应物理/历史或不限。",
                "subject_scope_two_required": "科目范围二必选科目，主要对应政治、地理、化学、生物等。",
                "subject_scope_two_recommended": "科目范围二建议科目；不是最低必选，但用于风险提示和方案校验。",
            },
        },
        "query_modes": [
            "major_requirement: 根据专业类/专业名称查询最低选科要求。",
            "career_requirement: 先把职业目标映射到相关专业，再查询选科要求。",
            "subject_combo_coverage: 根据用户已选或候选科目组合，筛出满足最低要求的专业。",
            "plan_validation: 在选科方案生成/复核前，核对目标专业或职业是否被候选组合覆盖。",
        ],
        "source_notes": _split_source_note(source_note),
        "usage_boundaries": [
            "本资产来自 2021 年通用版指引，适用于 3+3 与 3+1+2 的通用最低要求判断。",
            "高校可以在最低要求基础上增加科目要求；具体省份、年份、院校和专业组要求必须以当年本省考试院和高校招生章程为准。",
            "空白选科要求表示源文件未给出要求，不能当作“不限”。",
            "2021 年及以后新增专业如源文件没有对应要求，只能参考所属专业类，不能直接下确定结论。",
            "职业目标不能直接推出选科要求，必须先映射到专业或专业类，再核对该专业要求。",
        ],
        "career_aliases": CAREER_ALIASES,
        "broad_direction_aliases": BROAD_DIRECTION_ALIASES,
        "subject_aliases": SUBJECT_ALIASES,
        "category_summary": [
            {"major_category": category, "count": count}
            for category, count in category_counter.most_common()
        ],
        "requirement_summary": [
            {"minimum_requirement": "" if requirement == "__blank__" else requirement, "count": count}
            for requirement, count in requirement_counter.most_common()
        ],
        "records": records,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(asset, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output_path), "row_count": len(records)}, ensure_ascii=False))


def _find_repo_root(path: Path) -> Path:
    for parent in [path, *path.parents]:
        if (parent / "runtime_skills").is_dir() and (parent / "src").is_dir():
            return parent
    raise FileNotFoundError("无法定位仓库根目录。")


def _resolve_path(repo_root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else repo_root / path


def _read_xlsx_rows(path: Path, *, sheet_name: str) -> list[list[str]]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        sheet_path = _find_sheet_path(archive, sheet_name)
        root = ET.fromstring(archive.read(sheet_path))

    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []
    for row in root.findall(".//x:sheetData/x:row", namespace):
        row_number = int(row.attrib.get("r", len(rows) + 1))
        while len(rows) < row_number:
            rows.append([])
        values = rows[row_number - 1]
        for cell in row.findall("x:c", namespace):
            cell_ref = cell.attrib.get("r", "")
            col_index = _column_index(cell_ref)
            while len(values) < col_index:
                values.append("")
            values[col_index - 1] = _cell_text(cell, shared_strings)
    return rows


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values = []
    for item in root.findall("x:si", namespace):
        parts = [node.text or "" for node in item.findall(".//x:t", namespace)]
        values.append("".join(parts))
    return values


def _find_sheet_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    wb_ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rel_ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    rel_map = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.findall("r:Relationship", rel_ns)
    }
    for sheet in workbook.findall(".//x:sheets/x:sheet", wb_ns):
        if sheet.attrib.get("name") != sheet_name:
            continue
        rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
        target = rel_map.get(rel_id)
        if not target:
            break
        return "xl/" + target.lstrip("/")
    raise ValueError(f"未找到工作表: {sheet_name}")


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//x:t", namespace)).strip()
    value = cell.find("x:v", namespace)
    if value is None or value.text is None:
        return ""
    raw = value.text
    if cell_type == "s":
        index = int(raw)
        return shared_strings[index].strip() if 0 <= index < len(shared_strings) else ""
    return raw.strip()


def _column_index(cell_ref: str) -> int:
    letters = re.sub(r"[^A-Z]", "", cell_ref.upper())
    result = 0
    for char in letters:
        result = result * 26 + ord(char) - ord("A") + 1
    return result


def _split_source_note(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[\r\n]+", text)
        if item.strip()
    ]


def _parse_requirement(text: str) -> dict[str, object]:
    normalized = (text or "").strip()
    if not normalized:
        return {"status": "missing_in_source", "groups": []}
    if normalized == "不限":
        return {"status": "no_limit", "groups": [[]]}

    components = [_parse_subject_component(part) for part in normalized.split("+") if part.strip()]
    groups = []
    for combo in itertools.product(*components):
        group = []
        for subject in combo:
            if subject and subject not in group:
                group.append(subject)
        if group:
            groups.append(group)
    return {"status": "specified", "groups": groups}


def _parse_subject_component(text: str) -> list[str]:
    subjects = []
    for raw in re.split(r"[/、或]", text):
        subject = SUBJECT_ALIASES.get(raw.strip(), raw.strip())
        if subject in SUBJECTS and subject not in subjects:
            subjects.append(subject)
    return subjects or [text.strip()]


def _parse_subject_list(text: str) -> list[str]:
    if not text or text == "不限":
        return []
    subjects = []
    for subject in SUBJECTS:
        if subject in text and subject not in subjects:
            subjects.append(subject)
    return subjects


def _build_match_terms(major_category: str, major_name: str) -> list[str]:
    terms = [major_category, major_name]
    if major_category.endswith("类"):
        terms.append(major_category[:-1])
    for suffix in ("学", "工程", "技术", "管理"):
        if major_name.endswith(suffix) and len(major_name) > len(suffix) + 1:
            terms.append(major_name[: -len(suffix)])
    return sorted({item for item in terms if item})


if __name__ == "__main__":
    main()
