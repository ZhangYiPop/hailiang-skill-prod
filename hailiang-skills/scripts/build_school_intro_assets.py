from __future__ import annotations

import hashlib
import json
from pathlib import Path

from xlsx_utils import load_workbook_rows


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_XLSX = PROJECT_ROOT / "docs" / "院校介绍" / "院校介绍.xlsx"
TARGET_DIR = PROJECT_ROOT / "assets" / "generated" / "school_intro"


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_schools() -> list[dict]:
    rows_by_sheet = load_workbook_rows(SOURCE_XLSX)
    rows = next(iter(rows_by_sheet.values()), [])
    if len(rows) < 2:
        return []
    header = rows[0]
    items: list[dict] = []
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        record = dict(zip(header, row))
        school_name = (record.get("代表院校") or "").strip()
        if not school_name:
            continue
        items.append(
            {
                "school_name": school_name,
                "school_url": (record.get("院校链接") or "").strip(),
                "school_intro": (record.get("学校简介") or "").strip(),
                "source_payload": record,
            }
        )
    return items


def main() -> None:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    schools = parse_schools()
    manifest = {
        "source_file": str(SOURCE_XLSX.relative_to(PROJECT_ROOT)),
        "source_hash": hashlib.sha256(SOURCE_XLSX.read_bytes()).hexdigest(),
        "counts": {
            "schools": len(schools),
        },
    }
    write_json(TARGET_DIR / "schools.json", schools)
    write_json(TARGET_DIR / "asset_manifest.json", manifest)


if __name__ == "__main__":
    main()
