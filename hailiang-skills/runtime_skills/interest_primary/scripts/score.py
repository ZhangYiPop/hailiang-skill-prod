#!/usr/bin/env python3
"""小学兴趣测评：确定性计分 + 四轴画像 + 类型判定。

平台脚本约定（stdin JSON → stdout JSON）：
  平台通过 stdin 传入本轮对话 JSON 上下文，脚本从 stdin 读取，stdout 只输出一个 JSON 对象。
  8 题作答从上下文的常见位置自动适配（只做确定性映射，不猜测、不编造）：
    - answers / answer / 作答 字段：字符串（"ACDABCAB"、"1A 2C … 8B"、"Q1A Q2C …"）、
      q1-q8 字典、8 个单字母列表（v1.2.6 起键名宽容识别：任意含 answer/ans/作答/assessment/quiz
      词干的键均按作答字段解析——十五测实测 planner 自创 assessment_answers 键名致漏识别）；
    - round1_answers + round2_answers 分离字段（v1.2.1 新增，十测实测形态——对话两轮采集后模型按轮传参；
      roundN / rN / round_N_answers / 第N轮(作答) / 轮N 均识别，v1.2.3 起兼容 answers_roundN 键序
      （answers 在前 round 在后，十二测实测平台 parameters 形态），v1.2.6 起改为轮次模式识别——
      键名任意位置含 round1/round2/r1/r2（词边界）/第N轮/N轮 即按该轮解析（十五测实测 planner
      自创 assessment_round1/assessment_round2 键名，白名单精确匹配漏识别 → answers_not_found →
      模型估分编造 T11 与得分脱节）；另值兜底：作答词干键 + 纯 4 连字母值 + 键内含数字 1/2 → 按该轮，
      值为 4 连字母（"ABCD"）、4 元列表或轮内编号式 "1A 2B 3C 4D"（v1.2.11 起第 2 轮也认绝对题号
      "5B 6A 7B 8D"、无分隔符形态 "1a2b3c4d" 同样识别——六十/六十一测实测），第 1 轮映射 Q1-Q4、
      第 2 轮映射 Q5-Q8，仅做确定性位置映射）；turn1 / grade1 / q1 等无轮次词干的键不吞——q1 仍按单题键走 Q1）；
    - q1..q8 / Q1..Q8 / 题1..题8 / 第N题 键（含嵌套于 parameters / planner_parameters 等结构内，深度≤4）；
    - query / user_input / text / input / content 等文本字段中的作答串；
    - messages / history / conversation 列表中用户消息的 content（assistant / system 消息不扫描，
      防止题面示例文本被误采）。
  适配优先级：结构化字段 > 文本编号式作答 > 文本 8 连字母作答（仅当唯一且无其他来源时采用）。
  "<user_input>" 等尖括号占位符一律视为空；"3D电影"类文本不会误判为作答（选项字母后紧跟汉字不采）。

输出契约（任何输入都不抛异常、不输出空——模型拿不到数据时必须能从 hint 知道下一步）：
  ok: true  → 计分结果：answers / totals / ranking / top1 / top1_score / parallel /
              balanced_explorer / t11_directions（命中 T11 时：得分前 3 主修方向名，各抽 1 节体验课用）/
              profile / type / prompts（type.fallback=true 表示类型库零命中）
  ok: false → answers_incomplete（collected / missing / hint——v1.2.12 起 hint 先提示回捞另一轮
              齐传重试（missing 题可能在前几轮已收齐、只是本轮参数漏带），确无才按 missing 补问）
              answers_not_found（hint，先完成测评作答采集）
              script_error（hint，按 SKILL.md 手动计分工作单执行）
  禁止模型在 ok: false 时估分或编造结果。

调试与平台调用兼容（命令行仅为兼容保留，一律退出码 0、stdout 单 JSON）：
  平台实际观测形态：score.py --query "ACDABCAB" --user-id … --session-id …（v1.1.2 曾因不识别
  --query 报 usage 错误导致脚本从未成功执行——现已兼容，未知参数静默忽略）。
  python3 score.py --answers "ACDABCAB"     # 等价于 stdin 的 {"answers": "ACDABCAB"}
  python3 score.py --selftest               # 全量 4^8 组合自检
  输入优先级：--answers > --query > stdin；三种来源同时存在时合并解析。
  ok: false（含非法作答）不抛异常、退出码仍为 0——平台以 stdout JSON 为准，非零退出会被当作脚本失败。

计分依据 references_01（逐格一致，由 verify_skill.py 交叉校验）。
"""
import argparse
import itertools
import json
import re
import sys

CATEGORIES = ["理科思维", "实验探究", "体育竞技", "艺术感知", "语言沟通", "人文素养"]

