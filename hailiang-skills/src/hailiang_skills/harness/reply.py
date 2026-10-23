"""Small deterministic checks for explicit, measurable user output limits."""
from __future__ import annotations
import re


def reply_violations(goal, text):
    violations = []
    plain = re.sub(r'[\s*#`_]', '', text)
    total = re.search(r'(?:汇总|回复|全文|总计|总字数)[^。；\n]{0,16}不超过\s*(\d+)\s*字', goal)
    if total and len(plain) > int(total.group(1)):
        violations.append(f"总长度 {len(plain)} 字，要求不超过 {total.group(1)} 字")
    each = re.search(r'每条不超过\s*(\d+)\s*字', goal)
    if each:
        items = re.split(r'(?m)^\s*\d+[.、．]\s*', text)[1:]
        for index, item in enumerate(items, 1):
            count = len(re.sub(r'[\s*#`_]', '', item))
            if count > int(each.group(1)):
                violations.append(f"第 {index} 条 {count} 字，要求每条不超过 {each.group(1)} 字")
    return violations


def repair_instruction(goal):
    total = re.search(r'(?:汇总|回复|全文|总计|总字数)[^。；\n]{0,16}不超过\s*(\d+)\s*字', goal)
    each = re.search(r'每条不超过\s*(\d+)\s*字', goal)
    limits = []
    if total:
        limits.append(f"将全文压缩到 {max(1, int(total.group(1)) * 3 // 5)} 字以内")
    if each:
        limits.append(f"每条压缩到 {max(1, int(each.group(1)) * 3 // 5)} 字以内")
    return "；".join(limits) + "。只返回结论，不加开场、结束语或内部自查说明。"
