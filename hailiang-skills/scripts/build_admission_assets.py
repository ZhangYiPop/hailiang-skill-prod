from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = PROJECT_ROOT / "docs" / "模拟升学"
TARGET_DIR = PROJECT_ROOT / "assets" / "generated" / "admission"


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_province_flow_map(text: str) -> dict:
    flows = {
        "zhejiang": {"flow_type": "zhejiang", "provinces": ["浙江"], "notes": "省内/省外分流"},
        "three_plus_one_two": {"flow_type": "3+1+2", "provinces": [], "notes": "需要确认物理/历史"},
        "traditional": {"flow_type": "traditional", "provinces": [], "notes": "直接按总分推荐"},
    }
    for line in text.splitlines():
        line = line.strip("- ")
        if line.startswith("包含："):
            provinces = [item.strip() for item in line.replace("包含：", "").split("、") if item.strip()]
            if "广东" in provinces:
                flows["three_plus_one_two"]["provinces"] = provinces
            else:
                flows["traditional"]["provinces"] = provinces
    return flows


def split_markdown_sections(text: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^##\s+(.+)$", text, flags=re.M))
    sections = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sections.append((match.group(1).strip(), text[start:end].strip()))
    return sections


def extract_document_title(text: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", text, flags=re.M)
    return match.group(1).strip() if match else fallback


def infer_zhejiang_region_variant(filename: str) -> tuple[str, str]:
    if "省内" in filename:
        return "浙江", "浙江（省内）"
    if "省外" in filename:
        return "浙江", "浙江（省外）"
    return "浙江", "浙江"


def infer_section_metadata(file_path: Path, title: str, exam_mode: str) -> tuple[str, str | None]:
    if exam_mode == "zhejiang":
        return infer_zhejiang_region_variant(file_path.name)

    province = re.sub(r"（.*?）", "", title).strip()
    region_variant = title if title != province else None
    return province, region_variant


def parse_markdown_table(block: str) -> list[list[str]]:
    rows = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [item.strip() for item in line.strip("|").split("|")]
        if cells and set("".join(cells)) <= {"-", " ", ":"}:
            continue
        rows.append(cells)
    return rows


def parse_score_range(text: str) -> tuple[int | None, int | None]:
    match = re.search(r"(\d+)≤分数≤(\d+)", text)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def exam_mode_from_filename(filename: str) -> tuple[str, str | None]:
    if "浙江" in filename:
        return "zhejiang", None
    if "物理组" in filename:
        return "3+1+2", "物理"
    if "历史组" in filename:
        return "3+1+2", "历史"
    return "traditional", None


def parse_band_docs() -> list[dict]:
    items = []
    for file_path in sorted(DOCS_DIR.glob("[0-9][1-5]_*.md")):
        exam_mode, subject_group = exam_mode_from_filename(file_path.name)
        text = file_path.read_text(encoding="utf-8")
        sections = split_markdown_sections(text)
        if not sections:
            document_title = extract_document_title(text, file_path.stem)
            sections = [(document_title, text)]

        for title, block in sections:
            table = parse_markdown_table(block)
            if len(table) < 2:
                continue
            headers = table[0]
            province, region_variant = infer_section_metadata(file_path, title, exam_mode)
            for row in table[1:]:
                record = dict(zip(headers, row))
                min_score, max_score = parse_score_range(record.get("分数要求", ""))
                items.append(
                    {
                        "province": province,
                        "region_variant": region_variant,
                        "exam_mode": exam_mode,
                        "subject_group": subject_group,
                        "tier_name": record.get("院校层次", ""),
                        "min_score": min_score,
                        "max_score": max_score,
                        "sample_schools": [item.strip() for item in record.get("代表院校", "").split("、") if item.strip()],
                        "recommended_paths": [item.strip() for item in re.split(r"[、，]", record.get("推荐路径", "")) if item.strip()],
                        "raw_payload": record,
                    }
                )
    return items


def parse_tier_copywriting() -> list[dict]:
    text = (DOCS_DIR / "06_院校介绍文案.md").read_text(encoding="utf-8")
    table = parse_markdown_table(text)
    if len(table) < 2:
        return []
    headers = table[0]
    items = []
    for row in table[1:]:
        record = dict(zip(headers, row))
        items.append(
            {
                "tier_name": record.get("层次名称", ""),
                "intro": record.get("层次介绍文案", ""),
            }
        )
    return items


def main() -> None:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)

    flow_text = (DOCS_DIR / "00_省份分类规则.md").read_text(encoding="utf-8")
    flow_map = parse_province_flow_map(flow_text)
    province_score_bands = parse_band_docs()
    tier_copywriting = parse_tier_copywriting()

    source_hash = hashlib.sha256()
    for file_path in sorted(DOCS_DIR.glob("*.md")):
        source_hash.update(file_path.read_bytes())

    manifest = {
        "source_dir": str(DOCS_DIR.relative_to(PROJECT_ROOT)),
        "source_hash": source_hash.hexdigest(),
        "counts": {
            "flow_map": len(flow_map),
            "province_score_bands": len(province_score_bands),
            "tier_copywriting": len(tier_copywriting),
        },
    }

    write_json(TARGET_DIR / "province_flow_map.json", flow_map)
    write_json(TARGET_DIR / "province_score_bands.json", province_score_bands)
    write_json(TARGET_DIR / "tier_copywriting.json", tier_copywriting)
    write_json(TARGET_DIR / "asset_manifest.json", manifest)


if __name__ == "__main__":
    main()