# (题, 选项) -> {类别: 分}；空 dict = 不计分
SCORES = {
    ("Q1", "A"): {"体育竞技": 2},
    ("Q1", "B"): {"实验探究": 1, "艺术感知": 2},
    ("Q1", "C"): {"实验探究": 1, "人文素养": 2},
    ("Q1", "D"): {"体育竞技": 1, "艺术感知": 2},
    ("Q2", "A"): {"体育竞技": 1, "语言沟通": 1},
    ("Q2", "B"): {"理科思维": 1, "艺术感知": 1},
    ("Q2", "C"): {"实验探究": 1, "艺术感知": 1},
    ("Q2", "D"): {"艺术感知": 1, "人文素养": 1},
    ("Q3", "A"): {"实验探究": 2},
    ("Q3", "B"): {"理科思维": 2},
    ("Q3", "C"): {"体育竞技": 1, "艺术感知": 1},
    ("Q3", "D"): {"语言沟通": 1, "人文素养": 1},
    ("Q4", "A"): {"体育竞技": 2, "语言沟通": 1},
    ("Q4", "B"): {"艺术感知": 2},
    ("Q4", "C"): {"语言沟通": 2, "人文素养": 1},
    ("Q4", "D"): {"语言沟通": 2},
    ("Q5", "A"): {"实验探究": 2},
    ("Q5", "B"): {"人文素养": 2},
    ("Q5", "C"): {"体育竞技": 2},
    ("Q5", "D"): {"艺术感知": 2},
    ("Q6", "A"): {"艺术感知": 1, "语言沟通": 1},
    ("Q6", "B"): {"体育竞技": 1},
    ("Q6", "C"): {"理科思维": 1, "艺术感知": 1},
    ("Q6", "D"): {},
    ("Q7", "A"): {"实验探究": 1, "艺术感知": 1},
    ("Q7", "B"): {"体育竞技": 1, "艺术感知": 1},
    ("Q7", "C"): {"语言沟通": 2},
    ("Q7", "D"): {"理科思维": 1, "体育竞技": 1},
    ("Q8", "A"): {"理科思维": 1, "艺术感知": 1, "人文素养": 1},
    ("Q8", "B"): {},
    ("Q8", "C"): {},
    ("Q8", "D"): {},
}

SERIES = {("Q1", "A"): "运动", ("Q1", "B"): "艺术", ("Q1", "C"): "认知", ("Q1", "D"): "艺术",
          ("Q5", "A"): "认知", ("Q5", "B"): "认知", ("Q5", "C"): "运动", ("Q5", "D"): "艺术"}
SOCIAL = {("Q2", "A"): "群体", ("Q2", "B"): "中间", ("Q2", "C"): "独立", ("Q2", "D"): "群体",
          ("Q4", "A"): "中间", ("Q4", "B"): "独立", ("Q4", "C"): "中间", ("Q4", "D"): "群体"}
THINK = {("Q3", "A"): "拆解探索", ("Q3", "B"): "规则执行", ("Q3", "C"): "试错体验", ("Q3", "D"): "规则执行"}

# 轨道二零命中时，按主修方向取兜底类型行（references_08 第七节失败路径）
FALLBACK_ROW = {"体育竞技": "T1", "理科思维": "T3", "实验探究": "T4",
                "艺术感知": "T5", "语言沟通": "T8", "人文素养": "T9"}
TYPE_NAMES = {"T1": "活力运动家", "T2": "独立小健将", "T3": "策略小棋手", "T4": "小小建造家",
              "T5": "视觉小艺术家", "T6": "音乐感受者", "T7": "舞台小明星", "T8": "故事小达人",
              "T9": "阅读小书虫", "T10": "人文小讲师", "T11": "均衡探索者"}


def parse_answers(raw):
    pairs = re.findall(r"(?:Q|q)?(\d)\s*[:：.、\-]?\s*([A-Da-d])", str(raw))
    answers = {f"Q{n}": L.upper() for n, L in pairs if 1 <= int(n) <= 8}
    if len(answers) == 8:
        return answers
    compact = re.sub(r"[^A-Da-d]", "", str(raw))
    if re.fullmatch(r"[A-Da-d]{8}", compact):
        return {f"Q{i+1}": compact[i].upper() for i in range(8)}
    raise ValueError(f"作答解析失败：需要 8 题作答（如 \"ACDABCAB\" 或 \"1A 2C 3D 4A 5B 6C 7A 8B\"），收到：{raw!r}")


