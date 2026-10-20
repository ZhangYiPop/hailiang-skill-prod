"""Parse evaluation personas and answer follow-up fields from them.

Persona text in the evaluation table is a single line of `键:值` pairs.
The batch runner treats that text as the simulated user's knowledge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

GRADE_TO_STAGE = {
    "高一": "高中",
    "高二": "高中",
    "高三": "高中",
    "初一": "初中",
    "初二": "初中",
    "初三": "初中",
}
GRADE_VALUES = set(GRADE_TO_STAGE) | {"小学"}
STAGE_VALUES = {"小学", "初中", "高中"}
PARENT_HINTS = ("孩子", "我家", "闺女", "儿子", "家长", "小孩")
STUDENT_HINTS = ("我该", "我想", "我以后", "我最", "我讨厌", "我可以")

KEY_ALIASES = {
    "城市": "city",
    "学段": "stage_raw",
    "性别": "gender",
    "成绩": "score_level",
    "学习状态": "study_state",
    "各科成绩": "subject_scores",
    "兴趣": "interest",
    "特长": "talent",
    "优势学科": "strengths",
    "预算": "budget",
    "学费预算": "budget",
    "总分": "score_total",
    "总体成绩大致年级排名": "rank",
    "年级排名": "rank",
    "大致年级排名": "rank",
    "年级大致排名": "rank",
    "性格特点": "personality",
    "性格": "personality",
    "留学目的地": "abroad_destination",
    "目标学校类型": "school_type",
    "城市层级": "city_tier",
    "意向专业": "major",
    "语言成绩": "language_score",
    "选科": "subject_choice",
    "选科情况": "subject_choice",
    "家庭资源": "family_resource",
}

PROVINCES = {
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
}
CITY_TO_PROVINCE = {
    "广州": "广东", "深圳": "广东", "杭州": "浙江", "宁波": "浙江", "南京": "江苏",
    "苏州": "江苏", "西安": "陕西", "郑州": "河南", "武汉": "湖北", "成都": "四川",
    "石家庄": "河北", "长沙": "湖南", "合肥": "安徽", "福州": "福建", "厦门": "福建",
    "济南": "山东", "青岛": "山东", "昆明": "云南", "贵阳": "贵州", "太原": "山西",
    "沈阳": "辽宁", "大连": "辽宁", "哈尔滨": "黑龙江", "长春": "吉林", "南昌": "江西",
    "南宁": "广西", "兰州": "甘肃", "海口": "海南", "乌鲁木齐": "新疆", "呼和浩特": "内蒙古",
}

# Field key / label fragments that should read a canonical persona attribute.
FIELD_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("user_role", "身份", "角色"), "user_role"),
    (("school_stage", "学段"), "school_stage"),
    (("grade", "年级"), "grade"),
    (("province", "省份", "城市", "地区"), "province"),
    (("score_total", "总分", "分数", "成绩"), "score_text"),
    (("subject_group", "选科"), "subject_choice"),
    (("兴趣", "interest"), "interest"),
    (("特长", "talent"), "talent"),
    (("预算", "budget"), "budget"),
    (("性格", "personality"), "personality"),
    (("专业", "major"), "major"),
    (("排名", "rank"), "rank"),
    (("性别", "gender"), "gender"),
    (("留学", "目的地"), "abroad_destination"),
    (("语言", "雅思", "托福"), "language_score"),
)


@dataclass
class Persona:
    raw: str
    fields: dict[str, str] = field(default_factory=dict)
    user_role: str = "家长"
    school_stage: str = ""
    grade: str = ""
    province: str = ""
    city: str = ""

    def attribute(self, name: str) -> str:
        if name == "user_role":
            return self.user_role
        if name == "school_stage":
            return self.school_stage
        if name == "grade":
            return self.grade or self.fields.get("stage_raw", "")
        if name == "province":
            return self.province or self.city
        if name == "score_text":
            return self.fields.get("score_total") or self.fields.get("subject_scores") or self.fields.get("score_level") or ""
        if name == "subject_choice":
            return self.fields.get("subject_choice") or self.subject_group()
        return self.fields.get(name, "")

    def subject_group(self) -> str:
        text = self.fields.get("subject_choice", "")
        if "物理" in text or text.startswith("物"):
            return "物理"
        if "历史" in text or text.startswith("历"):
            return "历史"
        return ""

    def seed_facts(self, *, preset_identity: bool, seed_profile: bool) -> dict[str, str]:
        facts: dict[str, str] = {}
        if preset_identity:
            if self.user_role:
                facts["user_role"] = self.user_role
            if self.school_stage:
                facts["school_stage"] = self.school_stage
        if seed_profile:
            if self.grade in GRADE_VALUES:
                facts["grade"] = self.grade
            if self.province:
                facts["student_province"] = self.province
            total = self.fields.get("score_total", "")
            if total:
                facts["score_total"] = total
            group = self.subject_group()
            if group:
                facts["subject_group"] = group
        return facts

    def brief_reply(self) -> str:
        bits = [f"我是{self.user_role}"]
        place = f"{self.city}{self.fields.get('stage_raw') or self.school_stage}"
        if place.strip():
            bits.append(f"在{place}")
        if self.fields.get("score_level"):
            bits.append(f"成绩{self.fields['score_level']}")
        if self.fields.get("interest"):
            bits.append(f"兴趣是{self.fields['interest']}")
        if self.fields.get("subject_scores"):
            bits.append(f"各科成绩{self.fields['subject_scores']}")
        return "，".join(bits) + "。其余没提到的我暂时不清楚。"


def parse_persona(text: str, opening: str = "") -> Persona:
    raw = str(text or "").strip()
    fields = _split_fields(raw)
    stage_raw = fields.get("stage_raw", "")
    grade, school_stage = _stage_and_grade(stage_raw)
    city = fields.get("city", "")
    return Persona(
        raw=raw,
        fields=fields,
        user_role=_infer_role(opening),
        school_stage=school_stage,
        grade=grade,
        province=_province(city),
        city=city,
    )


def answer_fields(fields: list[dict], persona: Persona) -> tuple[dict, dict[str, str]]:
    """Return answers keyed by field key, and a source note per answered field.

    Unanswered fields are omitted. Callers skip the request when nothing matches.
    """
    answers: dict = {}
    sources: dict[str, str] = {}
    for item in fields:
        value, source = _answer_one(item, persona)
        if value is None or value == "" or value == []:
            continue
        answers[item["key"]] = value
        sources[item["key"]] = source
    return answers, sources


def _split_fields(text: str) -> dict[str, str]:
    normalized = text.replace("；", ";")
    normalized = re.sub(r"([)）])\s*(?=[^\s;:：]{1,16}[:：])", r"\1;", normalized)
    result: dict[str, str] = {}
    for part in normalized.split(";"):
        piece = part.strip()
        if not piece:
            continue
        matched = re.split(r"[:：]", piece, maxsplit=1)
        if len(matched) != 2:
            continue
        key = matched[0].strip()
        value = matched[1].strip()
        if not key or not value:
            continue
        canonical = KEY_ALIASES.get(key, key)
        result.setdefault(canonical, value)
    return result


def _stage_and_grade(stage_raw: str) -> tuple[str, str]:
    text = stage_raw.strip()
    if text in GRADE_TO_STAGE:
        return text, GRADE_TO_STAGE[text]
    if text in STAGE_VALUES:
        return "", text
    if "小学" in text or re.search(r"[一二三四五六1-6]年级", text):
        return "", "小学"
    if "初" in text:
        return "", "初中"
    if "高" in text:
        return "", "高中"
    return "", ""


def _infer_role(opening: str) -> str:
    text = opening or ""
    if any(hint in text for hint in PARENT_HINTS):
        return "家长"
    if any(hint in text for hint in STUDENT_HINTS):
        return "学生"
    return "家长"


def _province(city: str) -> str:
    text = city.strip()
    if not text:
        return ""
    compact = text.replace("省", "").replace("市", "")
    if text in PROVINCES or compact in PROVINCES:
        return compact if compact in PROVINCES else text
    for name, province in CITY_TO_PROVINCE.items():
        if name in text:
            return province
    for province in PROVINCES:
        if province in text:
            return province
    return ""


def _answer_one(item: dict, persona: Persona) -> tuple[object | None, str]:
    hint_text = " ".join(
        str(item.get(key) or "")
        for key in ("key", "fact_key", "label")
    )
    attribute = ""
    for hints, name in FIELD_HINTS:
        if any(hint and hint in hint_text for hint in hints):
            attribute = name
            break
    candidate = persona.attribute(attribute) if attribute else ""
    if not candidate:
        for raw_key, raw_value in persona.fields.items():
            label = item.get("label") or ""
            if raw_key and raw_key in str(label):
                candidate = raw_value
                attribute = raw_key
                break
    if not candidate:
        return None, ""
    kind = item.get("input_type") or "text"
    source = f"画像:{attribute or '原文'}"
    if kind == "text":
        return str(candidate), source
    options = item.get("options") or []
    if kind == "single_select":
        matched = _match_option(str(candidate), options)
        return (matched, source) if matched else (None, "")
    if kind == "multi_select":
        matched = [option["value"] for option in options if _option_hit(str(candidate), option)]
        return (matched, source) if matched else (None, "")
    if kind in {"integer", "number"}:
        numbers = re.findall(r"\d+(?:\.\d+)?", str(candidate))
        if not numbers:
            return None, ""
        number = float(numbers[0])
        return (int(number) if kind == "integer" and number.is_integer() else number), source
    return str(candidate), source


def _match_option(candidate: str, options: list[dict]) -> str | None:
    for option in options:
        if _option_hit(candidate, option):
            return str(option.get("value") or "")
    return None


def _option_hit(candidate: str, option: dict) -> bool:
    label = str(option.get("label") or "")
    value = str(option.get("value") or "")
    if not value:
        return False
    if value == candidate or label == candidate:
        return True
    if len(value) >= 2 and (value in candidate or candidate in value):
        return True
    return len(label) >= 2 and (label in candidate or candidate in label)
