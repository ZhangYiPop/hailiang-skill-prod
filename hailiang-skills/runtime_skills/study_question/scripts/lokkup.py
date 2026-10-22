# -*- coding: utf-8 -*-
"""学习问题诊断 · 知识取用脚本

平台约定：stdin 传入 JSON → stdout 输出一个 JSON 对象。
支持两种输入：
1. 显式指令：{"action": "...", ...}，action 取值：
   - search_cards {query}      按家长行为描述搜候选卡（返回 top3 + 命中信号）
   - get_card      {id}        取某张卡片全文（含可引用方法清单）
   - get_method    {id}        取某个方法原文小节
   - get_methods   {ids: []}   批量取方法原文
   - get_redline   {id}        取某条红线话术
   - index                     取问题总索引
2. 平台自动注入的上下文（无 action 字段）：从中提取家长描述文本，自动按 search_cards 处理。
"""
import json
import re
import sys
from pathlib import Path

REF_DIR = Path(__file__).resolve().parent.parent / "references"
CARD_FILE = REF_DIR / "reference01_问题条目匹配卡.md"
METHOD_FILE = REF_DIR / "reference02_行动建议方法库.md"
REDLINE_FILE = REF_DIR / "reference03_红线与转介话术.md"


def read(p):
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def parse_cards(text):
    """卡片按 '## ' 二级标题切分；总索引表和附录整体跳过。"""
    cards = {}
    parts = re.split(r"\n(?=## )", text)
    for part in parts:
        m = re.match(r"## ([A-Z]{2,3}-\d{2})\s+(.+)\n", part + "\n")
        if not m:
            continue
        cid, name = m.group(1), m.group(2).strip()
        # 去掉标题行里的 ⚠ 等装饰
        name = name.replace("⚠", "").strip()
        signals = []
        in_signals = False
        for line in part.splitlines():
            if line.startswith("**典型信号**"):
                in_signals = True
                continue
            if in_signals:
                if line.startswith("- "):
                    signals.append(line[2:].strip())
                elif line.startswith("**"):
                    in_signals = False
        methods = []
        for line in part.splitlines():
            if line.startswith("**可引用方法**"):
                seg = line.split("**", 2)[2].lstrip("：: ")
                methods = [s.strip() for s in re.split(r"[｜|]", seg) if s.strip()]
                break
        cards[cid] = {"id": cid, "name": name, "text": part.strip(), "signals": signals, "methods": methods}
    return cards


def parse_methods(text):
    methods = {}
    parts = re.split(r"\n(?=### \[)", text)
    for part in parts:
        m = re.match(r"### \[([A-Z]{2}-[A-Z]?\d{2})\]\s+(.+)\n", part + "\n")
        if not m:
            continue
        mid, name = m.group(1), m.group(2).strip()
        methods[mid] = {"id": mid, "name": name, "text": part.strip()}
    return methods


def parse_redlines(text):
    redlines = {}
    parts = re.split(r"\n(?=## \[)", text)
    for part in parts:
        m = re.match(r"## \[(RL-\d{2})\]\s*(.+)\n", part + "\n")
        if not m:
            continue
        rid = m.group(1)
        # 提取话术正文（"**话术**："后的引号内容）
        speech = ""
        m2 = re.search(r"\*\*话术\*\*：\s*\n?\s*\"(.+?)\"", part, re.S)
        if m2:
            speech = m2.group(1).strip()
        redlines[rid] = {"id": rid, "name": m.group(2).strip(), "text": part.strip(), "speech": speech}
    return redlines