def compute(answers):
    totals = dict.fromkeys(CATEGORIES, 0)
    for i in range(1, 9):
        qn, letter = f"Q{i}", answers[f"Q{i}"]
        for cat, v in SCORES[(qn, letter)].items():
            totals[cat] += v

    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    top1v = ranked[0][1]
    top1 = [c for c, v in ranked if v == top1v]
    parallel = [c for c, v in ranked[1:3] if c not in top1 and top1v - v <= 2]
    balanced = ranked[0][1] - ranked[1][1] <= 1 and ranked[1][1] - ranked[2][1] <= 1

    s1, s2 = SERIES[("Q1", answers["Q1"])], SERIES[("Q5", answers["Q5"])]
    activity = f"{s1}型" if s1 == s2 else "混合型"
    pair = [SOCIAL[("Q2", answers["Q2"])], SOCIAL[("Q4", answers["Q4"])]]
    if ("群体" in pair and "独立" in pair) or pair == ["中间", "中间"]:
        social = "混合"
    else:
        social = "群体驱动" if "群体" in pair else "独立探索"
    profile = {"活动倾向": activity, "社交模式": social,
               "思维特征": THINK[("Q3", answers["Q3"])],
               "动机来源": "内在驱动" if answers["Q7"] in "AD" else "外在驱动"}

    top2v = ranked[1][1]
    top2 = {c for c, v in ranked if v > 0 and v >= top2v}

    def match():
        if balanced:
            return "T11"
        p, a = profile, answers
        if p["活动倾向"] == "运动型" and p["社交模式"] == "群体驱动" and "体育竞技" in top1:
            return "T1"
        if p["活动倾向"] == "运动型" and p["社交模式"] == "独立探索" and "体育竞技" in top1:
            return "T2"
        if p["活动倾向"] == "认知型" and p["思维特征"] == "规则执行" and "理科思维" in top1:
            return "T3"
        if p["活动倾向"] == "认知型" and p["思维特征"] == "拆解探索" and "实验探究" in top1:
            return "T4"
        if p["活动倾向"] == "艺术型" and a["Q4"] == "B" and "艺术感知" in top1:
            return "T5"
        if p["活动倾向"] == "艺术型" and (a["Q1"] == "D" or a["Q8"] == "A") and "艺术感知" in top1:
            return "T6"
        if p["活动倾向"] == "艺术型" and a["Q7"] == "C" and ({"体育竞技", "艺术感知"} & top2):
            return "T7"
        if p["活动倾向"] == "认知型" and a["Q4"] == "C" and "语言沟通" in top1:
            return "T8"
        if p["活动倾向"] == "认知型" and (a["Q5"] == "B" or a["Q8"] == "A") and "人文素养" in top1:
            return "T9"
        if p["活动倾向"] == "认知型" and (a["Q1"] == "C" or a["Q4"] == "C") and "人文素养" in top1:
            return "T10"
        return None

    code = match()
    if code:
        type_out = {"code": code, "name": TYPE_NAMES[code], "fallback": False}
    else:
        major = top1[0]
        type_out = {"code": None, "name": None, "fallback": True,
                    "fallback_row": FALLBACK_ROW[major], "major": major}

    prompts = []
    if answers["Q6"] == "D":
        prompts.append("教学风格提示")
    if answers["Q8"] != "A":
        prompts.append("课程节奏提示")

    result = {"answers": {f"Q{i}": answers[f"Q{i}"] for i in range(1, 9)},
              "totals": totals,
              "ranking": [[c, v] for c, v in ranked],
              "top1": top1, "top1_score": top1v,
              "parallel": parallel,
              "balanced_explorer": balanced,
              "profile": profile,
              "type": type_out,
              "prompts": prompts}
    if balanced:
        result["t11_directions"] = [c for c, _ in ranked[:3]]
    return result


