#!/usr/bin/env python3 -I
"""Parse 03_行动建议方法库.md and output 03_行动建议方法库.json."""

import json
import re
import sys

INPUT = "/Users/letty/code/工作/hailiang/测试/1020/新规范/学习问题诊断/study_question/references/03_行动建议方法库.md"
OUTPUT = "/Users/letty/code/工作/hailiang/测试/1020/新规范/学习问题诊断/study_question/references/03_行动建议方法库.json"

with open(INPUT, "r", encoding="utf-8") as f:
    text = f.read()

# Split by method headers: lines starting with "### [XX-XXX]"
# We'll split on "### [" to get each method block
# First, strip the preamble (everything before the first "### [")
preamble_end = text.index("### [")
body = text[preamble_end:]

# Split on "### [" — each block will start with "### [XX-RXX] name...\n..."
# But we need to keep the marker. Let's use a regex approach instead.
# Split on "\n---\n" then parse each chunk
chunks = re.split(r'\n---\n', body)

methods = []

for chunk in chunks:
    chunk = chunk.strip()
    if not chunk:
        continue

    lines = chunk.split('\n')

    # First line should be "### [XX-RXX] 方法名称" or "### [XX-SXX] 方法名称"
    header_line = lines[0].strip()
    m = re.match(r'^###\s+\[([A-Z]+-[RS]\d+)\]\s+(.+)$', header_line)
    if not m:
        print(f"WARNING: Could not parse header: {header_line}", file=sys.stderr)
        continue

    code = m.group(1)
    name = m.group(2).strip()

    # Classify: R = 能力提升技巧 (A?), S = 方法策略 (B?)
    # The user said "类别": "A" — let me check if there's a pattern
    # Actually looking at the data, all start with module codes. Let's just leave
    # the category as the module prefix for now. Wait, the user gave an example
    # with "类别": "A". Let me re-read the instructions...
    # The user said:
    # "类别": "A"
    # There's no explicit category in the markdown. Let me look at the original file again.
    # The file says:
    # > 用途：A类（认知与学习方法）行动建议方法库
    # So all methods in this file are "A" class. Let me just set them all to "A".

    # Collect the body after the header
    body_lines = lines[1:]
    body_text = '\n'.join(body_lines).strip()

    # Parse sections: **适用**, **为什么做**, **怎么做**, **避坑**, **频次**, **核心原则**
    # Strategy: find each section by its bold marker, then extract content until
    # the next bold marker or end of text.

    # Find all bold-marked section starts
    # Pattern: **SECTION_NAME**： or **SECTION_NAME**:
    section_starts = list(re.finditer(r'\*\*([^*]+)\*\*[：:]', body_text))

    sections = {}
    for i, m in enumerate(section_starts):
        key = m.group(1).strip()
        start = m.end()
        if i + 1 < len(section_starts):
            end = section_starts[i + 1].start()
        else:
            end = len(body_text)
        content = body_text[start:end].strip()
        sections[key] = content

    method = {
        "编号": code,
        "方法名称": name,
        "类别": "A",
        "适用": sections.get("适用", ""),
        "为什么做": sections.get("为什么做", ""),
        "怎么做": sections.get("怎么做", ""),
        "避坑": sections.get("避坑", ""),
        "频次": sections.get("频次", ""),
    }

    # Also handle "核心原则" — if present, merge into 为什么做
    if "核心原则" in sections and not method["为什么做"]:
        method["为什么做"] = sections["核心原则"]

    methods.append(method)

# Verify count
print(f"Parsed {len(methods)} methods")

# Write JSON
with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(methods, f, ensure_ascii=False, indent=2)

print(f"Written to {OUTPUT}")

# Spot-check EF-R01, EF-R02, AT-R03
spot_check_codes = ["EF-R01", "EF-R02", "AT-R03"]
for code in spot_check_codes:
    for m in methods:
        if m["编号"] == code:
            print(f"\n--- {code}: {m['方法名称']} ---")
            for k, v in m.items():
                if k in ("编号", "方法名称", "类别"):
                    continue
                preview = v[:80].replace('\n', '\\n') if v else "(empty)"
                print(f"  {k}: {preview}...")
            break
    else:
        print(f"\n--- {code}: NOT FOUND ---")