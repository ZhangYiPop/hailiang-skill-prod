import json
import os
import re
import sys
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")


def load_json(name):
    path = os.path.join(ASSETS_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


SCHOOLS = load_json("schools.json")
TUITION = load_json("tuition.json")
ACCOMMODATION = load_json("accommodation.json")
CITY_LIVING = load_json("city_living.json")
VISA_MEDICAL = load_json("visa_medical.json")
FX = load_json("fx.json")
CONFIG = load_json("config.json")

SCHOOL_BY_ID = {s["school_id"]: s for s in SCHOOLS}
ACCOM_BY_SCHOOL = {a["school_id"]: a for a in ACCOMMODATION}
VISA_BY_COUNTRY = {v["country"]: v for v in VISA_MEDICAL}
FX_BY_COUNTRY = {f["country"]: f for f in FX}
FX_BY_CURRENCY = {f["currency"]: f for f in FX}

CITY_LIVING_BY_KEY = {(c["country"], c["city"]): c for c in CITY_LIVING}
CITY_LIVING_BY_COUNTRY_TIER = defaultdict(list)
CITY_LIVING_BY_COUNTRY = defaultdict(list)
for c in CITY_LIVING:
    CITY_LIVING_BY_COUNTRY_TIER[(c["country"], c["city_tier"])].append(c)
    CITY_LIVING_BY_COUNTRY[c["country"]].append(c)

TUITION_BY_KEY = {}
TUITION_BY_SCHOOL = defaultdict(list)
for t in TUITION:
    if t["tuition_min"] is None or t["tuition_max"] is None:
        continue
    TUITION_BY_KEY[(t["school_id"], t["major_category"])] = t
    TUITION_BY_SCHOOL[t["school_id"]].append(t)

COUNTRY_ALIASES = CONFIG.get("country_aliases", {})
MAJOR_KEYWORDS = CONFIG.get("major_keywords", {})


def _load_questionnaire_meta():
    """问卷题目的规范顺序与标签 —— 以 assets/questionnaire.json 为唯一权威。"""
    try:
        q = load_json("questionnaire.json")
    except Exception:
        return ["countries", "major", "school_type", "city_tier", "budget_range"], {}
    ids, labels = [], {}
    for item in q.get("questions") or []:
        qid = item.get("id")
        if not qid:
            continue
        ids.append(qid)
        labels[qid] = item.get("label") or qid
    return (ids or ["countries", "major", "school_type", "city_tier", "budget_range"]), labels


QUESTION_IDS, QUESTION_LABELS = _load_questionnaire_meta()

BUDGET_BANDS = CONFIG["budget_bands"]
# 档位写法与 assets/questionnaire.json 的选项 value 保持一致（统一用「10万-20万」这种带「万」的写法）
BAND_ALIASES = {
    "10万以下": ["10万以下", "10万以内", "不到10万"],
    "10万-20万": ["10万-20万", "10-20万", "10万到20万"],
    "20万-30万": ["20万-30万", "20-30万", "20万到30万"],
    "30万-50万": ["30万-50万", "30-50万", "30万到50万"],
    "50万以上": ["50万以上", "50万及以上"],
    "暂不确定": ["暂不确定", "不确定"],
}
# 预算上限由 config.budget_bands 的 max 推导（单位：万元），改配置即生效
BAND_CAP = {
    b["code"]: (b["max"] * 10000 if b.get("max") is not None else None)
    for b in BUDGET_BANDS
}

# 「预算自由文本」兜底用的信号词。**只作用于结构化槽位**（budget_range / budget_amount），
# 不走全文扫描——全文扫描仍必须带意图词，否则助手历史里的费用区间会被读成用户预算。
#   - 带金额 + 「以上/起」= 用户只给了下界，按无上限处理（与档位「50万以上」同义）
#   - 无金额 + 下面这些词 = 表了态但没定量，等价档位「暂不确定」
BUDGET_NO_CEILING_WORDS = ["以上", "起", "往上", "起步"]
BUDGET_UNDETERMINED_WORDS = [
    "不限", "都行", "多少都行", "无所谓", "无偏好", "看情况", "随缘",
    "没想好", "还没定", "暂不确定", "不确定",
]

CURRENCY_ALIASES = {
    "美元": "USD", "美金": "USD", "USD": "USD", "usd": "USD",
    "英镑": "GBP", "GBP": "GBP", "gbp": "GBP",
    "澳元": "AUD", "澳大利亚元": "AUD", "AUD": "AUD", "aud": "AUD",
    "加元": "CAD", "加拿大元": "CAD", "CAD": "CAD", "cad": "CAD",
    "日元": "JPY", "日币": "JPY", "JPY": "JPY", "jpy": "JPY",
    "韩元": "KRW", "韩币": "KRW", "KRW": "KRW", "krw": "KRW",
    "新币": "SGD", "新加坡元": "SGD", "SGD": "SGD", "sgd": "SGD",
    "港币": "HKD", "港元": "HKD", "HKD": "HKD", "hkd": "HKD",
    "马币": "MYR", "林吉特": "MYR", "MYR": "MYR", "myr": "MYR",
    "新西兰元": "NZD", "纽币": "NZD", "NZD": "NZD", "nzd": "NZD",
    "欧元": "EUR", "EUR": "EUR", "eur": "EUR",
    "法郎": "CHF", "瑞郎": "CHF", "瑞士法郎": "CHF", "CHF": "CHF", "chf": "CHF",
    "卢布": "RUB", "RUB": "RUB", "rub": "RUB",
}
CURRENCY_ALIASES_SORTED = sorted(CURRENCY_ALIASES.keys(), key=len, reverse=True)

SCHOOL_STOPWORDS = [
    "国外", "海外", "外国", "中国", "国内", "目标", "计划",
    # 院校类型词 / 城市层级词 / 修饰语：出现在片段里说明是「偏好」，不是校名
    # （「美国公立大学」「想读好的大学」「去一线城市的大学」都会被这组词挡下）
    "公立", "私立", "综合", "一线", "二线", "三线",
    "好的", "排名靠前", "知名", "读大学", "的大学",
]

# 学段护栏：命中关键词且全文未出现「本科」时，判定超出范围，直接兜底、不给数字
OUT_OF_SCOPE_KEYWORDS = [
    "硕士", "博士", "研究生", "mba", "预科", "国际大一", "语言班", "桥梁课程",
    "大专", "专科", "专升本", "非学历", "成人教育", "postgraduate", "master", "phd",
]
OUT_OF_SCOPE_PHRASES = [
    "高中留学", "初中留学", "小学留学", "中学留学", "低龄留学",
    "出国读高中", "出国读初中", "去国外读高中", "申请高中", "高中及以下", "初高中留学",
]
# 低龄留学：K12 学段词 + 留学/就读意图（刻意排除「高中在读」这类只描述身份的表述）
K12_WORDS = ["高中", "初中", "小学", "中学"]
K12_INTENT_AFTER = ["留学", "出国", "升学", "申请", "就读", "费用", "学费", "多少钱", "预科"]
K12_INTENT_BEFORE = ["想", "要", "去", "读", "念", "上", "申请", "就读", "升", "打算", "准备", "考虑"]
BACHELOR_KEYWORDS = ["本科", "undergraduate", "bachelor"]

# 范围外地区：库内不收录，命中即说明范围、不给数字（别名表在 config.fallback.unsupported_regions）
UNSUPPORTED_REGIONS = CONFIG["fallback"].get("unsupported_regions", {})

# 校名前的动词/意图前缀，剥离后再判校名（「想读伦敦大学学院」也能命中）
SCHOOL_TOKEN_PREFIXES = [
    "想读", "想上", "想学", "想去", "打算", "计划", "准备", "考虑", "申请", "报考", "就读",
    "留学", "出国", "去", "读", "上", "选", "念", "学",
]

# 预算金额的「意图词」：数字前后需出现这些词才认定为用户预算，
# 避免把助手历史回复里的费用区间（如「折合人民币约 32 - 46 万」）误读成预算。
BUDGET_INTENT_PREFIX = [
    "预算", "家里", "家庭", "每年", "一年", "最多", "不超过", "控制在", "能出",
    "准备", "打算", "计划", "预计", "一共", "总共", "只能", "只有",
]
BUDGET_INTENT_SUFFIX = ["够不够", "够", "可以吗", "行吗", "能去", "能读", "能上", "能覆盖", "行不行"]

# 显式金额槽位（单位：万元，与表单标签一致）
BUDGET_AMOUNT_KEYS = [
    "budget_amount", "budgetamount", "budget_amount_wan", "budget_amount_cny",
    "年预算具体金额", "年预算金额", "预算金额", "预算具体金额",
]


# ---------- 专业识别的「意图词窗口」与「校名区间屏蔽」（方案 C） ----------
# 背景：专业关键词是全文扫描的，而很多校名本身含专业词（巴黎「理工」学院 / 香港「教育」大学 /
# 伦敦「政治经济学」院）。旧逻辑「先命中先赢」不看位置，于是校名里的专业词把用户明说的专业顶掉
# （166 校 x 6 专业 = 996 组里 36 组误判）。两条修正同时上：
#   A. 屏蔽：命中区间完全落在「校名/校名别名」区间内的，不算专业命中。
#   B. 意图优先：用户明说专业时通常带意图词（想读/读的是…），先在意图词之后的窗口里取；
#      窗口内没有，再退回全文（**仍带 A 的屏蔽**）。
# 注意意图词表刻意不含裸「学」「读」——「医学」「数学」「科学」里的「学」会被当成意图词，
# 反而开出一个落在专业词之后的假窗口。要表达「读XX」用 MAJOR_INTENT_PAIRS 的组合式。
MAJOR_INTENT_WORDS = [
    "想读", "要读", "想学", "要学", "报考", "申请",
    "专业是", "专业方向", "方向是", "读的是", "学的是",
]
# 「读XX专业 / 学XX方向」这类带中间字的组合式：(前缀, 后缀)，中间最多 3 个非标点字符
MAJOR_INTENT_PAIRS = [("读", "专业"), ("学", "专业"), ("读", "方向"), ("学", "方向")]
# 意图词之后取多长的窗口做专业匹配
MAJOR_INTENT_WINDOW = 10


# ---------- 上下文解析 ----------

# 承载「用户本轮原话」的键：这些键下的文本才参与槽位扫描。
# 其余键（尤其是 messages / recent_messages 里的 assistant 段）只做上下文承接，
# 不提供槽位——助手自己的追问列着全套枚举选项（「商科 / 文科 / …」「公立 / 私立 / …」），
# 一旦参与扫描就会把用户真正的选择顶掉（详见「结构化槽位优先」）。
SLOT_TEXT_KEYS = ("query", "用户消息", "user_message", "用户原话", "message")

# 结构化槽位键 -> slots 字段名（平台表单 / planner 直接给出的键，优先级最高）
# 「目标留学国家/地区」的「未指定」写法。问卷里对应「暂不确定」选项，语义 = 全部收录
# 国家/地区都算一遍（与「一个国家都不选」等价）。因为问卷里该字段是必填多选，用户想表达
# 「都可以」时只能选这个，所以引擎必须把字面量也认下来，不能原样透传。
COUNTRY_UNDETERMINED_TOKENS = ("暂不确定", "不确定", "还没确定", "未确定", "待定")

EXPLICIT_SLOT_KEYS = {
    "major": "major",
    "school_type": "school_type",
    "city_tier": "city_tier",
    "budget_range": "budget_range",
    "countries": "countries",
}

# 「学校」不在 EXPLICIT_SLOT_KEYS 里：那套表的键都走 _norm_slot_choice() 做「值 -> 规范值」
# 的枚举归一，而校名是**自由文本**（中文名 / 英文名 / 别名 / 一句话），得走学校匹配。
# 平台 planner 其实早就把学校解好了：第三十一次诊断实测，港大会话里它同时给过
# `school`（单校）与 `schools: ["香港大学","墨尔本大学"]`（复数），而旧引擎一个都没读，
# 只能靠扫文本重新猜 —— 文本里校名被国名吞掉、或长短名互抢时就会**静默丢校**。
SCHOOL_SLOT_KEYS = ("school", "schools", "school_name", "school_names")

# 并列 / 对比信号词。只有「同一段文本里命中 >= 2 所学校」时才有意义：
# 有它 = 用户在做对比（一所都不能丢），没有它 = 多半是长名把短名吞了（沿用旧的支配判据）。
SCHOOL_COMPARE_TOKENS = ("和", "与", "跟", "、", "以及", "及", "还是",
                         "对比", "比较", "哪个", "哪所", "哪家", "谁", "vs", "VS", "Vs")

# 表单 / planner 缺项时用来占位的空值字样，命中即视为「该维度未给」
SLOT_EMPTY_TOKENS = ("", "不限", "无偏好", "任意", "全部", "无所谓", "待定", "-", "—")

# 结构化槽位的英文写法补充表。**键 = 库内规范值、值 = 各种写法**，
# 与 MAJOR_KEYWORDS / BAND_ALIASES / COUNTRY_ALIASES 方向一致；
# 这样 _norm_slot_choice() 命中后返回的永远是「键」，即库内认的中文规范值，
# 不会把 'any' / 'business' 这类英文原样透出去。
SLOT_VALUE_ALIASES = {
    "major": {
        "商科": ["business", "commerce", "business administration", "mba", "finance", "economics", "management", "marketing"],
        "文科": ["humanities", "liberal arts", "arts and humanities", "law", "journalism", "education"],
        "理科": ["science", "sciences", "natural science", "math", "mathematics", "physics", "chemistry", "biology"],
        "工科": ["engineering", "computer science", "computer", "cs", "software engineering", "information technology"],
        "医学": ["medicine", "medical", "clinical medicine", "pharmacy", "nursing", "dentistry"],
        "艺术": ["art", "arts", "fine arts", "design", "visual arts", "music", "architecture"],
        "不限": ["any", "all", "no preference", "nopreference", "无偏好", "任意", "都可以", "无所谓"],
    },
    "school_type": {
        "公立": ["public", "state", "public university"],
        "私立": ["private", "private university"],
        "无偏好": ["any", "no preference", "nopreference", "不限", "任意", "都可以", "无所谓"],
    },
    "city_tier": {
        "一线": ["tier1", "tier 1", "first tier", "first-tier", "first"],
        "二三线": ["tier2", "tier 2", "tier23", "tier 2 & 3", "second tier", "second", "2030"],
        "无偏好": ["any", "no preference", "nopreference", "不限", "任意", "都可以", "无所谓"],
    },
}


def _slot_text(payload):
    """只取「用户本轮原话」参与槽位扫描；显式键缺位时回落到全部文本。

    返回 (用于扫描的文本, [[{role, content}]] 结构里的助手段是否被剔除)。
    """
    current, _ = _slot_text_split(payload)
    if current:
        return current
    texts = []
    _deep_collect(payload, texts)
    return " ".join(texts)


def _slot_text_split(payload):
    """把扫描文本拆成「本轮」与「历史」两段，用于区分槽位的时效性。

    背景（2026-09-21 线上会话 rev_646c933e 实测）：
    用户第 1 轮说「一年25万」，之后每轮平台都把完整用户历史注入 payload 的
    `messages`。旧的 `_slot_text()` 把历史一并拼进同一段文本，导致
    `parse_budget_amount()` 每一轮都重新扫出 250000 并覆盖档位算出的上限
    —— 用户后来改口「50万以上」完全失效（countries 恒为空）。

    因此金额这一类「会被后续轮次改写」的槽位必须区分时效：
    **本轮的金额才算数；历史金额只作为兜底，且不得压过本轮给出的档位。**

    返回 (current_text, history_text)：
    - current_text：本轮用户原话（SLOT_TEXT_KEYS 命中的键，以及 messages 里最后一条 user）
    - history_text：更早的历史用户消息（只承接国家/专业这类不冲突的槽位）
    """
    current_parts = []
    history_parts = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and k in SLOT_TEXT_KEYS and v.strip():
                    current_parts.append(v)
                elif isinstance(v, list) and k in ("messages", "recent_messages"):
                    users = [item.get("content") for item in v
                             if (isinstance(item, dict)
                                 and str(item.get("role", "")).lower() == "user"
                                 and isinstance(item.get("content"), str)
                                 and item.get("content").strip())]
                    # 最后一条 user = 本轮；其余为历史
                    if users:
                        current_parts.append(users[-1])
                        history_parts.extend(users[:-1])
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    return " ".join(current_parts), " ".join(history_parts)


def _deep_collect(obj, out):
    """把 payload 递归摊平成字符串列表（兜底通道：仅在 slot_text() 取不到用户原话时使用）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            _deep_collect(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _deep_collect(item, out)
    elif isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, (int, float)):
        out.append(str(obj))


def _in_cny_range(cny):
    return cny is not None and 5000 <= cny <= 50000000


def parse_budget_amount(text):
    """从自然语言里识别预算金额。

    只有在数字前后出现「预算意图词」（预算 40 万 / 家里一年 20 万 / 4 万美元够吗）时才认定，
    避免把助手历史回复里的费用区间误读成用户预算。
    """
    currency_pattern = "|".join(re.escape(k) for k in CURRENCY_ALIASES_SORTED)
    prefix_alt = "|".join(re.escape(k) for k in BUDGET_INTENT_PREFIX)
    suffix_alt = "|".join(re.escape(k) for k in BUDGET_INTENT_SUFFIX)

    # 1) 意图词在前 + 外币：预算 4 万美元 / 家里一年 3 万英镑
    pattern = (r"(" + prefix_alt + r")[^\d]{0,10}(\d+(?:\.\d+)?)\s*(万|w|W)?\s*("
               + currency_pattern + r")")
    for m in re.finditer(pattern, text):
        amount = float(m.group(2))
        if m.group(3):
            amount *= 10000
        currency = CURRENCY_ALIASES.get(m.group(4))
        if not currency:
            continue
        fx = FX_BY_CURRENCY.get(currency)
        if not fx:
            continue
        cny = amount * fx["cny_per_unit"]
        if _in_cny_range(cny):
            return cny, currency

    # 2) 意图词在前 + 万元：预算 20 万 / 家里一年 20 万
    for m in re.finditer(r"(?:" + prefix_alt + r")[^\d]{0,10}(\d+(?:\.\d+)?)\s*(万|w|W)", text):
        # 「预算 20 万-30 万」是区间，上限由档位决定，这里不取单值
        if re.match(r"\s*[-~—到至]\s*\d", text[m.end():m.end() + 8]):
            continue
        cny = float(m.group(1)) * 10000
        if _in_cny_range(cny):
            return cny, "CNY"

    # 3) 金额在前、意图词在后：4 万美元够吗 / 20 万够不够留学
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|w|W)?\s*(" + currency_pattern + r")?[^\d]{0,4}(?:"
                  + suffix_alt + r")", text)
    if m:
        currency = CURRENCY_ALIASES.get(m.group(3)) if m.group(3) else "CNY"
        if currency:
            amount = float(m.group(1))
            if m.group(2):
                amount *= 10000
            elif currency == "CNY":
                # 纯数字（“40 够吗”）不认定为预算金额
                return None, None
            fx = FX_BY_CURRENCY.get(currency)
            if fx:
                cny = amount * fx["cny_per_unit"]
                if _in_cny_range(cny):
                    return cny, currency

    return None, None


def parse_budget_free_text(value):
    """结构化预算槽位里的「非档位文本」兜底判定。返回 (kind, cny)。

    背景：`_norm_slot_choice()` 认不出时会**原样透出**槽位值，于是
    `slots["budget_range"]` 是一个不在 6 个档位里的自由文本（「8万以下」「不限」）：
    `band` 匹配不到 → 既不给上限、也不置「暂不确定」→ **筛选静默不执行**，
    但 `budget_range` 非空 ⇒ `budget_known=True` ⇒ 表单也不会再问预算。
    一句话：用户给了预算，系统既没按预算筛、也不再问他，且不解释。

    处理分三级：
      L1 带金额     → "cap"（当**上限**；无方向词也当上限）+ 人民币金额
                      带「以上/起」→ "no_ceiling"（只给了下界，等价档位「50万以上」）
      L2 无金额但表态 → "undetermined"（等价档位「暂不确定」）
      L3 其余垃圾值   → (None, None) 交回原流程，**行为与改动前逐字一致**
    """
    if not isinstance(value, str):
        return None, None
    s = re.sub(r"\s+", "", value)
    if not s:
        return None, None
    if any(w in s for w in BUDGET_UNDETERMINED_WORDS) and not re.search(r"\d", s):
        return "undetermined", None
    # 「10万-20万」这类区间是档位的活，不在这里取单值
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if len(nums) != 1:
        return None, None
    if re.search(r"万|w|W", s):
        cny = float(nums[0]) * 10000
    else:
        n = float(nums[0])
        cny = n * 10000 if n <= 500 else n
    if not _in_cny_range(cny):
        return None, None
    if any(w in s for w in BUDGET_NO_CEILING_WORDS):
        return "no_ceiling", None
    return "cap", cny


def _slot_amount_to_cny(value):
    """显式金额槽位 -> 人民币。槽位单位是「万元」（与表单标签一致）；明显是元的（>500）按元处理。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n <= 0:
            return None
        cny = n * 10000 if n <= 500 else n
        return cny if _in_cny_range(cny) else None
    if isinstance(value, str):
        s = re.sub(r"\s+", "", value).replace(",", "").replace("，", "")
        m = re.fullmatch(r"(\d+(?:\.\d+)?)(万|w|W)?(?:元|人民币)?", s)
        if not m:
            # 带修饰语的说法（「15万左右」「8万以下」）走兜底：能抽出唯一金额就当上限；
            # 「30万以上」只给了下界、不能当上限，交回原流程（与改动前一致）。
            kind, cny = parse_budget_free_text(value)
            return cny if kind == "cap" else None
        n = float(m.group(1))
        if m.group(2):
            cny = n * 10000
        else:
            cny = n * 10000 if n <= 500 else n
        return cny if _in_cny_range(cny) else None
    return None


def find_budget_amount_slot(obj):
    """从 payload 里读显式金额槽位（键名含 budget_amount / 预算金额 等）。返回 (人民币, 键名)。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k).lower()
            if any(t.lower() in key for t in BUDGET_AMOUNT_KEYS):
                cny = _slot_amount_to_cny(v)
                if cny is not None:
                    return cny, str(k)
            cny, src = find_budget_amount_slot(v)
            if cny is not None:
                return cny, src
    elif isinstance(obj, list):
        for item in obj:
            cny, src = find_budget_amount_slot(item)
            if cny is not None:
                return cny, src
    return None, None


def _major_scan_all(text, mask_spans=()):
    """专业关键词的全文扫描。返回 [(pos, major, kw), ...]，只保留每个专业最早的一处命中。

    落在 mask_spans（校名区间）内的命中不算——这就是方案 A。
    """
    hits = []
    for major, keywords in MAJOR_KEYWORDS.items():
        for kw in sorted(keywords, key=len, reverse=True):
            if not kw:
                continue
            for span in _find_spans(text, kw):
                if _inside_any(span, mask_spans):
                    continue
                hits.append((span[0], major, kw))
                break
    return hits


def _major_scan_first(text, mask_spans=()):
    """全文扫描里最靠前的一处命中；没有则 None。返回 (pos, major, kw)。"""
    hits = _major_scan_all(text, mask_spans)
    if not hits:
        return None
    # 同位置并列时长词优先（第三十五次）：此前只按 pos 排序，并列时靠 MAJOR_KEYWORDS 的
    # 字典顺序（商→文→理→工→医→艺）决出，短词恒胜 ——「生物医学工程」被判成理科（「生物」）
    # 而不是工科（「生物医学工程」）。这条判据与 _major_attr_fallback() 的长词优先对齐。
    hits.sort(key=lambda x: (x[0], -len(x[2])))
    return hits[0]


# 「不限」的预算语境词：紧邻「不限」时说明用户说的是钱，不是专业。
_UNLIMITED_BUDGET_WORDS = ("预算", "费用", "花销", "开销", "金额", "学费", "成本")


def _is_major_unlimited(text):
    """文本里的「不限」是否指**专业不限**（而不是预算不限）。

    判据只看「不限」的左右各 2 字（取并集）：
      - 出现预算语境词（预算/费用/金额…）→ 是「预算不限」，不算专业哨兵
      - 否则一律算专业哨兵（「专业不限」「不限专业」「改成不限」「不限」）
    左右各 2 字的小窗口是刻意的：窗口一放大，「费用不限但专业想读商科」这类句子
    会把真正该生效的「不限」也误判掉。
    """
    if not text or "不限" not in text:
        return False
    for m in re.finditer("不限", text):
        i, j = m.start(), m.end()
        near = text[max(0, i - 2):i] + text[j:j + 2]
        if any(w in near for w in _UNLIMITED_BUDGET_WORDS):
            continue
        return True
    return False


def find_major_in_text(text, mask_spans=()):
    """专业识别：A（屏蔽校名区间）+ B（意图词窗口优先，位置竞争）。

    mask_spans 必须传入校名区间，否则校名里的专业词会再次胜出。
    返回 (pos, major, kw, via)；via 取 "intent" 或 "full"，便于口径文档与调试。
    """
    cands = []
    for word in MAJOR_INTENT_WORDS:
        for m in re.finditer(re.escape(word), text):
            ws = m.end()
            sub = _major_scan_first(text[ws:ws + MAJOR_INTENT_WINDOW])
            if sub:
                cands.append((ws + sub[0], sub[1], sub[2], "intent"))
    for pre, suf in MAJOR_INTENT_PAIRS:
        pattern = re.escape(pre) + r"[^，,。；;!？?\s]{0,3}" + re.escape(suf)
        for m in re.finditer(pattern, text):
            ws = m.end()
            sub = _major_scan_first(text[ws:ws + MAJOR_INTENT_WINDOW])
            if sub:
                cands.append((ws + sub[0], sub[1], sub[2], "intent"))
    for pos, major, kw in _major_scan_all(text, mask_spans):
        cands.append((pos, major, kw, "full"))
    if not cands:
        return None
    # 同位置并列时长词优先（第三十五次），与 _major_scan_first() 保持同一判据。
    cands.sort(key=lambda x: (x[0], -len(x[2])))
    return cands[0]


def _school_names(s):
    return [n for n in ([s.get("name_zh"), s.get("name_en")] + list(s.get("aliases", []))) if n]


def _find_spans(haystack, needle):
    spans = []
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            return spans
        spans.append((i, i + len(needle)))
        start = i + 1


_LATIN_WORD = re.compile(r"[A-Za-z]")


def _find_spans_bounded(text, text_lower, needle_lower):
    """按「词边界」找 needle 在文本里的区间（只用于短别名的小写副本）。

    为什么需要它：短别名小写化后是极短的拉丁串，「裸子串匹配」会大量误命中——
    "study **au**broad" 命中 AU(阿德莱德)、"m**um**bai" 命中 UM(马来亚)、
    "**lu**lu" 命中 LU(岭南)。加词边界后这些都不再成立。

    边界判定：命中片段的**左右邻接字符**都不能是拉丁字母。
    这样 "mit" 在 "mit读工科"（右邻是汉字）与 "mit 一年多少"（右邻是空格）都能命中，
    但在 "admit" / "smitten" 里不会命中。
    """
    spans = []
    n = len(needle_lower)
    if n == 0:
        return spans
    start = 0
    while True:
        i = text_lower.find(needle_lower, start)
        if i < 0:
            return spans
        left_ok = (i == 0) or not _LATIN_WORD.match(text_lower[i - 1])
        j = i + n
        right_ok = (j >= len(text_lower)) or not _LATIN_WORD.match(text_lower[j])
        if left_ok and right_ok:
            spans.append((i, j))
        start = i + 1


def _inside_any(span, containers):
    s, e = span
    for cs, ce in containers:
        if cs <= s and e <= ce:
            return True
    return False


# 需要「词边界」匹配的短别名小写副本（第二十四次改动引入）。
# 由 config.json 的 school_alias_lower_bounded 提供；缺配置时为空集合（行为退回旧版）。
SCHOOL_ALIAS_LOWER_BOUNDED = set(CONFIG.get("school_alias_lower_bounded") or [])


def _name_spans(name, text, text_lower):
    """名字在文本里的全部字符区间（**不做**屏蔽判断）。

    三分支是「同一个名字」的唯一判据：`_name_in_text()` 与 `school_name_spans()` 都必须走它，
    否则会出现「匹配得到、却屏蔽不到」（第二十四次的教训）。
    """
    if name in SCHOOL_ALIAS_LOWER_BOUNDED:
        return _find_spans_bounded(text, text_lower, name)
    if len(name) <= 4:
        return _find_spans(text, name)
    return _find_spans(text_lower, name.lower())


def _name_in_text(name, text, text_lower, mask_spans=()):
    """名字是否出现在文本里；落在 mask_spans（国家/地区别名区间）内部的命中不算。

    用途是防止「学校短别名」被「国名」吞掉：别名必须整个落在国名区间之外才算命中。
    短名字（4 字及以内，如别名 "AU"）保持大小写敏感，避免小写化后 "au" 命中 "Australia"。
    例外：`SCHOOL_ALIAS_LOWER_BOUNDED` 里的名字（短别名的小写副本，如 "mit"/"ucl"）
    按**词边界**匹配 —— 见 `_find_spans_bounded()`。
    """
    for span in _name_spans(name, text, text_lower):
        if not _inside_any(span, mask_spans):
            return True
    return False


def country_alias_spans(text):
    """文本中所有国家/地区别名的字符区间，用于屏蔽嵌在国名里的短别名。"""
    spans = []
    for _, aliases in COUNTRY_ALIASES.items():
        for alias in aliases:
            if alias:
                spans.extend(_find_spans(text, alias))
    return spans


# ---------- 国家扫描的「非国别语境」区间屏蔽（第三十五次新增） ----------
# 根因：美国含单字别名「美」、中国香港含「港」，国家扫描用的是裸子串匹配，
# 于是「美术」判成美国、「港币」判成中国香港、「港口物流」判成中国香港。
# 修法与 school_name_spans() 同一套哲学：不删别名，只把「落在这些区间里」的国别命中排除。
#
# 区间来源有两类：
#   ① 专业词（MAJOR_KEYWORDS 全量）——「美术」「美学」里的「美」不是美国。
#   ② 货币 / 金额单位 ——「港币」「美元」「澳元」里的单字不是国家。
#      （货币不在专业词表里，但误命中机理相同，故一并屏蔽。）
_CURRENCY_WORDS = (
    "港币", "美元", "美金", "澳元", "日元", "欧元", "英镑", "加元", "新元",
    "人民币", "万元", "币",
)

# 单字国别别名的「他义词」：这些词里的「美 / 港」不是国家。
# 为什么需要显式列：专业词屏蔽只挡得住词表里有的词（「美术」在表内 ✅），
# 挡不住「港口」这种不在专业词表、却同样以单字别名开头的词。
# 列的是**确实不指该国**的词；「港澳 / 港人 / 港府 / 港校 / 港大」不在列（它们确实指香港）。
_COUNTRY_ALIAS_COLLISION_WORDS = (
    # 「美」不指美国
    "美术", "美学", "审美", "美容", "美声", "美工", "美发", "美甲", "美食",
    "美剧", "美式", "美妆", "美颜", "完美", "赞美", "优美", "甜美", "美景",
    # 「港」不指中国香港
    "港口", "港湾", "港股", "港片", "港剧", "港式", "港味", "港风",
)

_MAJOR_ALL_KEYWORDS = None


def _major_all_keywords():
    """MAJOR_KEYWORDS 的全量词条去重、按长度降序（只为算屏蔽区间，不参与归属）。"""
    global _MAJOR_ALL_KEYWORDS
    if _MAJOR_ALL_KEYWORDS is None:
        ws = set()
        for keywords in MAJOR_KEYWORDS.values():
            for kw in keywords:
                if kw:
                    ws.add(kw)
        _MAJOR_ALL_KEYWORDS = sorted(ws, key=len, reverse=True)
    return _MAJOR_ALL_KEYWORDS


def non_country_spans(text):
    """文本中「不能按国别解读」的字符区间：专业词 + 货币单位。

    只用于国家全文扫描的屏蔽判断；不影响专业识别，也不影响校名匹配。
    """
    spans = []
    for kw in _major_all_keywords():
        spans.extend(_find_spans(text, kw))
    for w in _CURRENCY_WORDS:
        spans.extend(_find_spans(text, w))
    for w in _COUNTRY_ALIAS_COLLISION_WORDS:
        spans.extend(_find_spans(text, w))
    return spans


def school_name_spans(text):
    """文本中所有「校名 / 校名别名」的字符区间，用于屏蔽嵌在校名里的专业关键词。

    与 country_alias_spans() 同一套哲学：不删关键词，只把落在别人名字里的命中排除掉。
    短名（4 字及以内，含英文缩写如 "MIT"）保持大小写敏感，避免小写化后误命中普通单词；
    词边界小写副本（`SCHOOL_ALIAS_LOWER_BOUNDED`，如 "mit"）走 `_find_spans_bounded()`，
    与 `_name_in_text()` 保持同一判据，避免「匹配得到、却屏蔽不到」的不一致。
    """
    spans = []
    text_lower = text.lower()
    for s in SCHOOLS:
        for name in _school_names(s):
            if not name or len(name) < 2:
                continue
            spans.extend(_name_spans(name, text, text_lower))
    return spans


def match_school(text, mask_spans=()):
    """精确/别名匹配。返回 (唯一学校, 歧义候选列表)。

    **单值**接口：只回「唯一命中」。多所并列时不再只回「与最长名等长的那所」——
    旧写法会把短一些的整所吞掉（实测「香港大学和墨尔本大学」候选里只剩墨尔本），
    现在候选 = 命中的全部学校，由调用方决定是让用户确认还是走对比。
    需要「全部命中」的调用方请直接用 school_hits() / resolve_school_slots()。
    """
    hits = school_hits(text, mask_spans)
    if not hits:
        return None, []
    if len(hits) == 1:
        return hits[0][1], []
    return None, [s for _, s, _ in hits]


def _merge_spans(spans):
    """把同一所学校命中的多个区间并成不重叠的粗区间。"""
    out = []
    for b, e in sorted(set(spans)):
        if out and b <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((b, e))
    return out


def _drop_nested_hits(hits):
    """丢掉「命中区间完全落在另一所学校的命中区间之内」的学校。

    成因：短别名当子串命中。用户打 "UBC"（不列颠哥伦比亚大学）时，巴塞罗那大学的别名 "UB"
    落在 (0,3) 之内；打 "UCLA" 时伦敦大学学院的 "UCL" 落在 (0,4) 之内。它们**不是**用户提到的
    第二所学校，只是同一段字符被切了两刀。

    旧代码用「只留与最长名等长的候选」顺手挡住了这种噪音，但同一个过滤器也会把**真实的**
    第二所学校挡掉（实测「香港大学和墨尔本大学」候选里只剩墨尔本、香港大学被丢）。
    改用区间包含关系判定：既挡噪音，又不丢真学校——并列/比较时两校区间不会互相包含。
    """
    kept = []
    for ln, s, spans in hits:
        nested = False
        for ln2, s2, spans2 in hits:
            if s2["school_id"] == s["school_id"] or ln2 <= ln:
                continue
            if all(any(b2 <= b and e <= e2 for b2, e2 in spans2) for b, e in spans):
                nested = True
                break
        if not nested:
            kept.append((ln, s, spans))
    return kept


def school_hits(text, mask_spans=()):
    """文本里命中的**全部**学校，按「最长命中名」降序。返回 [(名长, school, 区间), ...]。

    与 match_school() 的分工：那个是**单值**接口（历史遗留，只回唯一命中或候选），
    本函数把「这段文本里到底出现了几所学校」原样交出来，由 resolve_school_slots() 决定怎么用。
    每所学校只出一条记录（best = 该校命中的最长名字长度），所以「一所学校的长短名」
    不会变成两条 —— 这是「并列对比」判据的前提。
    """
    text_lower = text.lower()
    hits = []
    for s in SCHOOLS:
        spans = []
        for name in _school_names(s):
            if not name:
                continue
            spans.extend(sp for sp in _name_spans(name, text, text_lower)
                         if not _inside_any(sp, mask_spans))
        if not spans:
            continue
        hits.append((max(e - b for b, e in spans), s, _merge_spans(spans)))
    hits.sort(key=lambda x: (-x[0], x[1]["school_id"]))
    return _drop_nested_hits(hits)


def has_school_compare_signal(text):
    return any(t in text for t in SCHOOL_COMPARE_TOKENS)


def _dedupe_ids(ids):
    out = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out


def _explicit_school_ids(payload):
    """读平台 planner 给的 school / schools 槽位，解析成 school_id 列表（保序去重）。

    值可以是中文名、英文名或别名，也允许带修饰的一句话（含校名即可）。解析不出来的值
    直接丢掉，**不据此判 not_found** —— 那是文本扫描路径的职责，槽位里塞了脏值时
    应该退回文本路径，而不是把一个用户可见的失败结论甩出去。
    """
    values = []
    for obj in _slot_candidate_containers(payload):
        for key in SCHOOL_SLOT_KEYS:
            raw = obj.get(key) if isinstance(obj, dict) else None
            if raw is None:
                continue
            if isinstance(raw, (list, tuple)):
                values.extend([v for v in raw if isinstance(v, str) and v.strip()])
            elif isinstance(raw, str) and raw.strip():
                values.append(raw)
    ids = []
    for v in values:
        v = v.strip()
        for _, s, _spans in school_hits(v, country_alias_spans(v)):
            if s["school_id"] not in ids:
                ids.append(s["school_id"])
    return ids


def resolve_school_slots(payload, current_text, joined):
    """决定「本轮到底要算哪几所学校」。返回 (school_ids, mode)。

    mode ∈ {"none", "single", "compare", "ambiguous"}。判定顺序（第三十二次定稿）：

      1. 平台显式给了 >= 2 所（如 `schools: [...]`）→ 直接多校对比：**结构是平台解好的，
         比我们扫文本更可信**，也不再受文本里存不存在连词影响。
      2. 合并候选：平台单校 > 本轮文本命中 > 历史文本命中，按序去重。
         文本侧的噪音（短别名当子串）已由 `_drop_nested_hits()` 去掉，不再需要「长度支配」过滤。
      3. 候选 0 所 → none；1 所 → single。
      4. 候选 >= 2 所：
         - **本轮文本里有并列/比较连词** → compare，一所都不丢。
         - 否则只看**本轮**文本（R-b：本轮优先于历史）：
           本轮恰好 1 所 → 就这一所（历史里的旧校名不再劫持后续每一轮）；
           本轮 >= 2 所（无连词）→ ambiguous，让用户确认；
           本轮 0 所（只有历史）→ 历史唯一一所才用，否则 ambiguous。

    为什么要区分「本轮 / 历史」：`joined` 是「本轮 + 历史」拼出来的，而平台每轮都会把
    完整用户历史注入 payload。用户早轮说过 A 校、本轮改问 B 校时，只看 joined 会被
    A 校的长名支配掉 B 校（实测：历史「墨尔本大学」+ 本轮「香港大学呢」→ 判成歧义且
    候选里只剩墨尔本，用户点名的香港大学被丢了）。
    """
    explicit = _explicit_school_ids(payload)
    if len(explicit) >= 2:
        return explicit, "compare"

    hits_cur = school_hits(current_text, country_alias_spans(current_text)) if current_text else []
    hits_all = school_hits(joined, country_alias_spans(joined))
    # 比较信号只看**本轮**：历史里的「A 和 B」不该让本轮追问单校时又变成对比。
    compare = has_school_compare_signal(current_text)

    cur_ids = _dedupe_ids([s["school_id"] for _, s, _ in hits_cur])
    all_ids = _dedupe_ids([s["school_id"] for _, s, _ in hits_all])

    if len(cur_ids) == 1:
        return cur_ids, "single"
    if len(cur_ids) >= 2:
        return (cur_ids, "compare") if compare else (cur_ids, "ambiguous")

    # 本轮没点校名：平台显式单校 > 历史
    if len(explicit) == 1:
        return explicit, "single"
    if len(all_ids) == 1:
        return all_ids, "single"
    if len(all_ids) >= 2:
        return (all_ids, "compare") if compare else (all_ids, "ambiguous")
    return [], "none"


def clean_school_token(token):
    """剥离「想读 / 去 / 申请」这类动词前缀，让半截校名也能进匹配。"""
    changed = True
    while changed:
        changed = False
        for p in SCHOOL_TOKEN_PREFIXES:
            if token.startswith(p) and len(token) - len(p) >= 4:
                token = token[len(p):]
                changed = True
                break
    return token


def detect_school_token(text):
    """抓出文本里最像校名的片段（去动词前缀、长度 >= 4）；没有则返回 None。"""
    pattern = r"([\u4e00-\u9fa5A-Za-z]{2,20}(?:大学|学院|学校))"
    for m in re.finditer(pattern, text):
        token = clean_school_token(m.group(1))
        if len(token) < 4:
            continue
        # 停用词命中时不再靠长度豁免：改用「是否存在真实校名的半截匹配」来豁免。
        # 这样「麻省理工学院」（含「理工」，但确实是校名）照常进匹配，
        # 而「美国公立大学」「想读好的大学」这类偏好短语被挡下，不再吞掉主路径。
        if any(w in token for w in SCHOOL_STOPWORDS) and not match_school_partial(token):
            continue
        return token
    return None


def match_school_partial(token, countries=None):
    """半截校名匹配：返回名称包含 token 的候选学校（可用用户提到的国家缩小范围）。

    从最长片段开始，逐字丢掉开头再试一次——「英国伦敦大学」也会在第 3 轮命中「伦敦大学」。
    """
    if not token or len(token) < 4:
        return []
    for start in range(0, len(token) - 3):
        sub_lower = token[start:].lower()
        hits = {}
        for s in SCHOOLS:
            if countries and s["country"] not in countries:
                continue
            for name in _school_names(s):
                if len(name) >= 4 and sub_lower in name.lower():
                    hits[s["school_id"]] = s
                    break
        if hits:
            return list(hits.values())
    return []


def _has_ascii_keyword(text_lower, k):
    """ASCII 关键词按词边界匹配：「McMaster」不会被 master 命中。"""
    return re.search(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", text_lower) is not None


def detect_k12_out_of_scope(text):
    """低龄留学拦截：K12 学段词 + 留学/就读意图（「高中在读」这类身份描述不拦）。"""
    for kw in K12_WORDS:
        for m in re.finditer(re.escape(kw), text):
            after = text[m.end():m.end() + 4]
            before = text[max(0, m.start() - 4):m.start()]
            if any(a in after for a in K12_INTENT_AFTER):
                return kw
            if any(b in before for b in K12_INTENT_BEFORE):
                return kw
    return None


def detect_out_of_scope(text):
    """学段护栏：命中非本科学段关键词、且全文未出现「本科」时，判定超出范围。"""
    text_lower = text.lower()
    for k in BACHELOR_KEYWORDS:
        if k.lower() in text_lower:
            return None
    for k in OUT_OF_SCOPE_KEYWORDS:
        if k.isascii():
            if _has_ascii_keyword(text_lower, k):
                return k
        elif k.lower() in text_lower:
            return k
    for p in OUT_OF_SCOPE_PHRASES:
        if p in text:
            return p
    return detect_k12_out_of_scope(text)


def detect_unsupported_region(text):
    """范围外地区（如中国澳门）拦截：命中即说明范围、不给数字。"""
    for region, aliases in UNSUPPORTED_REGIONS.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if alias and alias in text:
                return region
    return None


# ---------- 结构化槽位（优先级最高） ----------

def _norm_slot_text(value):
    """规整槽位原始值：折叠空白、去首尾。

    注意**不能把空格全删掉**——'computer science' 一旦被压成 'computerscience'，
    英文多词写法就再也匹配不上了。中文侧的「忽略空格」在匹配阶段另做处理。
    """
    if not isinstance(value, str):
        return None
    s = re.sub(r"\s+", " ", value).strip()
    return s or None


def _norm_slot_country(value):
    s = _norm_slot_text(value)
    if not s:
        return None
    for country, aliases in COUNTRY_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            if alias and alias in s:
                return country
    return s


def _norm_slot_choice(value, allowed, aliases=None):
    """把槽位值规整到库里认的写法。

    别名表里 **键 = 库内规范值、值 = 各种写法（含英文）**，所以永远返回键。
    三档匹配，逐档收紧：
      1) 整串精确命中（含大小写归一）；
      2) 英文变体按**词边界**匹配，且**长词优先**——否则 'art' 会先于
         'liberal arts' 命中、'science' 会先于 'computer science' 命中、
         'cs' 会落进 'economics'（这些是跨键子串，一旦顺序颠倒就错映射）；
      3) 中文变体按子串匹配（中文无词边界，靠长词优先已经够用）；
      4) 兜底：「不限」类字样统一归一。
    """
    s = _norm_slot_text(value)
    if not s:
        return None
    if s in allowed:
        return s
    mapping = aliases or {}
    low = s.lower()
    # 中文侧忽略空格（'商 科' 也能命中）；英文侧保留空格（'computer science' 靠空格分词）
    s_nospace = s.replace(" ", "")
    low_nospace = low.replace(" ", "")

    # 展平成 (写法, 规范值) 并按写法长度降序，长词优先
    flat = []
    seen = set()
    for canonical, forms in mapping.items():
        for f in [canonical] + list(forms):
            if not f or (f, canonical) in seen:
                continue
            seen.add((f, canonical))
            flat.append((f, canonical))
    flat.sort(key=lambda x: len(x[0]), reverse=True)

    # 1) 整串精确（大小写归一 / 空格归一）
    for f, canonical in flat:
        if f.lower() in (low, low_nospace):
            return canonical

    # 2) 英文变体：词边界匹配（避免 'arts' 里的 'art'、'physics' 里的 'cs'）
    if re.search(r"[a-z]", low):
        for f, canonical in flat:
            fl = f.lower()
            if not re.search(r"[a-z]", fl):
                continue
            if re.search(r"(?<![a-z])" + re.escape(fl) + r"(?![a-z])", low):
                return canonical

    # 3) 中文变体：子串匹配（用去空格版）
    for f, canonical in flat:
        if not re.search(r"[一-龥]", f):
            continue
        if f.replace(" ", "") in s_nospace:
            return canonical

    # 4) 兜底
    if "不限" in s:
        return "不限"
    return s


def _major_attr_table():
    """专业关键词 -> 六大类 的归属表（只收 dims.majors 里的大类）。

    与 MAJOR_KEYWORDS 同源：那个是「全文扫描用的宽表」，这里把它反过来当
    「某个专业全称属于哪个大类」的字典用。
    """
    table = {}
    majors = CONFIG.get("dims", {}).get("majors", [])
    for canonical, keywords in MAJOR_KEYWORDS.items():
        if canonical not in majors:
            continue
        for kw in keywords:
            if kw and kw not in table:
                table[kw] = canonical
    return table


_MAJOR_ATTR_FLAT = None


def _major_attr_flat():
    """(_major_attr_table 摊平成 [(写法, 大类)]，按写法长度降序) —— 长词优先。"""
    global _MAJOR_ATTR_FLAT
    if _MAJOR_ATTR_FLAT is None:
        flat = list(_major_attr_table().items())
        flat.sort(key=lambda x: len(x[0]), reverse=True)
        _MAJOR_ATTR_FLAT = flat
    return _MAJOR_ATTR_FLAT


def _major_attr_fallback(value):
    """把「未归类的专业写法」回退到六大类；认不出来返回 None。

    触发场景：平台把用户选的专业全称（`金融` / `计算机` / `临床医学`）直接放进
    major 槽位，而 `_norm_slot_choice` 的别名表只覆盖 6 个规范值 + 少量英文写法，
    其余原样透出 —— 那个裸值在 `filter_schools` 里查不到任何 `(school_id, major)`，
    一路走到 `reject / no_school_match`，等于「用户说了专业反而没结果」。

    匹配两档，长词优先（避免 `医学` 抢走 `临床医学`、`工程` 抢走 `软件工程`）：
      1) 整串精确（大小写 / 空格归一）
      2) 子串包含
    「不限」是槽位哨兵值，不参与回退。
    """
    s = _norm_slot_text(value)
    if not s or s == "不限":
        return None
    low = s.lower()
    low_nospace = low.replace(" ", "")
    flat = _major_attr_flat()

    for kw, canonical in flat:
        kwl = kw.lower()
        if kwl == low or kwl.replace(" ", "") == low_nospace:
            return canonical
    for kw, canonical in flat:
        kwl = kw.lower()
        if kwl in low or kwl.replace(" ", "") in low_nospace:
            return canonical
    return None


def _major_slot_value(raw, majors):
    """major 槽位的规整总入口：先走通用别名表，未归类时再用归属表回退。"""
    val = _norm_slot_choice(raw, majors, _slot_choice_aliases("major"))
    if val in majors or val == "不限":
        return val
    return _major_attr_fallback(val) or val


def _slot_choice_aliases(key):
    """某个维度的「规范值 -> 各种写法」别名表。

    所有表的键都是**库内规范值**（与 MAJOR_KEYWORDS / BAND_ALIASES 同向），
    因此 _norm_slot_choice() 命中后返回的一定是库内认的值。
    MAJOR_KEYWORDS 本身是「全文扫描用的宽表」，直接拿来当槽位别名会把
    'art'（落在 'arts' 里）之类过宽命中，所以 major 只用紧凑的英文补充表。
    """
    if key == "budget_range":
        return BAND_ALIASES
    if key == "major":
        keys = set(CONFIG.get("dims", {}).get("majors", [])) | {"不限"}
        return {canonical: forms
                for canonical, forms in SLOT_VALUE_ALIASES.get("major", {}).items()
                if canonical in keys}
    if key in ("school_type", "city_tier"):
        field = "school_types" if key == "school_type" else "city_tiers"
        keys = CONFIG.get("dims", {}).get(field, [])
        merged = {t: [t] for t in keys}
        for canonical, forms in SLOT_VALUE_ALIASES.get(key, {}).items():
            merged.setdefault(canonical, [canonical])
            for f in forms:
                if f not in merged[canonical]:
                    merged[canonical].append(f)
        return merged
    return {}


def _slot_candidate_containers(payload):
    """收集可能承载显式槽位的 dict 层，**顶层优先**。

    平台除了顶层名键，还会把同样的内容放在 parameters / planner_parameters / facts 里，
    因此这些层按固定顺序补充；同一维度先命中的层胜出。
    """
    out = []

    def walk(obj, depth):
        if isinstance(obj, dict):
            out.append(obj)
            if depth < 4:
                for k, v in obj.items():
                    if k in ("parameters", "planner_parameters", "facts", "slots", "form", "values"):
                        walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth)

    walk(payload, 0)
    return out


def _norm_slot_countries(values):
    """countries 槽位的规整总入口。

    - 逐个值走 `_norm_slot_country()`；认不出国家的（含「暂不确定」这类**未指定**写法）
      一律丢掉 —— 目的是让「暂不确定」等价于「未指定」，从而走既有「全部国家都算一遍」路径。
    - 若**全部**值都是未指定，返回空列表（= 未指定）；这是刻意选择：问卷多选且必填，
      用户只勾「暂不确定」时不该被当成「指定了一个叫暂不确定的国家」而筛空。
    - 未指定写法**混选**时也整体归零（例：["美国","暂不确定"]）——「暂不确定」语义是
      「都可以/还没定」，与具体国家互斥；只保美国会误砍范围。此组合理论上不该出现，
      因为问卷里它是独立选项，这里只是防御。
    """
    out = []
    saw_undetermined = False
    for v in (values or []):
        if v is None:
            continue
        s = _norm_slot_text(v)
        if not s:
            continue
        if any(t in s for t in COUNTRY_UNDETERMINED_TOKENS):
            saw_undetermined = True
            continue
        c = _norm_slot_country(s)
        if c and c in COUNTRY_ALIASES:
            out.append(c)
    if saw_undetermined:
        return []
    return list(dict.fromkeys(out))


def parse_explicit_slots(payload):
    """从结构化键里读槽位。返回 (slots片段, 命中维度集合)。

    这是「表单提交」路径的正解：平台已经把用户选的值算好了，直接采信即可，
    绝不能用全文扫描去「猜」——助手历史里的选项清单会猜错。
    """
    found = {}
    containers = _slot_candidate_containers(payload)
    majors = CONFIG.get("dims", {}).get("majors", [])

    for key, field in EXPLICIT_SLOT_KEYS.items():
        for obj in containers:
            if key not in obj:
                continue
            raw = obj[key]
            if raw is None:
                continue
            if field == "countries":
                vals = raw if isinstance(raw, list) else [raw]
                got = _norm_slot_countries(vals)
                if got:
                    found[field] = got
                    break
                # 空结果 = 未指定（含「暂不确定」）。继续找其它层，可能有更具体的值。
                continue
            if isinstance(raw, list):
                raw = raw[0] if raw else None
            if raw is None:
                continue
            if field == "major":
                val = _major_slot_value(raw, majors)
            elif field == "budget_range":
                val = _norm_slot_choice(raw, [b["code"] for b in BUDGET_BANDS],
                                        _slot_choice_aliases("budget_range"))
            else:
                val = _norm_slot_choice(raw, [], _slot_choice_aliases(field))
            if val:
                found[field] = val
                break
    return found


def extract_slots(payload):
    # 扫描文本只取「用户本轮原话」：助手历史里的选项清单会把槽位带偏（P1）
    current_text, history_text = _slot_text_split(payload)
    if not current_text:
        texts = []
        _deep_collect(payload, texts)
        current_text = " ".join(texts)
    # 若调用方显式传了 user_query（连用户原话都不在 payload 里时），再补一层
    if isinstance(payload, dict):
        extra = payload.get("user_query")
        if isinstance(extra, str) and extra.strip() and extra not in current_text:
            current_text = (current_text + " " + extra).strip()
    # 全文（本轮在前、历史在后）用于国家/专业/校名这类不受后续轮次改写的槽位；
    # 历史在前会让旧信息在位置竞争中胜出，故本轮必须排在前面。
    joined = current_text if not history_text else (current_text + " " + history_text).strip()

    # 校名/别名区间先算出来：既给专业识别做屏蔽（方案 A），也给校名匹配复用
    school_mask = school_name_spans(joined)

    slots = {
        "countries": [],
        "major": None,
        "school_type": None,
        "city_tier": None,
        "budget_range": None,
        "budget_amount_cny": None,
        "budget_cap_cny": None,
        "budget_undetermined": False,
        "budget_from_history": False,
        "budget_known": False,
        "major_source": None,
        "major_conflict": None,
        "school_query": None,
        "school_id": None,
        # 多校并列/对比：本轮要一起出数的学校（保序）。>= 2 时走多校对比形态。
        "school_ids": [],
        "school_queries": [],
        "school_ambiguous": False,
        "school_candidates": [],
        "school_not_found": False,
        "out_of_scope": None,
        "unsupported_region": None,
        "budget_amount_source": None,
    }

    # 结构化槽位优先：平台/表单已经给出的值直接采信，命中即锁，该维度不再做全文扫描
    explicit = parse_explicit_slots(payload)

    explicit_countries = [c for c in (explicit.get("countries") or []) if c]
    if explicit_countries:
        slots["countries"] = explicit_countries
    else:
        # 命中必须整体落在「非国别语境」区间之外：单字别名（美 / 港）落进专业词
        # （美术 / 美学）或货币单位（港币）时不算国别（第三十五次）。
        blocked = non_country_spans(joined)
        for country, aliases in COUNTRY_ALIASES.items():
            for alias in sorted(aliases, key=len, reverse=True):
                if not alias or alias not in joined:
                    continue
                if all(_inside_any(sp, blocked) for sp in _find_spans(joined, alias)):
                    continue
                slots["countries"].append(country)
                break
        slots["countries"] = list(dict.fromkeys(slots["countries"]))

    # ---- 专业：引擎自主识别优先于「外部传入的大类名」（2026-09-21 新增）----
    # 背景（线上会话 rev_646c933e 实测）：平台 planner 抢在引擎之前把用户口语专业
    # 归一成一个问卷大类，再以 `--major 艺术` 形式传进来。旧逻辑「结构化槽位优先」
    # 直接采信并**整段跳过全文扫描**，于是引擎自己的 781 词表根本没机会用。
    #
    # 新规则：先把用户原话扫一遍。
    #   - 扫不出 → 采信外部传入的大类名（外部信息仍有价值）。
    #   - 扫得出、且与传入值**同属一个大类** → 一致，采信外部值（无副作用）。
    #   - 扫得出、但**与传入值不同类** → 以引擎自主识别为准，并记 `major_conflict`
    #     供上层察觉（典型：传入「艺术」而用户说的是「建筑」→ 建筑归工科）。
    # 注意：大类名本身（商科/文科/…/不限）在词表里也各有一条，扫到它们与传入值必然同类，
    # 因此「用户直接说大类」的正常路径不会触发冲突。
    scanned = find_major_in_text(joined, school_mask)
    # ---- 「不限」哨兵：本轮的「不限」> 扫描出的专业 > 历史的「不限」（第三十五次）----
    # 旧写法 `("不限" in joined and "不限")` 把「不限」当成 joined（本轮+历史）的兜底，
    # 于是本轮说「改成不限」时，历史里扫出来的「商科」先胜出（scanned 非空），
    # 用户改口完全失效；而且「预算不限」也会被误当成「专业不限」。
    if _is_major_unlimited(current_text):
        scanned_major = "不限"
    elif scanned:
        scanned_major = scanned[1]
    elif _is_major_unlimited(history_text):
        # 历史里的「不限」仍要承接（否则下一轮会重复追问专业），但优先级最低。
        scanned_major = "不限"
    else:
        scanned_major = None
    explicit_major = explicit.get("major")

    if explicit_major:
        if not scanned_major:
            slots["major"] = explicit_major
            slots["major_source"] = "explicit"
        elif scanned_major == explicit_major:
            slots["major"] = explicit_major
            slots["major_source"] = "explicit"
        else:
            slots["major"] = scanned_major
            slots["major_source"] = "scanned_over_explicit"
            slots["major_conflict"] = {"explicit": explicit_major, "scanned": scanned_major}
    elif scanned_major:
        slots["major"] = scanned_major
        slots["major_source"] = "scanned"

    if explicit.get("school_type"):
        slots["school_type"] = explicit["school_type"]
    else:
        for t in ("公立", "私立", "无偏好"):
            if t in joined:
                slots["school_type"] = t
                break

    if explicit.get("city_tier"):
        slots["city_tier"] = explicit["city_tier"]
    else:
        for t in ("一线", "二三线", "无偏好"):
            if t in joined:
                slots["city_tier"] = t
                break

    if explicit.get("budget_range"):
        slots["budget_range"] = explicit["budget_range"]
    else:
        for band in BUDGET_BANDS:
            code = band["code"]
            for a in BAND_ALIASES.get(code, [code]):
                if a in joined:
                    slots["budget_range"] = code
                    break
            if slots["budget_range"]:
                break
    # 自由文本兜底是否把预算判成「不设限」（L2）。它必须同时压掉「历史金额兜底」：
    # 「暂不确定」会让 has_current_band 为假，于是早轮说过的金额会被翻出来当上限 ——
    # 用户这轮说「不限」，却按历史金额筛掉一批国家，自相矛盾。
    free_text_undetermined = False

    if slots["budget_range"]:
        band = next((b for b in BUDGET_BANDS if b["code"] == slots["budget_range"]), None)
        if band and band.get("max") is None and band.get("min") is None:
            slots["budget_undetermined"] = True
        elif band:
            slots["budget_cap_cny"] = BAND_CAP.get(slots["budget_range"])
        else:
            # 槽位值不在 6 个档位里（自由文本「8万以下」「不限」）：三级兜底，
            # 见 parse_budget_free_text()。判不出来的（L3）**不改任何状态**，
            # 与改动前一样原样透出 —— 行为零变化。
            kind, cny = parse_budget_free_text(slots["budget_range"])
            if kind == "cap":
                slots["budget_cap_cny"] = cny
            elif kind == "undetermined":
                slots["budget_undetermined"] = True
                free_text_undetermined = True
                slots["budget_range"] = "暂不确定"
            elif kind == "no_ceiling":
                slots["budget_range"] = "50万以上"

    # ---- 预算：三条通道的优先级（本轮 > 历史）----
    # ① 本轮显式金额槽位（平台表单带的具体金额）
    # ② 本轮自然语言里的金额（「一年25万」）
    # ③ 档位（20万-30万 …）—— 来源可以是显式槽位或本轮/历史文本
    # 规则：**本轮给出的档位，不得被历史扫出来的金额压掉**。
    # 否则用户早轮说过的「一年25万」会永久霸占预算上限，后续改口全部失效。
    current_amount_cny, current_amount_src = find_budget_amount_slot(payload)
    if current_amount_cny is None:
        current_amount_cny, current_amount_src = parse_budget_amount(current_text)
    history_amount_cny = None
    if current_amount_cny is None and history_text:
        history_amount_cny, _ = parse_budget_amount(history_text)

    has_current_band = bool(slots["budget_range"]) and not slots["budget_undetermined"]

    if current_amount_cny is not None:
        # 本轮金额最优先：直接覆盖档位上限
        slots["budget_amount_cny"] = current_amount_cny
        slots["budget_amount_source"] = current_amount_src
        slots["budget_cap_cny"] = current_amount_cny
        slots["budget_from_history"] = False
    elif has_current_band:
        # 本轮（或显式）给了档位：档位胜出，历史金额不参与
        slots["budget_from_history"] = False
    elif history_amount_cny is not None and not free_text_undetermined:
        # 只有历史金额、且本轮没有任何预算信息：作为兜底承接
        slots["budget_amount_cny"] = history_amount_cny
        slots["budget_amount_source"] = "history"
        slots["budget_cap_cny"] = history_amount_cny
        slots["budget_from_history"] = True
    else:
        slots["budget_from_history"] = False

    # 预算是否「已经给过」——只要任一通道拿到了预算信息，就不得再向用户追问预算。
    # 注意「50万以上」这类开区间档位 cap 为 None，但它同样代表用户已经表过态，
    # 因此必须靠 budget_range 在场来判定，不能只看 cap。
    slots["budget_known"] = bool(
        slots["budget_amount_cny"] is not None
        or slots["budget_cap_cny"] is not None
        or slots["budget_range"]
        or slots["budget_undetermined"]
        or slots["budget_from_history"]
    )

    # 校名匹配仍屏蔽「国家/地区别名区间」（防止 "AU" 这类短别名被国名吞掉）；
    # school_mask 在这里不能当 mask 用——那会让整段校名自己被屏蔽掉。
    # 第三十二次改动：改走 resolve_school_slots()。它同时读平台给的 school / schools 槽位，
    # 并区分「本轮 / 历史」与「并列 / 非并列」——旧写法只扫 joined（本轮+历史）且只认
    # 唯一命中，用户本轮改问另一所学校时会被历史校名劫持、报歧义。
    school_ids, school_mode = resolve_school_slots(payload, current_text, joined)
    if school_mode == "single":
        school = SCHOOL_BY_ID.get(school_ids[0])
        if school:
            slots["school_id"] = school["school_id"]
            slots["school_query"] = school["name_zh"]
    elif school_mode == "compare":
        slots["school_ids"] = school_ids
        slots["school_queries"] = [SCHOOL_BY_ID[i]["name_zh"] for i in school_ids
                                   if i in SCHOOL_BY_ID]
    elif school_mode == "ambiguous":
        slots["school_ambiguous"] = True
        slots["school_candidates"] = [SCHOOL_BY_ID[i]["name_zh"] for i in school_ids
                                      if i in SCHOOL_BY_ID][:6]
    else:
        token = detect_school_token(joined)
        if token:
            partial = match_school_partial(token, slots["countries"] or None)
            if len(partial) == 1:
                slots["school_query"] = partial[0]["name_zh"]
                slots["school_id"] = partial[0]["school_id"]
            elif len(partial) > 1:
                slots["school_ambiguous"] = True
                slots["school_candidates"] = [s["name_zh"] for s in partial][:6]
            else:
                slots["school_query"] = token
                slots["school_not_found"] = True

    slots["out_of_scope"] = detect_out_of_scope(joined)
    slots["unsupported_region"] = detect_unsupported_region(joined)

    return slots


# ---------- 数据查询 ----------

def get_city_living(country, city, city_tier):
    c = CITY_LIVING_BY_KEY.get((country, city))
    if c:
        return c
    candidates = CITY_LIVING_BY_COUNTRY_TIER.get((country, city_tier))
    if candidates:
        return {
            "cost_min": min(x["cost_min"] for x in candidates),
            "cost_max": max(x["cost_max"] for x in candidates),
            "currency": candidates[0]["currency"],
        }
    candidates = CITY_LIVING_BY_COUNTRY.get(country)
    if candidates:
        return {
            "cost_min": min(x["cost_min"] for x in candidates),
            "cost_max": max(x["cost_max"] for x in candidates),
            "currency": candidates[0]["currency"],
        }
    return None


def to_cny(amount, currency):
    if amount is None:
        return None
    fx = FX_BY_CURRENCY.get(currency)
    if not fx:
        return None
    return amount * fx["cny_per_unit"]


def get_visa_medical_detail(country):
    """取某国的签证费与医疗保险费（分开取数，不做合计）。

    金额同时给人民币值与原币值：签证费是办理一次性费用，医疗保险按年、取区间。
    """
    v = VISA_BY_COUNTRY.get(country)
    if not v:
        return None
    fx = FX_BY_CURRENCY.get(v["currency"])
    rate = fx["cny_per_unit"] if fx else 0.0
    return {
        "currency": v["currency"],
        "visa": v["visa_fee"],
        "medical_min": v["medical_insurance_min"],
        "medical_max": v["medical_insurance_max"],
        "visa_cny": v["visa_fee"] * rate,
        "medical_min_cny": v["medical_insurance_min"] * rate,
        "medical_max_cny": v["medical_insurance_max"] * rate,
    }


def pick_display_currency(school_list):
    counts = defaultdict(int)
    for s in school_list:
        for t in TUITION_BY_SCHOOL.get(s["school_id"], []):
            counts[t["currency"]] += 1
    if len(counts) == 1:
        return next(iter(counts))
    if counts:
        return None
    for s in school_list:
        a = ACCOM_BY_SCHOOL.get(s["school_id"])
        if a:
            return a["currency"]
    return None


# ---------- 筛选 ----------

def filter_schools(slots):
    result = []
    for s in SCHOOLS:
        if slots["countries"] and s["country"] not in slots["countries"]:
            continue
        if slots["school_type"] and slots["school_type"] != "无偏好":
            if s["school_type"] != slots["school_type"]:
                continue
        if slots["city_tier"] and slots["city_tier"] != "无偏好":
            if s["city_tier"] != slots["city_tier"]:
                continue
        if slots["major"] and slots["major"] != "不限":
            if (s["school_id"], slots["major"]) not in TUITION_BY_KEY:
                continue
        else:
            if s["school_id"] not in TUITION_BY_SCHOOL:
                continue
        result.append(s)
    return result


# ---------- 费用计算 ----------

def original_range(rows, min_key, max_key):
    """一组数据行的原币区间，供“费用明细”的原币列直接展示（不做汇率反推）。

    同一分项混用多种币种时无法用单一原币表达 → 返回 None，模型侧写「—」。
    """
    if not rows:
        return None
    currencies = {r["currency"] for r in rows}
    if len(currencies) != 1:
        return None
    return {
        "min": min(r[min_key] for r in rows),
        "max": max(r[max_key] for r in rows),
        "currency": next(iter(currencies)),
    }


def calc_country_cost(country, school_list, major):
    if not school_list:
        return None

    tuition_rows = []
    for s in school_list:
        if major and major != "不限":
            t = TUITION_BY_KEY.get((s["school_id"], major))
            tlist = [t] if t else []
        else:
            tlist = TUITION_BY_SCHOOL.get(s["school_id"], [])
        for t in tlist:
            if t["currency"] in FX_BY_CURRENCY:
                tuition_rows.append(t)

    no_tuition = not tuition_rows
    tuition_min_cny = min(to_cny(r["tuition_min"], r["currency"]) for r in tuition_rows) if tuition_rows else 0.0
    tuition_max_cny = max(to_cny(r["tuition_max"], r["currency"]) for r in tuition_rows) if tuition_rows else 0.0

    accom_rows = [ACCOM_BY_SCHOOL[s["school_id"]] for s in school_list
                  if s["school_id"] in ACCOM_BY_SCHOOL
                  and ACCOM_BY_SCHOOL[s["school_id"]]["currency"] in FX_BY_CURRENCY]
    accom_min_cny = min(to_cny(r["accommodation_fee_min"], r["currency"]) for r in accom_rows) if accom_rows else 0.0
    accom_max_cny = max(to_cny(r["accommodation_fee_max"], r["currency"]) for r in accom_rows) if accom_rows else 0.0

    living_rows = []
    seen = set()
    for s in school_list:
        key = (s["country"], s["city"])
        if key in seen:
            continue
        seen.add(key)
        c = get_city_living(s["country"], s["city"], s["city_tier"])
        if c and c["currency"] in FX_BY_CURRENCY:
            living_rows.append(c)
    living_min_cny = min(to_cny(r["cost_min"], r["currency"]) for r in living_rows) if living_rows else 0.0
    living_max_cny = max(to_cny(r["cost_max"], r["currency"]) for r in living_rows) if living_rows else 0.0

    vm = get_visa_medical_detail(country)
    if vm is None:
        vm = {"currency": None, "visa": None, "medical_min": None, "medical_max": None,
              "visa_cny": 0.0, "medical_min_cny": 0.0, "medical_max_cny": 0.0}
    # 合计口径与拆前的“签证及医疗费”完全一致：签证费一次性额 + 医疗保险费区间
    visa_medical_min_cny = vm["visa_cny"] + vm["medical_min_cny"]
    visa_medical_max_cny = vm["visa_cny"] + vm["medical_max_cny"]

    total_min_cny = tuition_min_cny + accom_min_cny + living_min_cny + visa_medical_min_cny
    total_max_cny = tuition_max_cny + accom_max_cny + living_max_cny + visa_medical_max_cny

    display_currency = pick_display_currency(school_list)
    total_min_original = total_max_original = None
    if display_currency:
        fx = FX_BY_CURRENCY.get(display_currency)
        if fx:
            total_min_original = total_min_cny / fx["cny_per_unit"]
            total_max_original = total_max_cny / fx["cny_per_unit"]

    return {
        "country": country,
        "currency": display_currency,
        "total_min_cny": round(total_min_cny),
        "total_max_cny": round(total_max_cny),
        "total_min_original": round(total_min_original) if total_min_original is not None else None,
        "total_max_original": round(total_max_original) if total_max_original is not None else None,
        "details_cny": {
            "tuition_min": round(tuition_min_cny),
            "tuition_max": round(tuition_max_cny),
            "accommodation_min": round(accom_min_cny),
            "accommodation_max": round(accom_max_cny),
            "living_min": round(living_min_cny),
            "living_max": round(living_max_cny),
            "medical_min": round(vm["medical_min_cny"]),
            "medical_max": round(vm["medical_max_cny"]),
            "visa_min": round(vm["visa_cny"]),
            "visa_max": round(vm["visa_cny"]),
        },
        "details_original": {
            "tuition": original_range(tuition_rows, "tuition_min", "tuition_max"),
            "accommodation": original_range(accom_rows, "accommodation_fee_min", "accommodation_fee_max"),
            "living": original_range(living_rows, "cost_min", "cost_max"),
            "medical": None if vm["medical_min"] is None else {
                "min": vm["medical_min"], "max": vm["medical_max"], "currency": vm["currency"]},
            "visa": None if vm["visa"] is None else {
                "min": vm["visa"], "max": vm["visa"], "currency": vm["currency"]},
        },
        "no_tuition": no_tuition,
    }


# ---------- 预算匹配 ----------

# 替代地区条目只保留这三个键（第三十二次 ④-b），理由见 _slim_alternative()。
ALT_KEYS = ("country", "total_min_cny", "total_max_cny")

# 替代地区最多列几个（第三十三次）。与 DROPPED_DISPLAY_LIMIT 取同一上限 5——
# 线上一次返回 14 个替代地区（rev_ab70a2e…：马来西亚 4.9 万 → 新加坡 29.7 万），
# 逐条罗列会把风险说明与导流话术顶出视野。
ALTERNATIVE_DISPLAY_LIMIT = 5


def _slim_alternative(result):
    """替代地区条目 -> 只留「国家 + 人民币总费用区间」。

    第三十二次 ④-b：替代地区**只输出折算人民币区间、不输出原币**。把原币与明细从返回值里
    去掉，模型就没有可误用的字段（线上实测它写过「31,428 - 59,738 林吉特」，还漏列条目、
    自造标题）。另外 `details_cny` / `details_original` 占失败返回体的 58.5%（4375/7476 字符），
    去掉后返回体明显变小——而报告模板 §九 本来就规定替代地区「只展示总区间、不展开明细」，
    这两个键**从来不进输出**。
    """
    return {k: result[k] for k in ALT_KEYS}


def build_alternative_countries(slots, budget_cap_cny):
    """预算不足时给出「符合预算的替代国家/地区」。

    忽略用户原本选定的国家/学校，保留专业、学校类型、城市层级条件，按人民币总费用下限**降序**
    取前 ALTERNATIVE_DISPLAY_LIMIT 个（第三十三次）——先给最接近预算上限的地区，用户一眼就
    能看出「再加一点就够得着」；升序会让最便宜的地区占满名额（线上实测列到了 14 个）。
    返回体经 `_slim_alternative()` 收敛（只有国家 + 人民币区间，无原币、无明细）。
    """
    if budget_cap_cny is None:
        return []
    alt_slots = dict(slots)
    alt_slots["countries"] = []
    alt_slots["school_id"] = None
    alt_slots["school_query"] = None
    alt_slots["school_not_found"] = False
    alt_slots["school_ambiguous"] = False
    school_list = filter_schools(alt_slots)
    if not school_list:
        return []
    grouped = defaultdict(list)
    for s in school_list:
        grouped[s["country"]].append(s)
    results = []
    for country, s_list in grouped.items():
        r = calc_country_cost(country, s_list, alt_slots["major"])
        if r and r["total_min_cny"] <= budget_cap_cny:
            results.append(_slim_alternative(r))
    # 第三十三次：按人民币下限降序（最接近预算上限的在前）取前 ALTERNATIVE_DISPLAY_LIMIT 个。
    # 与 _dropped_of() 同向——超预算侧也是「下限降序」，两侧口径一致。
    results.sort(key=lambda x: (x["total_min_cny"], x["total_max_cny"]), reverse=True)
    return results[:ALTERNATIVE_DISPLAY_LIMIT]


def budget_shortfall_message(slots, budget_cap_cny, dropped_results=None):
    """预算不足时的统一分档（阈值 10 万人民币）。

    返回 (budget_match_message, 替代国家/地区)：
      - 上限 < 10 万：低档话术，不给替代区域
      - 上限 >= 10 万：高端前缀（以冒号结尾）+ 符合预算的替代区域

    `dropped_results`（可选）= 因超预算被丢弃的国家/地区结果。传了它、且它们**全部**超预算时，
    前缀前会补一句「你提到的地区（…）按当前条件估算的总费用超出了你的年度预算，本次未展开。
    其总费用区间为：…。」——第三十二次 ④-a(α)：避免用户只看到替代区域、看不到自己所问地区的费用。
    点名学校单校路径**不传**（该校数字已在正文里，复述是噪音）。
    """
    if budget_cap_cny is not None and budget_cap_cny >= 100000:
        alternatives = build_alternative_countries(slots, budget_cap_cny)
        if alternatives:
            # all_dropped_msg 自带「还可以关注这些区域：」这句结尾，命中时**不要**再拼前缀，
            # 否则同一句会出现两遍。
            lead = all_dropped_msg(dropped_results or [], budget_cap_cny)
            if lead:
                return lead, alternatives
            return CONFIG["budget_match"]["insufficient_high_prefix"], alternatives
    return CONFIG["budget_match"]["insufficient_low"], []


# 被剔除地区最多列几个。报告模板的展示惯例是前 5 个，这里沿用同一上限——
# 实测「按预算推荐」路径一次能剔除 8 个，不限个数话术会变成大段罗列。
# 「前 5 个」= 费用下限最高的 5 个（降序）；被截掉的是最接近预算、最可能靠调整预算够到的那几个。
DROPPED_DISPLAY_LIMIT = 5


def _wan_cny(cny):
    """人民币元 -> 「X.X 万」的数值（与预算同币种，用户能直接看出超了多少）。"""
    return round(cny / 10000.0, 1)


def _dropped_of(country_results, budget_cap_cny):
    """按人民币总费用下限**降序**取出「超预算被剔除」的那些（超出最多的排最前）。"""
    return sorted([c for c in country_results if c["total_min_cny"] > budget_cap_cny],
                  key=lambda x: (x["total_min_cny"], x["total_max_cny"]), reverse=True)


def _dropped_names_ranges(dropped, label=None):
    """被剔除项 -> (名称串, 区间串)，最多前 DROPPED_DISPLAY_LIMIT 个、超出加「等」。

    两串都由本函数排定，不随数据文件排列漂移；`label` 可换名称来源（国家 / 学校）。
    """
    shown = dropped[:DROPPED_DISPLAY_LIMIT]
    more = "等" if len(dropped) > len(shown) else ""
    label = label or (lambda c: c["country"])
    names = "、".join(label(c) for c in shown) + more
    parts = []
    for c in shown:
        lo, hi = _wan_cny(c["total_min_cny"]), _wan_cny(c["total_max_cny"])
        rng = "%s 万人民币" % lo if lo == hi else "%s - %s 万人民币" % (lo, hi)
        parts.append("%s 约 %s" % (label(c), rng))
    return names, "；".join(parts)


def dropped_countries_msg(country_results, budget_cap_cny):
    """部分国家/地区因总费用下限超预算被剔除时：固定话术 + 被剔除地区的总费用区间。

    仍然只走 `budget_match_message` 一个字段（不新增字段，Q2=B），话术与区间同句返回。
    被剔除地区按费用下限**降序**取前 DROPPED_DISPLAY_LIMIT 个（超出预算最多的排在最前）；
    被截掉的是最接近预算的那几个，末尾加「等」。
    只有「有留存、也有被剔除」时才说话——**全部**被剔除时走 all_dropped_msg()。
    """
    if budget_cap_cny is None or not country_results:
        return None
    dropped = _dropped_of(country_results, budget_cap_cny)
    if not dropped or len(dropped) == len(country_results):
        return None
    names, ranges = _dropped_names_ranges(dropped)
    return (CONFIG["budget_match"]["partial_dropped_msg"]
            .replace("{countries}", names)
            .replace("{ranges}", ranges))


def all_dropped_msg(country_results, budget_cap_cny):
    """**全部**国家/地区都被剔除时：话术里补出它们各自的总费用区间（第三十二次 ④-a α）。

    背景（第三十一次排查，线上会话实证）：`apply_budget_filter()` 在「一个都不匹配」时
    `return [], msg, alternatives`——被点名国家/地区的费用被整段丢掉，用户只看到
    「根据你的年度预算，还可以关注这些区域：」，**从头到尾不知道所问的国家要多少钱**。
    「部分剔除」那条路径（第二十七/二十八次）早就把区间塞进话术了，同一诉求漏了这条。

    做法（α）：与 `partial_dropped_msg` 同构——话术与区间同在一句、仍只走
    `budget_match_message` 一个字段，**不新增字段**（Q2=B）。超过 DROPPED_DISPLAY_LIMIT 个
    时同样按下限降序取前 5 个并加「等」。

    注意：**点名学校单校路径不调用本函数**——该校的费用本来就在正文里，复述一遍是噪音。
    """
    if budget_cap_cny is None or not country_results:
        return None
    if len(_dropped_of(country_results, budget_cap_cny)) != len(country_results):
        return None  # 还有留存：那是 partial_dropped_msg 的活
    tpl = CONFIG["budget_match"].get("all_dropped_msg")
    if not tpl:
        return None
    names, ranges = _dropped_names_ranges(_dropped_of(country_results, budget_cap_cny))
    return tpl.replace("{countries}", names).replace("{ranges}", ranges)


def apply_budget_filter(country_results, budget_cap_cny, slots=None):
    """按预算上限筛选。返回 (展示结果, budget_match_message, 替代国家/地区)。"""
    if budget_cap_cny is None:
        return country_results, None, []
    matched = [c for c in country_results if c["total_min_cny"] <= budget_cap_cny]
    matched.sort(key=lambda x: (x["total_min_cny"], x["total_max_cny"]))
    if matched:
        return matched, dropped_countries_msg(country_results, budget_cap_cny), []
    if slots is None:
        return [], CONFIG["budget_match"]["insufficient_low"], []
    # 传 country_results 进去：全部不匹配时，它们的费用区间要出现在话术里（④-a α），
    # 否则用户只看到替代区域、看不到自己所问地区的费用。
    budget_msg, alternatives = budget_shortfall_message(slots, budget_cap_cny, country_results)
    return [], budget_msg, alternatives


# ---------- 指定学校不存在 ----------

def handle_school_not_found(slots):
    if not slots["countries"]:
        return {
            "ok": True,
            "action": "school_not_found",
            "slots": slots,
            "result": {
                "message": CONFIG["similar_school_fallback"]["priority3_msg"],
                "school_query": slots["school_query"],
                "countries": [],
            },
        }

    fallback_slots = dict(slots)
    fallback_slots["school_query"] = None
    fallback_slots["school_id"] = None
    fallback_slots["school_not_found"] = False

    # 同类替代固定两级（前提都是「国家相同 + 学校类型相同」，不引入城市维度）：
    #   第 1 级：专业相同（该国同类型院校里有该专业的学费数据）
    #   第 2 级：不限专业（第 1 级无结果时放宽，价格取这些院校全部专业的区间）
    fallback_level = 1
    school_list = filter_schools(fallback_slots)
    if not school_list and slots["major"] and slots["major"] != "不限":
        level2_slots = dict(fallback_slots)
        level2_slots["major"] = None
        school_list = filter_schools(level2_slots)
        fallback_level = 2
    if not school_list:
        return {
            "ok": True,
            "action": "school_not_found",
            "slots": slots,
            "result": {
                "message": CONFIG["similar_school_fallback"]["priority3_msg"],
                "school_query": slots["school_query"],
                "countries": [],
            },
        }

    grouped = defaultdict(list)
    for s in school_list:
        grouped[s["country"]].append(s)

    calc_major = None if fallback_level == 2 else slots["major"]
    country_results = []
    for country, s_list in grouped.items():
        r = calc_country_cost(country, s_list, calc_major)
        if r:
            country_results.append(r)
    country_results.sort(key=lambda x: x["total_min_cny"])

    budget_msg = None
    alternatives = []
    if slots["budget_cap_cny"] is not None:
        country_results, budget_msg, alternatives = apply_budget_filter(
            country_results, slots["budget_cap_cny"], slots)

    return {
        "ok": True,
        "action": "school_not_found",
        "slots": slots,
        "result": {
            # 两把文案键的文案逐字相同，只用 fallback_level 区分命中的级次
            "message": CONFIG["similar_school_fallback"][
                "priority1_hit_msg" if fallback_level == 1 else "priority2_hit_msg"],
            "fallback_level": fallback_level,
            "school_query": slots["school_query"],
            "countries": country_results,
            "alternative_countries": alternatives,
            "budget_match_message": budget_msg,
            "risk_disclaimer": build_disclaimer(),
        },
    }


# ---------- action 决策 ----------

def missing_questions(slots):
    """还缺哪些题 —— 按问卷顺序返回 question_id 列表，只列确实未获得的项。

    让 show_form 有据可依：调用方不必自己猜该挂哪几题，照这个清单挂即可。
    """
    have = {
        "countries": bool(slots["countries"]),
        "major": bool(slots["major"]),
        "school_type": bool(slots["school_type"]),
        "city_tier": bool(slots["city_tier"]),
        "budget_range": bool(slots["budget_known"]),
    }
    return [q for q in QUESTION_IDS if not have.get(q, False)]


def decide_action(slots):
    if slots["school_ambiguous"]:
        return "school_ambiguous"
    if slots["school_not_found"]:
        if not slots["major"]:
            return "ask_slot"
        return "school_not_found"
    if len(slots.get("school_ids") or []) >= 2:
        # 多校并列/对比：与点名单校同一档——缺专业只问专业，其余直接出数。
        # 排在 ambiguous / not_found 之后：那两种状态下 school_ids 是空的，互不影响。
        return "ask_slot" if not slots["major"] else "estimate"
    if slots["school_id"]:
        if not slots["major"]:
            return "ask_slot"
        return "estimate"
    if slots["budget_cap_cny"] and not slots["countries"]:
        return "recommend_by_budget"
    if slots["budget_undetermined"] and not slots["countries"]:
        # 预算「暂不确定」且未指定国家：输出全部支持国家的费用区间，不展开明细
        return "estimate" if slots["major"] else "ask_slot"
    if not slots["countries"] and not slots["school_id"] and not slots["budget_cap_cny"]:
        # 预算已给（含「50万以上」这类无上限档位、或历史里给过的金额）时，
        # 不能再回 show_form 去问预算——那正是「已给的槽位又问一遍」。
        # 此时若专业也有，就直接走全部国家的估算；否则问专业。
        if slots["budget_known"]:
            return "estimate" if slots["major"] else "ask_slot"
        return "show_form"
    if slots["countries"] and not slots["major"]:
        return "ask_slot"
    return "estimate"


# ---------- 结果组织 ----------

def build_disclaimer():
    fx_date = FX[0]["quote_date"] if FX else ""
    return CONFIG["risk_disclaimer"].replace("{date}", fx_date)


def build_sample_note(matched_count):
    """样本过少时的局限提示。

    库内多数国家/地区只收录了个位数院校，个别「国家×专业」组合更是只剩 1 所
    （实测 96 组里 12 组只有 1 所，如意大利·文科仅博洛尼亚大学）。
    这种情况下的区间来自单点样本，容易被误读成「该国该专业的整体水平」，
    因此单独给一句提示；样本正常时返回 None，不给噪音。
    """
    cfg = CONFIG.get("sample_note") or {}
    threshold = cfg.get("threshold", 1)
    if matched_count is None or matched_count < 1:
        return None
    if matched_count <= threshold:
        return cfg.get("single_school")
    return None


def _estimate_multi_school(slots, school_ids):
    """多校并列/对比：逐校一条总费用区间。

    与「点名单校」的两点差异（第三十二次定稿）：
      - **不展开明细**：两校也好三校也好，只逐校给总区间。用户追问其中一所时，
        把该校作为唯一校名再问一次就会走单校路径、自动展开明细。
      - **不做预算剔除**：用户点名了这几所，就把这几所都给出来。按预算把用户点名的学校
        藏起来与「点名学校 = 直接出数」直接冲突——那正是第三十次线上事故的回退形态
        （该校数字被丢弃、模型手里没有可播的内容）。
    """
    results = []
    for sid in school_ids:
        school = SCHOOL_BY_ID.get(sid)
        if not school:
            continue
        r = calc_country_cost(school["country"], [school], slots["major"])
        if not r:
            continue
        r["school_name"] = school["name_zh"]
        r["school_type"] = school["school_type"]
        r["city"] = school["city"]
        results.append(r)
    if not results:
        return handle_school_not_found(slots)
    results.sort(key=lambda x: (x["total_min_cny"], x["total_max_cny"]))
    return {
        "ok": True,
        "action": "estimate",
        "slots": slots,
        "result": {
            "countries": results,
            "alternative_countries": [],
            "budget_match_message": None,
            "sample_note": None,
            "risk_disclaimer": build_disclaimer(),
        },
    }


def handle_estimate(slots):
    if len(slots.get("school_ids") or []) >= 2:
        return _estimate_multi_school(slots, slots["school_ids"])
    if slots["school_id"]:
        school = SCHOOL_BY_ID.get(slots["school_id"])
        if not school:
            return handle_school_not_found(slots)
        result = calc_country_cost(school["country"], [school], slots["major"])
        result["school_name"] = school["name_zh"]
        result["school_type"] = school["school_type"]
        result["city"] = school["city"]

        budget_msg = None
        alternatives = []
        # no_tuition（点名学校在该专业方向上表内无记录）时学费按 0 计，总额被明显低估，
        # 预算比较不成立 → 整段预算匹配不输出（话术与替代区域都不给），
        # 避免与「库内暂无该方向学费数据」提示自相矛盾。
        if slots["budget_cap_cny"] is not None and not result["no_tuition"]:
            if result["total_min_cny"] > slots["budget_cap_cny"]:
                # 点名学校超预算：统一按 10 万分档，>=10 万时给出符合预算的替代国家/地区
                budget_msg, alternatives = budget_shortfall_message(slots, slots["budget_cap_cny"])
            else:
                budget_msg = CONFIG["budget_match"]["matched"]

        return {
            "ok": True,
            "action": "estimate",
            "slots": slots,
            "result": {
                "countries": [result],
                "alternative_countries": alternatives,
                "budget_match_message": budget_msg,
                "sample_note": None,
                "risk_disclaimer": build_disclaimer(),
            },
        }

    school_list = filter_schools(slots)
    if not school_list:
        # 表单路径筛完一所不剩：用户根本没提学校，不能复用「暂未收录该校/专业明细」那句
        return {
            "ok": True,
            "action": "reject",
            "reason": "no_school_match",
            "slots": slots,
            "result": {
                "message": CONFIG["fallback"]["no_school_match_msg"],
                "risk_disclaimer": build_disclaimer(),
            },
        }

    grouped = defaultdict(list)
    for s in school_list:
        grouped[s["country"]].append(s)

    country_results = []
    for country, s_list in grouped.items():
        r = calc_country_cost(country, s_list, slots["major"])
        if r:
            # 样本校数量随国附在结果里，供模型按国播报局限提示（不进明细、不参与计算）
            r["matched_schools"] = len(s_list)
            r["sample_note"] = build_sample_note(len(s_list))
            country_results.append(r)
    country_results.sort(key=lambda x: x["total_min_cny"])

    budget_msg = None
    alternatives = []
    if slots["budget_cap_cny"] is not None:
        country_results, budget_msg, alternatives = apply_budget_filter(
            country_results, slots["budget_cap_cny"], slots)

    return {
        "ok": True,
        "action": "estimate",
        "slots": slots,
        "result": {
            "countries": country_results,
            "alternative_countries": alternatives,
            "budget_match_message": budget_msg,
            "sample_note": next((c["sample_note"] for c in country_results if c.get("sample_note")), None),
            "risk_disclaimer": build_disclaimer(),
        },
    }


def handle_recommend_by_budget(slots):
    # 与 estimate 路径口径一致：先把候选校按专业（及学校类型/城市层级）筛一遍，
    # 再按国家分组计费。此处必须走 filter_schools()，否则住宿费/生活费会
    # 落到「有任意专业学费、但不开设该专业」的学校上（如米兰新美术学院只有艺术，
    # 其高价住宿会被算进意大利文科上限，实测把意大利文科上限从 13.5 万抬到 19.0 万）。
    budget_slots = dict(slots)
    budget_slots["countries"] = []
    scoped = filter_schools(budget_slots)

    all_country_groups = defaultdict(list)
    for s in scoped:
        all_country_groups[s["country"]].append(s)

    country_results = []
    for country, s_list in all_country_groups.items():
        r = calc_country_cost(country, s_list, slots["major"])
        if r:
            r["matched_schools"] = len(s_list)
            r["sample_note"] = build_sample_note(len(s_list))
            country_results.append(r)

    country_results, budget_msg, _ = apply_budget_filter(
        country_results, slots["budget_cap_cny"], slots)

    if not country_results:
        return {
            "ok": True,
            "action": "recommend_by_budget",
            "slots": slots,
            "result": {
                "countries": [],
                "budget_match_message": CONFIG["budget_match"]["insufficient_low"],
                "sample_note": None,
                "risk_disclaimer": build_disclaimer(),
            },
        }

    return {
        "ok": True,
        "action": "recommend_by_budget",
        "slots": slots,
        "result": {
            "countries": country_results,
            "budget_match_message": budget_msg,
            "sample_note": next((c["sample_note"] for c in country_results if c.get("sample_note")), None),
            "risk_disclaimer": build_disclaimer(),
        },
    }


def main(payload):
    slots = extract_slots(payload)

    if slots["out_of_scope"]:
        return {
            "ok": True,
            "action": "reject",
            "reason": "out_of_scope",
            "slots": slots,
            "result": {
                "message": CONFIG["fallback"]["out_of_scope_msg"],
                "countries": [],
            },
        }

    if slots["unsupported_region"]:
        return {
            "ok": True,
            "action": "reject",
            "reason": "unsupported_region",
            "slots": slots,
            "result": {
                "message": CONFIG["fallback"]["unsupported_region_msg"],
                "countries": [],
            },
        }

    action = decide_action(slots)

    if action == "show_form":
        miss = missing_questions(slots)
        return {"ok": True, "action": "show_form", "slots": slots,
                # 明确告诉调用方「还缺哪些题」，避免自己猜（历史问题：第 1 轮只挂 major、
                # 后续轮一次挂 4 题，都出在这里）。顺序即问卷顺序。
                "missing_question_ids": miss,
                "missing_labels": [QUESTION_LABELS.get(q, q) for q in miss],
                "askable_slots": miss}

    if action == "ask_slot":
        return {"ok": True, "action": "ask_slot",
                "missing_slot": "major", "slots": slots,
                # 明确告诉调用方「预算已给」，避免上层把预算再问一遍
                "budget_known": slots["budget_known"],
                "askable_slots": [] if slots["budget_known"] else ["budget_range"]}

    if action == "school_ambiguous":
        # message 同时放在 result 内（与其它分支一致）和顶层（历史读取端兼容）
        ambiguous_msg = CONFIG["similar_school_fallback"]["school_ambiguous_msg"]
        return {
            "ok": True,
            "action": "school_ambiguous",
            "slots": slots,
            "message": ambiguous_msg,
            "candidates": slots.get("school_candidates", []),
            "result": {
                "message": ambiguous_msg,
                "candidates": slots.get("school_candidates", []),
            },
        }

    if action == "school_not_found":
        return handle_school_not_found(slots)

    if action == "recommend_by_budget":
        return handle_recommend_by_budget(slots)

    return handle_estimate(slots)


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
    except Exception:
        request = {}
    print(json.dumps(main(request), ensure_ascii=False))