def selftest():
    dist = {}
    fallback = 0
    for combo in itertools.product("ABCD", repeat=8):
        r = compute({f"Q{i+1}": combo[i] for i in range(8)})
        assert sum(r["totals"].values()) == sum(
            sum(SCORES[(f"Q{i+1}", combo[i])].values()) for i in range(8)), combo
        t = r["type"]
        key = t["code"] or "FALLBACK"
        dist[key] = dist.get(key, 0) + 1
        fallback += t["fallback"]
    total = 4 ** 8
    # v1.2.1：stdin 解析回归（第 1 例为十测实测平台形态——分轮字段 + 哈希 query，当时因不识别分轮字段而
    # answers_not_found，模型被迫估分输出错误类型 T11，实际应为 T1 兜底）
    # v1.2.3：第 7 例为十二测实测平台形态（answers_round1/answers_round2——answers 在前 round 在后，
    # v1.2.2 ROUND_RE 只认 round 在前 → answers_not_found，模型被迫估分编造 T11，实际 AACC+AACC
    # 应为语言沟通 6 分第一、fallback T8）
    # v1.2.6：第 8/9/10 例为十五测实测形态——planner 又自创键名 assessment_round1/assessment_round2
    # （白名单漏识别 → answers_not_found → 模型估分编造 T11，实际 AAAAAAAA 应为实验探究/体育竞技
    # 各 5 分并列第一、balanced_explorer=false、fallback T4）；第 9 例为 8 连字母自创键兜底；
    # 第 10 例为负例——无轮次/作答词干的键（turn1）不得吞作答
    # v1.2.7：第 11/12 例为十六测实测形态——家长以数字选项作答（"1111"/"1234"），脚本按
    # 1/2/3/4 → A/B/C/D 序位映射（对话层对位规则同步扩容，见 SKILL.md §三b）
    parse_cases = [
        ({"parameters": {"age": "7岁", "city": "杭州", "budget": "约1w/年",
                         "round1_answers": "ABCD", "round2_answers": "ABCD"},
          "round1_answers": "ABCD", "round2_answers": "ABCD",
          "query": {"length": 4, "sha256": "x", "preview": "…"},
          "messages": [{"role": "user", "content": {"length": 4, "sha256": "x", "preview": "…"}}]},
         "ABCDABCD"),
        ({"round_1_answers": "ACDA", "round_2_answers": "BCAB"}, "ACDABCAB"),
        ({"第一轮作答": ["A", "C", "D", "A"], "第2轮": "BCAB"}, "ACDABCAB"),
        ({"r1": "ACDA", "r2_answers": "1B 2C 3A 4B"}, "ACDABCAB"),
        ({"answers": "ACDABCAB"}, "ACDABCAB"),
        ({"parameters": {"round1_answers": "ACDA"}, "query": "ABCD"}, None),  # 仅一轮 → 不完整
        ({"parameters": {"age": 9, "city": "杭州", "grade": "四年级", "budget": 10000,
                         "answers_round1": "AACC", "answers_round2": "AACC"},
          "answers_round1": "AACC", "answers_round2": "AACC",
          "query": {"length": 4, "sha256": "x", "preview": "…"},
          "messages": [{"role": "user", "content": {"length": 4, "sha256": "x", "preview": "…"}}]},
         "AACCAACC"),
        ({"parameters": {"age": 8, "city": "杭州", "grade": "三年级", "budget": 10000,
                         "assessment_round1": "AAAA", "assessment_round2": "AAAA"},
          "query": {"length": 4, "sha256": "x", "preview": "…"},
          "messages": [{"role": "user", "content": {"length": 4, "sha256": "x", "preview": "…"}}]},
         "AAAAAAAA"),
        ({"parameters": {"assessment_answers": "AAAAAAAA"},
          "query": {"length": 4, "sha256": "x", "preview": "…"}}, "AAAAAAAA"),
        ({"parameters": {"turn1": "ABCD", "flag1": 1}}, False),  # 负例：无词干键不吞
        # v1.2.7：数字选项作答（十六测实测——家长回 "aaaa"/"1234" 形态）
        ({"parameters": {"age": 7, "assessment_round1": "1111", "assessment_round2": "1234"},
          "query": {"length": 4, "sha256": "x", "preview": "…"}}, "AAAAABCD"),
        ({"parameters": {"answers": "11111111"}, "query": {"length": 8, "sha256": "x", "preview": "…"},
          "messages": [{"role": "user", "content": {"length": 8, "sha256": "x", "preview": "…"}}]},
         "AAAAAAAA"),
        # v1.2.11：六十测实测形态——第 2 轮按全局题号作答（绝对 5-8）
        ({"parameters": {"age": 10, "city": "宁波", "grade": "五年级", "budget": "不超过2万",
                         "answers_round1": "1C,2B,3C,4B", "answers_round2": "5B,6A,7B,8D"},
          "query": {"length": 12, "sha256": "x", "preview": "…"}}, "CBCBBABD"),
        # v1.2.11：六十一测实测形态——无分隔符小写编号串 + 纯字母第 2 轮
        ({"parameters": {"city": "杭州", "grade": "四年级", "budget": "一两万",
                         "answers_round1": "1c2c3d4c", "answers_round2": "bdac"},
          "query": {"length": 8, "sha256": "x", "preview": "…"}}, "CCDCBDAC"),
        # v1.2.11：无分隔符编号对 8 题全（answers 整字段）
        ({"parameters": {"answers": "1C2B3C4B5B6A7B8D"},
          "query": {"length": 16, "sha256": "x", "preview": "…"}}, "CBCBBABD"),
        # v1.2.19：十七测实测形态——家长答小写单轮 "aaaa"（旧版 4 字母在 answers 整字段无落点
        # → answers_not_found → 模型误判"作答无效"要求按 1A 格式重答）；现按第 1 轮 Q1–Q4 登记
        ({"parameters": {"age": 9, "city": "杭州", "grade": "四年级", "budget": 10000,
                         "answers": "aaaa"},
          "query": {"length": 4, "sha256": "x", "preview": "…"},
          "messages": [{"role": "user", "content": {"length": 4, "sha256": "x", "preview": "…"}}]},
         None),
        # v1.2.19：单轮数字串 + 分隔符形态 + 整轮同选批量短语
        ({"answers": "1234"}, None),
        ({"answers": "A A A A A A A A"}, "AAAAAAAA"),
        ({"answers": "a,a,a,a,a,a,a,a"}, "AAAAAAAA"),
        ({"answers": "全选A"}, "AAAAAAAA"),
        ({"answers": "都选b"}, "BBBBBBBB"),
        ({"parameters": {"answers_round1": "aaaa", "answers_round2": "AAAA"},
          "query": {"length": 4, "sha256": "x", "preview": "…"}}, "AAAAAAAA"),
    ]
    for payload, expect in parse_cases:
        got, _src = _resolve_answers(payload)
        got8 = "".join(got.get(f"Q{i}", "?") for i in range(1, 9))
        if expect is False:
            assert not got, (payload, got8)
        elif expect:
            assert got8 == expect, (payload, got8)
        else:
            assert len(got) == 4, (payload, got8)
    print(json.dumps({"combinations": total, "fallback_count": fallback,
                      "fallback_rate": round(fallback / total, 4),
                      "type_distribution": dict(sorted(dist.items())),
                      "parse_regression": len(parse_cases)},
                     ensure_ascii=False, indent=2))
    return 0


