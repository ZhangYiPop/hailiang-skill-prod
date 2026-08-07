from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def col_to_num(col: str) -> int:
    value = 0
    for ch in col:
        if "A" <= ch <= "Z":
            value = value * 26 + ord(ch) - 64
    return value


def parse_ref(ref: str) -> tuple[int, int]:
    match = re.match(r"([A-Z]+)(\d+)", ref)
    if not match:
        return 0, 0
    return int(match.group(2)), col_to_num(match.group(1))


def parse_range(ref: str) -> tuple[int, int, int, int]:
    if ":" not in ref:
        row, col = parse_ref(ref)
        return row, col, row, col
    start_ref, end_ref = ref.split(":", 1)
    start_row, start_col = parse_ref(start_ref)
    end_row, end_col = parse_ref(end_ref)
    return start_row, start_col, end_row, end_col


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    shared_strings: list[str] = []
    if "xl/sharedStrings.xml" not in archive.namelist():
        return shared_strings
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    for item in root.findall(f"{NS_MAIN}si"):
        shared_strings.append("".join(node.text or "" for node in item.iter(f"{NS_MAIN}t")))
    return shared_strings


def _read_sheet_values(
    archive: zipfile.ZipFile, sheet_path: str, shared_strings: list[str]
) -> dict[tuple[int, int], str]:
    sheet_xml = ET.fromstring(archive.read(sheet_path))
    values: dict[tuple[int, int], str] = {}

    sheet_data = sheet_xml.find(f"{NS_MAIN}sheetData")
    if sheet_data is not None:
        for row in sheet_data.findall(f"{NS_MAIN}row"):
            for cell in row.findall(f"{NS_MAIN}c"):
                row_idx, col_idx = parse_ref(cell.attrib.get("r", ""))
                if row_idx == 0 or col_idx == 0:
                    continue
                value_node = cell.find(f"{NS_MAIN}v")
                value = value_node.text if value_node is not None else ""
                cell_type = cell.attrib.get("t")
                if cell_type == "s" and value:
                    value = shared_strings[int(value)]
                elif cell_type == "inlineStr":
                    inline = cell.find(f"{NS_MAIN}is")
                    value = (
                        "".join(node.text or "" for node in inline.iter(f"{NS_MAIN}t"))
                        if inline is not None
                        else ""
                    )
                values[(row_idx, col_idx)] = (value or "").strip()

    merge_cells = sheet_xml.find(f"{NS_MAIN}mergeCells")
    if merge_cells is not None:
        for merge_cell in merge_cells.findall(f"{NS_MAIN}mergeCell"):
            start_row, start_col, end_row, end_col = parse_range(merge_cell.attrib.get("ref", ""))
            top_left = values.get((start_row, start_col), "").strip()
            if not top_left:
                continue
            for row_idx in range(start_row, end_row + 1):
                for col_idx in range(start_col, end_col + 1):
                    values.setdefault((row_idx, col_idx), top_left)

    return values


def load_workbook_rows(path: Path) -> dict[str, list[list[str]]]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map = {
            rel.attrib.get("Id"): "xl/" + rel.attrib.get("Target", "").lstrip("/")
            for rel in rels
        }

        rows_by_sheet: dict[str, list[list[str]]] = {}
        for sheet in workbook.find(f"{NS_MAIN}sheets"):
            name = sheet.attrib.get("name", "")
            target = rel_map[sheet.attrib.get(NS_REL + "id")]
            values = _read_sheet_values(archive, target, shared_strings)
            if not values:
                rows_by_sheet[name] = []
                continue

            max_row = max(row_idx for row_idx, _ in values)
            max_col = max(col_idx for _, col_idx in values)
            sheet_rows: list[list[str]] = []
            for row_idx in range(1, max_row + 1):
                row = [values.get((row_idx, col_idx), "").strip() for col_idx in range(1, max_col + 1)]
                while row and not row[-1]:
                    row.pop()
                if any(cell.strip() for cell in row):
                    sheet_rows.append(row)
            rows_by_sheet[name] = sheet_rows

        return rows_by_sheet