def bigrams(s):
    s = re.sub(r"[^\w\u4e00-\u9fff]", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


# 家长高频口语 → 卡片用语的同义扩展（追加到查询里提升召回）
SYNONYMS = {
    "算数": "计算 口算",
    "算术": "计算 口算",
    "磨蹭": "拖延 启动困难 推迟开始 慢",
    "读题": "阅读 审题 读不懂",
    "抓不住重点": "概括大意 中心思想",
    "记不住": "记忆 忘",
    "就忘": "记忆策略 复习",
    "粗心": "漏条件 抄错数 检查 一做就错 简单题",
    "丢分": "一做就错 错题",
    "效率低": "慢 速度 耗时",
    "手机": "屏幕 电子产品",
    "考试": "限时 闭卷",
    "做题慢": "速度慢 限时做不完",
    "写作业慢": "速度慢 耗时长",
    "掉链子": "限时 考试 不会做",
    "没兴趣": "动力 兴趣缺失",
    "没动力": "动力缺失 兴趣缺失",
    "没目标": "目标感 方向",
    "三分钟热度": "兴趣短暂 坚持",
    "叛逆": "逆反 反抗安排",
    "就放弃": "遇难就退 坚持不了",
}


def expand_query(query):
    extra = []
    for k, v in SYNONYMS.items():
        if k in query:
            extra.append(v)
    return query + " " + " ".join(extra) if extra else query


# 过于泛化的 2-gram，不参与匹配（否则"学习/孩子"会让所有卡片虚高）
STOP_GRAMS = {"学习", "孩子", "家长", "老师", "不会", "就是", "特别", "非常",
              "现在", "怎么", "时候", "什么", "也是", "还有", "觉得", "情况",
              "问题", "老是", "总是", "最近", "好像"}


def search_cards(cards, query, features=None):
    """2-gram 重叠匹配：每条信号 + 索引一句话特征各算一次命中，卡片得分 = 命中数。"""
    if not query.strip():
        return []
    qg = bigrams(expand_query(query)) - STOP_GRAMS
    features = features or {}
    results = []
    for cid, c in cards.items():
        hit_signals = []
        lines = list(c["signals"])
        if cid in features:
            lines.append(features[cid])  # 索引一句话特征
        for sig in lines:
            keys = re.split(r"[（(，,：:；;、\s]", sig)
            key_grams = set()
            for k in keys:
                if len(k) >= 3:
                    key_grams |= bigrams(k)
            if (key_grams - STOP_GRAMS) & qg:
                hit_signals.append(sig)
        # 字面对应加权：同义词/特征分段/卡片名出现在查询里 → 加一次命中
        feature = features.get(cid, "")
        bonus = None
        for tok in expand_query(query).split():
            if len(tok) >= 2 and (tok in c["name"] or tok in feature):
                bonus = "【词面匹配】%s" % tok
                break
        if not bonus:
            for seg in re.split(r"[、，,]", feature):
                if len(seg) >= 3 and seg in query:
                    bonus = "【特征匹配】%s" % seg
                    break
        if not bonus and len(c["name"]) >= 3 and c["name"] in query:
            bonus = "【名称匹配】%s" % c["name"]
        if bonus:
            hit_signals.append(bonus)
        if hit_signals:
            results.append({
                "id": cid, "name": c["name"], "hit_count": len(hit_signals),
                "hit_signals": hit_signals, "methods": c["methods"],
            })
    results.sort(key=lambda x: -x["hit_count"])
    return results[:3]


def extract_query(payload):
    """从平台上下文 JSON 里提取家长描述文本。"""
    for key in ("query", "user_message", "message", "text", "input"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    return c
    return ""


def main():
    raw = sys.stdin.read() if not sys.stdin.isatty() else "{}"
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"query": str(payload)}

    cards = parse_cards(read(CARD_FILE))
    methods = parse_methods(read(METHOD_FILE))
    redlines = parse_redlines(read(REDLINE_FILE))
    action = payload.get("action")

    # 总索引的一句话特征（用于搜索召回）
    card_text = read(CARD_FILE)
    features = {}
    if "## 问题总索引" in card_text:
        idx = card_text[card_text.index("## 问题总索引"):]
        idx = idx[:idx.index("\n---")] if "\n---" in idx else idx
        for cid, feat in re.findall(r"^\|\s*([A-Z]{2,3}-\d{2})\s*\|[^|]+\|\s*(.+?)\s*\|", idx, re.M):
            features[cid] = feat

    try:
        if action == "index":
            result = [{"id": c["id"], "name": c["name"]} for c in cards.values()]
            out = {"ok": True, "result": result}
        elif action == "get_card":
            cid = str(payload.get("id", "")).strip().upper()
            c = cards.get(cid)
            out = {"ok": True, "result": c} if c else {"ok": False, "error": "卡片不存在: %s" % cid, "available": sorted(cards)}
        elif action == "get_method":
            mid = str(payload.get("id", "")).strip().upper()
            m = methods.get(mid)
            out = {"ok": True, "result": m} if m else {"ok": False, "error": "方法不存在: %s" % mid, "available": sorted(methods)}
        elif action == "get_methods":
            ids = payload.get("ids", [])
            got, miss = {}, []
            for i in ids:
                i = str(i).strip().upper()
                if i in methods:
                    got[i] = methods[i]
                else:
                    miss.append(i)
            out = {"ok": not miss, "result": got, "missing": miss}
        elif action == "get_redline":
            rid = str(payload.get("id", "")).strip().upper()
            r = redlines.get(rid)
            out = {"ok": True, "result": r} if r else {"ok": False, "error": "红线不存在: %s" % rid, "available": sorted(redlines)}
        elif action == "search_cards":
            results = search_cards(cards, str(payload.get("query", "")), features)
            out = {"ok": True, "result": results, "note": "命中分档：≥3条信号且鉴别排除→确诊；2条→初步诊断；<2条→继续追问或无法确诊"}
        elif action is None:
            q = extract_query(payload)
            results = search_cards(cards, q, features)
            out = {"ok": True, "action": "search_cards", "query_used": q[:200], "result": results}
        else:
            out = {"ok": False, "error": "未知 action: %s" % action,
                   "supported": ["index", "get_card", "get_method", "get_methods", "get_redline", "search_cards"]}
    except Exception as e:  # 任何异常都不能让脚本裸崩，统一 JSON 出口
        out = {"ok": False, "error": "内部错误: %s" % e}

    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