# ---------------- stdin 平台约定适配（确定性映射，不猜测） ----------------

TEXT_KEYS = {"query", "user_input", "text", "input", "content", "question",
             "message", "answer_text", "utterance"}
LIST_KEYS = {"messages", "history", "conversation", "msgs", "chats", "dialog"}
# v1.2.1：分轮字段（十测实测 round1_answers/round2_answers；兼容 rN / round_N_answers / 第N轮(作答) / 轮N）
# v1.2.3：兼容 answers_roundN 键序（十二测实测：平台把分轮作答放 parameters.answers_round1/answers_round2，
# answers 在前 round 在后，v1.2.2 ROUND_RE 只认 round 在前 → answers_not_found，模型被迫估分编造 T11）
# v1.2.6：白名单精确匹配升级为轮次模式识别——十五测实测 planner 又自创 assessment_round1/assessment_round2
# 键名（键名每次实测都在漂移：round1_answers → answers_round1 → assessment_round1），锚定正则枚举穷举
# 不胜其烦，改为搜索式：键名任意位置含 round1/round2、r1/r2（词边界）、第N轮/N轮 即按该轮解析。
# 安全边界：turn1/grade1/address1 等不含轮次词干（r 前有字母、无 round、无"轮"字）不命中；
# "q1" 等单题键无轮次标识不命中，仍走 QKEY 单题分支——与"第N题"区分的旧约束保持。
ROUND_EN_RE = re.compile(r"round[\s_-]*([12])(?![0-9])")
ROUND_CN_RE = re.compile(r"第\s*([一二12])\s*轮|(?<![0-9])([12])\s*轮|^轮\s*([12])")
ROUND_R_RE = re.compile(r"(?<![a-z0-9])r[\s_-]*([12])(?![0-9])")
ROUND_NUM = {"1": 1, "2": 2, "一": 1, "二": 2}
# v1.2.6：作答字段从精确键名集合升级为词干识别（assessment_answers 等自创键名也能命中；
# "unanswered" 之类负义词干的值经 _merge_answer_value 确定性解析，非作答文本自然落空）
ANSWER_STEM_RE = re.compile(r"answer|ans|作答|assessment|quiz")
PURE4_RE = re.compile(r"^[A-Da-d]{4}$")
# v1.2.7：4 位数字串（十六测实测家长以数字选项作答——"1234" 序位映射 ABCD）
PURE4D_RE = re.compile(r"^[1-4]{4}$")
QKEY_RE = re.compile(r"^(?:q|Q|题|第)?\s*([1-8])\s*(?:题)?$")


def _round_of_key(ks):
    # 轮次模式识别（v1.2.6）：round 优先（其内含的 r 不满足 r 分支词边界），其次中文轮次，最后 r1/r2
    s = ks.strip().lower()
    m = ROUND_EN_RE.search(s)
    if m:
        return int(m.group(1))
    m = ROUND_CN_RE.search(s)
    if m:
        g = m.group(1) or m.group(2) or m.group(3)
        return ROUND_NUM[g]
    m = ROUND_R_RE.search(s)
    if m:
        return int(m.group(1))
    return None
# 编号式作答："1A" / "Q1A" / "题1A" / "第1题选A" / "2: c"；字母后紧跟字母数字或汉字不采（防"3D电影"误判）
NUM_RE = re.compile(r"(?<![A-Za-z0-9])(?:[Qq]|题|第)?\s*([1-8])\s*(?:题)?\s*[:：.、\-—]?\s*(?:选)?\s*"
                    r"([A-Da-d])(?![A-Za-z0-9])(?![\u4e00-\u9fff])")
COMPACT_RE = re.compile(r"(?<![A-Za-z0-9])([A-Da-d]{8})(?![A-Za-z0-9])")
# v1.2.7：8 位数字紧凑串（作答字段值专用；文本扫描侧同样仅经唯一性闸门使用）
COMPACT_DIGIT_RE = re.compile(r"(?<![A-Za-z0-9])([1-4]{8})(?![A-Za-z0-9])")
PLACEHOLDER_RE = re.compile(r"^<[^<>]*>$")


