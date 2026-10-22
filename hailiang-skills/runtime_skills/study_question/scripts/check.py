# -*- coding: utf-8 -*-
"""学习问题诊断 · 维护期一致性校验

维护者改完 references/ 下任何文件后运行本脚本（无需参数）。
校验内容：
  1. 卡片【可引用方法】里的每个方法编号在方法库中存在（ID+名称双向一致）
  2. 方法库中每个方法至少被一张卡片引用（孤儿方法警告）
  3. 卡片引用的红线编号（RL-XX）在红线文件中存在
  4. 总索引表与卡片正文一一对应（编号、名称）
  5. 卡片结构完整（定义/典型信号/学段差异/鉴别/可引用方法）
  6. SKILL.md 白名单方法存在
退出码：0 = 全部通过；1 = 存在错误（警告不改变退出码）。
"""
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CARD_FILE = BASE / "references" / "reference01_问题条目匹配卡.md"
METHOD_FILE = BASE / "references" / "reference02_行动建议方法库.md"
REDLINE_FILE = BASE / "references" / "reference03_红线与转介话术.md"
SKILL_FILE = BASE / "SKILL.md"

errors, warnings = [], []


def check(cond, msg, warn=False):
    (warnings if warn else errors).append(msg) if not cond else None


def main():
    card_text = CARD_FILE.read_text(encoding="utf-8")
    method_text = METHOD_FILE.read_text(encoding="utf-8")
    redline_text = REDLINE_FILE.read_text(encoding="utf-8")

    # 方法库：ID -> 名称
    methods = dict(re.findall(r"^### \[([A-Z]{2}-[A-Z]?\d{2})\]\s+(.+)$", method_text, re.M))
    # 红线：ID 集合
    redlines = set(re.findall(r"^## \[(RL-\d{2})\]", redline_text, re.M))

    # 卡片正文（跳过总索引表和附录）
    card_parts = re.split(r"\n(?=## )", card_text)
    cards = {}
    for part in card_parts:
        m = re.match(r"## ([A-Z]{2,3}-\d{2})\s+(.+)\n", part + "\n")
        if not m:
            continue
        cid = m.group(1)
        name = m.group(2).replace("⚠", "").strip()
        if cid.startswith("RL") or "附录" in name:
            continue
        cards[cid] = (name, part)

    # 1+5. 卡片方法引用与结构
    referenced = set()
    for cid, (name, part) in cards.items():
        for field in ("**定义**", "**典型信号**", "**学段差异**", "**鉴别**", "**可引用方法**"):
            check(field in part, "卡片 %s 缺少字段 %s" % (cid, field))
        m = re.search(r"\*\*可引用方法\*\*：(.+)", part)
        if m:
            for token in re.split(r"[｜|]", m.group(1)):
                token = token.strip()
                if not token:
                    continue
                mid, mname = token.split(" ", 1) if " " in token else (token, "")
                mid, mname = mid.strip(), mname.strip()
                check(mid in methods, "卡片 %s 引用了方法库中不存在的方法 %s" % (cid, mid))
                if mid in methods and mname:
                    check(methods[mid] == mname,
                          "卡片 %s 方法 %s 名称不一致：卡片写「%s」，方法库为「%s」" % (cid, mid, mname, methods[mid]))
                referenced.add(mid)
        for rid in set(re.findall(r"(RL-\d{2})", part)):
            check(rid in redlines, "卡片 %s 引用了不存在的红线 %s" % (cid, rid))

    # 2. 孤儿方法
    for mid in methods:
        if mid not in referenced:
            warnings.append("方法 %s %s 未被任何卡片引用（若为通用/白名单方法可忽略）" % (mid, methods[mid]))

    # 3. 白名单
    if SKILL_FILE.exists():
        skill_text = SKILL_FILE.read_text(encoding="utf-8")
        for mid in re.findall(r"白名单[^：\n]*：([^。\n]+)", skill_text):
            for token in re.findall(r"([A-Z]{2}-[A-Z]?\d{2})", mid):
                check(token in methods, "SKILL.md 白名单引用了不存在的方法 %s" % token)

    # 4. 总索引一致性
    idx_section = card_text[card_text.index("## 问题总索引"):card_text.index("\n---\n", card_text.index("## 问题总索引"))]
    index_rows = re.findall(r"^\|\s*([A-Z]{2,3}-\d{2})\s*\|\s*(.+?)\s*\|", idx_section, re.M)
    index_ids = {}
    for cid, name in index_rows:
        index_ids[cid] = name
    for cid, (name, _) in cards.items():
        check(cid in index_ids, "卡片 %s 未收录进总索引" % cid)
        if cid in index_ids:
            check(index_ids[cid].startswith(name.replace("（B类）", "").replace("⚠", "").strip()) or name.startswith(index_ids[cid].replace("⚠", "").strip()),
                  "卡片 %s 名称与索引不一致：索引「%s」vs 卡片「%s」" % (cid, index_ids[cid], name))
    for cid in index_ids:
        check(cid in cards, "总索引中的 %s 没有对应卡片正文" % cid)

    # 报告
    print("=" * 56)
    print("学习问题诊断 · 一致性校验")
    print("卡片 %d 张 ｜ 方法 %d 个 ｜ 红线 %d 条" % (len(cards), len(methods), len(redlines)))
    print("=" * 56)
    if errors:
        print("\n[错误] %d 项：" % len(errors))
        for e in errors:
            print("  ✗", e)
    if warnings:
        print("\n[警告] %d 项：" % len(warnings))
        for w in warnings:
            print("  !", w)
    if not errors and not warnings:
        print("\n全部通过，无警告。")
    elif not errors:
        print("\n错误 0 项，警告 %d 项。" % len(warnings))
    print()
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