def _is_placeholder(v):
    return isinstance(v, str) and bool(PLACEHOLDER_RE.fullmatch(v.strip()))


def _letter(v):
    # v1.2.7：结构化值支持数字选项——"1"/"2"/"3"/"4" 映射 A/B/C/D（十六测实测家长以数字串作答；
    # 仅作用于结构化字段值，文本扫描分支不认数字，防预算/年龄等数字噪声误判）
    if v is None:
        return None
    s = str(v).strip()
    if s in ("1", "2", "3", "4"):
        return "ABCD"[int(s) - 1]
    s = s.upper()
    if not s or s[0] not in "ABCD":
        return None
    rest = s[1:]
    if rest and re.match(r"[A-Za-z0-9]", rest):
        return None
    return s[0]


def _numbered_from_text(t):
    out = {}
    for m in NUM_RE.finditer(t):
        out[f"Q{m.group(1)}"] = m.group(2).upper()
    return out


# v1.2.19：整轮同选批量短语——"全选A"/"全部选A"/"都选a"/"统一选B"（十七测实测形态）
BATCH_RE = re.compile(r"(?:全部|全|都|统一)\s*(?:选|填|答)?\s*([A-Da-d])")
# v1.2.19：分隔符容错——"A A A A" / "A-A-A-A" / "A、A、A、A" / "a,a,a,a"
SEP_ONLY_RE = re.compile(r"[A-Da-d1-4\s,，、\-—/·.]+")


def _merge_answer_text(t, out):
    found = _numbered_from_text(t)
    for qn, L in found.items():
        out.setdefault(qn, L)
    if not found:
        # v1.2.19：先剥分隔符（仅当整串由字母/数字/分隔符组成，防误吞含汉字的自然语句）
        if t and SEP_ONLY_RE.fullmatch(t):
            compact = re.sub(r"[\s,，、\-—/·.]+", "", t)
            if re.fullmatch(r"[A-Da-d]{4}|[A-Da-d]{8}|[1-4]{4}|[1-4]{8}", compact):
                t = compact
        m = COMPACT_RE.search(t)
        if m:
            s = m.group(1).upper()
            for i in range(8):
                out.setdefault(f"Q{i+1}", s[i])
            return
        # v1.2.7：8 位纯数字串——"11111111" → 序位映射 ABCD（作答字段的值，非文本扫描）
        m = COMPACT_DIGIT_RE.search(t)
        if m:
            s = m.group(1)
            for i in range(8):
                out.setdefault(f"Q{i+1}", "ABCD"[int(s[i]) - 1])
            return
        # v1.2.11：无分隔符编号对（8 题全）——"1C2B3C4B5B6A7B8D"（六十/六十一测实测形态：
        # NUM_RE 的前后瞻为防"3D电影"类噪声会吞掉无分隔符逐对相邻串，故整串锚定解析；绝对题号 1-8）
        m = re.fullmatch(r"(?:[1-8][A-Da-d]){8}", t)
        if m:
            for i in range(8):
                out.setdefault(f"Q{i+1}", m.group(0)[2 * i + 1].upper())
            return
        # v1.2.11：4 对无分隔符（一轮 4 题）——"1a2b3c4d"（同上理由；落在 answers 整字段时按 Q1-Q4
        # 解析、后续按 missing 补问，分轮字段里经 _merge_round_value 加偏移）
        m = re.fullmatch(r"(?:[1-4][A-Da-d]){4}", t)
        if m:
            for i in range(4):
                out.setdefault(f"Q{i+1}", m.group(0)[2 * i + 1].upper())
            return
        # v1.2.19：整轮同选批量短语——"全选A" → Q1–Q8 全 A（十七测实测形态）
        m = BATCH_RE.fullmatch(t.strip())
        if m:
            L = m.group(1).upper()
            for i in range(8):
                out.setdefault(f"Q{i+1}", L)
            return
        # v1.2.19：单轮 4 连字母/4 位数字串落在 answers 整字段——按第 1 轮 Q1–Q4 解析，
        # 由 main() 返回 answers_incomplete 提示回捞第 2 轮（十七测实测：家长答 "aaaa"/"AAAA"
        # 因 4 字母无落点返回 answers_not_found，模型误判"作答无效"要求按 1A 格式重答）
        m = re.fullmatch(r"([A-Da-d]{4})", t)
        if m:
            for i in range(4):
                out.setdefault(f"Q{i+1}", m.group(1)[i].upper())
            return
        m = re.fullmatch(r"([1-4]{4})", t)
        if m:
            for i in range(4):
                out.setdefault(f"Q{i+1}", "ABCD"[int(m.group(1)[i]) - 1])


def _merge_answer_value(v, out):
    if isinstance(v, dict):
        for k, val in v.items():
            m = QKEY_RE.match(str(k).strip())
            if m:
                L = _letter(val)
                if L:
                    out.setdefault(f"Q{m.group(1)}", L)
    elif isinstance(v, list):
        letters = [_letter(x) for x in v]
        if len(letters) == 8 and all(letters):
            for i, L in enumerate(letters):
                out.setdefault(f"Q{i+1}", L)
        else:
            _merge_answer_text(" ".join(str(x) for x in v), out)
    elif isinstance(v, str):
        _merge_answer_text(v, out)


def _merge_round_value(v, offset, out):
    # offset 0 → 第 1 轮（Q1–Q4）；offset 4 → 第 2 轮（Q5–Q8）；仅位置映射，不猜测
    if isinstance(v, dict):
        for k, val in v.items():
            m = QKEY_RE.match(str(k).strip())
            if m and 1 <= int(m.group(1)) <= 4:
                L = _letter(val)
                if L:
                    out.setdefault(f"Q{offset + int(m.group(1))}", L)
        return
    if isinstance(v, list):
        letters = [_letter(x) for x in v]
        if len(letters) == 4 and all(letters):
            for i, L in enumerate(letters):
                out.setdefault(f"Q{offset + i + 1}", L)
            return
        v = " ".join(str(x) for x in v)
    if isinstance(v, str):
        t = v.strip()
        if re.fullmatch(r"[A-Da-d]{4}", t):
            for i in range(4):
                out.setdefault(f"Q{offset + i + 1}", t[i].upper())
            return
        # v1.2.7：纯数字串作答——"1111" → ABCD 序位映射（十六测实测形态）
        if re.fullmatch(r"[1-4]{4}", t):
            for i in range(4):
                out.setdefault(f"Q{offset + i + 1}", "ABCD"[int(t[i]) - 1])
            return
        # v1.2.11：无分隔符编号对——"1c2c3d4c"（六十一测实测形态；相对题号 1-4 + 偏移）
        m = re.fullmatch(r"(?:[1-4][A-Da-d]){4}", t)
        if m:
            for i in range(4):
                out.setdefault(f"Q{offset + i + 1}", m.group(0)[2 * i + 1].upper())
            return
        for qn, L in _numbered_from_text(t).items():
            n = int(qn[1:])
            if 1 <= n <= 4:
                out.setdefault(f"Q{offset + n}", L)
            # v1.2.11：第 2 轮用绝对题号 5-8（六十测实测形态——家长答完前 4 题后按全局题号作答
            # "5B,6A,7B,8D"；仅第 2 轮认 5-8，第 1 轮出现 5-8 仍是噪声不采）
            elif offset and offset + 1 <= n <= offset + 4:
                out.setdefault(f"Q{n}", L)


def _collect_structured(obj, out, depth=0):
    if depth > 4:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _is_placeholder(v):
                continue
            ks = str(k).strip()
            rn = _round_of_key(ks)
            if rn:
                _merge_round_value(v, 4 if rn == 2 else 0, out)
                continue
            if ANSWER_STEM_RE.search(ks.lower()):
                # 值兜底（v1.2.6）：作答词干键 + 纯 4 连字母值 + 键内含数字 1/2 → 按该轮
                # （answer1/quiz_round2 等自创键名；轮次词干已在上方 _round_of_key 命中）
                # v1.2.7：4 位数字串同规则（"1111" 形态）
                if isinstance(v, str):
                    t = v.strip()
                    if PURE4_RE.fullmatch(t) or PURE4D_RE.fullmatch(t):
                        m = re.search(r"[12]", ks)
                        if m:
                            _merge_round_value(t, 4 if m.group(0) == "2" else 0, out)
                            continue
                _merge_answer_value(v, out)
                continue
            qm = QKEY_RE.match(str(k).strip())
            if qm:
                L = _letter(v)
                if L:
                    out.setdefault(f"Q{qm.group(1)}", L)
                continue
            _collect_structured(v, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_structured(item, out, depth + 1)


def _collect_messages(items, texts):
    for item in items:
        if isinstance(item, dict):
            role = str(item.get("role", "")).lower()
            if role in ("assistant", "system", "ai", "bot"):
                continue
            for tk in ("content", "text", "message"):
                c = item.get(tk)
                if isinstance(c, str) and not _is_placeholder(c):
                    texts.append(c)
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            texts.append(part["text"])
                        elif isinstance(part, str):
                            texts.append(part)
        elif isinstance(item, str):
            texts.append(item)


def _collect_texts(obj, texts, depth=0):
    if depth > 4:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k).strip().lower()
            if ks in TEXT_KEYS:
                if isinstance(v, str) and not _is_placeholder(v):
                    texts.append(v)
                elif isinstance(v, list):
                    texts.extend(x for x in v if isinstance(x, str))
            elif ks in LIST_KEYS and isinstance(v, list):
                _collect_messages(v, texts)
            else:
                _collect_texts(v, texts, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_texts(item, texts, depth + 1)


def _resolve_answers(payload):
    structured = {}
    _collect_structured(payload, structured)
    texts = []
    _collect_texts(payload, texts)
    answers = dict(structured)
    sources = []
    if structured:
        sources.append("structured")
    numbered = {}
    for t in texts:
        for qn, L in _numbered_from_text(t).items():
            numbered[qn] = L
    if numbered:
        sources.append("text_numbered")
        for qn, L in numbered.items():
            if qn not in answers:
                answers[qn] = L
    if not answers and texts:
        tokens = set()
        for t in texts:
            tokens.update(m.group(1).upper() for m in COMPACT_RE.finditer(t))
            tokens.update("".join("ABCD"[int(c) - 1] for c in m.group(1))
                          for m in COMPACT_DIGIT_RE.finditer(t))
        if len(tokens) == 1:
            s = next(iter(tokens))
            answers = {f"Q{i+1}": s[i] for i in range(8)}
            sources.append("text_compact")
    return answers, "+".join(sources)


def main(payload: dict) -> dict:
    try:
        if isinstance(payload, str):
            payload = {"query": payload}
        answers, source = _resolve_answers(payload)
        if len(answers) == 8:
            result = compute(answers)
            result["ok"] = True
            result["answers_source"] = source
            return result
        ordered = {f"Q{i}": answers[f"Q{i}"] for i in range(1, 9) if f"Q{i}" in answers}
        if ordered:
            return {"ok": False, "error": "answers_incomplete",
                    "collected": ordered,
                    "missing": [f"Q{i}" for i in range(1, 9) if f"Q{i}" not in answers],
                    "hint": "作答不完整：先检查是否只传了一轮——missing 题若在前几轮已收齐"
                            "（含自由文本映射登记的对位字母），回捞合并两轮齐传重新执行，"
                            "禁止把已答题目重发；确无作答才按 missing 补问缺失题号，"
                            "禁止估分或编造结果。单轮 4 字母（如 \"aaaa\"/\"AAAA\"）"
                            "按第 1 轮 Q1–Q4 登记、属有效作答，禁止判为无效或要求家长重答。"}
        return {"ok": False, "error": "answers_not_found",
                "hint": "上下文中没有可解析的 8 题作答（支持 answers 字段、round1_answers+round2_answers "
                        "分轮字段（兼容 answers_round1/answers_round2 键序）、q1-q8 键，或文本中的 "
                        "'ACDABCAB' / '1A 2C … 8B' / 'Q1A Q2C …'；小写 \"aaaa\"、数字 \"1234\"、"
                        "分隔 \"A A A A\"、整轮同选 \"全选A\" 一律有效）。若两轮作答实际已收齐：改用 "
                        "--answers 传 8 连字母（第 1 轮 4 字母接第 2 轮 4 字母）重新执行；仍不可用时按 "
                        "references_01 第二节逐选项计分速查手动计分（8 题逐题抄录分值后按 6 类分别求和，"
                        "逐题分值之和必须等于 6 类总分之和），禁止凭印象估分或编造类型。"}
    except Exception as e:
        return {"ok": False, "error": "script_error",
                "message": f"{type(e).__name__}: {e}",
                "hint": "脚本执行异常，按 references_01 手动计分工作单计分，禁止编造结果。"}


def _run_cli():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--answers", help='8 题作答，如 "ACDABCAB" 或 "1A 2C 3D 4A 5B 6C 7A 8B"')
    ap.add_argument("--round1_answers", help="第 1 轮 4 题作答（映射 Q1–Q4），如 ACDA")
    ap.add_argument("--round2_answers", help="第 2 轮 4 题作答（映射 Q5–Q8），如 DACA")
    ap.add_argument("--query", help="本轮用户输入文本（平台以 --query 方式调用时传入，可含作答）")
    ap.add_argument("--selftest", action="store_true", help="全量 4^8 组合自检")
    # 平台可能附带 --user-id / --session-id 等额外参数，parse_known_args 静默忽略，禁止因未知参数报 usage 错误
    args, _unknown = ap.parse_known_args()
    if args.selftest:
        sys.exit(selftest())
    payloads = []
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            try:
                payloads.append(json.loads(raw))
            except json.JSONDecodeError:
                payloads.append({"query": raw})
    if args.query:
        payloads.append({"query": args.query})
    if args.answers is not None:
        payloads.append({"answers": args.answers})
    round_payload = {}
    if args.round1_answers is not None:
        round_payload["round1_answers"] = args.round1_answers
    if args.round2_answers is not None:
        round_payload["round2_answers"] = args.round2_answers
    if round_payload:
        payloads.append(round_payload)
    if not payloads:
        print(json.dumps({"ok": False, "error": "no_input",
                          "hint": "无输入：平台经 stdin 传入 JSON 上下文或以 --query 传入本轮输入；调试可用 --answers。"},
                         ensure_ascii=False))
        return
    merged = {}
    for p in payloads:
        if isinstance(p, dict):
            merged.update(p)
        elif isinstance(p, str):
            merged.update({"query": p})
    print(json.dumps(main(merged), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _run_cli()
