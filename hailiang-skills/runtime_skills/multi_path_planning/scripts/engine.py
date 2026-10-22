#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多元升学路径匹配引擎（主线 multi-path-planning-v3 · 四改后字段模型）

平台脚本约定：stdin 注入单 JSON 请求 → stdout 只输出一个 JSON 对象。
运行依赖仅 Python 3.9+ 标准库；通过自身文件位置定位同包 assets/，不依赖当前工作目录。

数据源（唯一出处）：
  assets/province_line.json   2026 分数线（58 条，province + subject + full_marks/TL/BL/ZL）
  assets/pathway_rules.json   路径规则数据（文案 / 职业映射 / 地区特色 / 阈值 / 单路径字段集）

字段键 = assets/questionnaire.json 的 question id（见 SKILL.md D-08 契约）：
  student_type / gaokao_province / grade / subject_choice / estimated_score / foreign_lang /
  english_score / ethnicity / border_guard_child / region_preference / education_budget /
  aspiration_goal / career_priority / zhejiang_xuekao / olympiad_level /
  gender / height_cm / vision_left / vision_right

正文补问（不进问卷、由 SKILL.md 4.1 补问后随请求传入）：
  tibet_class（西藏 A类/B类）

动作一览（请求 JSON 的 "action" 字段）：
  compute-tier    计算分数档
  match           完整匹配
  match-single    单路径匹配
  arts-sports     艺体子流程（占位，规则另见 references/art_sports_rules.md）
  verify          回归自检

【R1 防泄线】本引擎输出中不出现任何内部分数线/阈值/分数区间数字。依据文本一律为
用户侧客观事实 + 达标/未达标结论；分数线/阈值只存在于 assets/*.json，仅供引擎内部比较。
"""

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINES_PATH = os.path.join(BASE_DIR, "assets", "province_line.json")
RULES_PATH = os.path.join(BASE_DIR, "assets", "pathway_rules.json")

_SUBJECT_ALIAS = {
    "物": "物理", "理": "物理",
    "化": "化学",
    "生": "生物",
    "政": "政治",
    "史": "历史",
    "地": "地理",
    "技": "技术",
}
_PROVINCE_SUFFIX = ["维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "省", "市"]

# 省份模式（与 province_lines_2026.md §5.1 对齐）
_G3PLUS3 = ["北京", "天津", "上海", "浙江", "山东", "海南"]
_LEGACY = ["西藏", "新疆"]


def _load():
    lines = json.load(open(LINES_PATH, encoding="utf-8"))
    rules = json.load(open(RULES_PATH, encoding="utf-8"))
    return lines, rules


_LINES, _RULES = _load()
_PATH_META = _RULES["path_meta"]
_REGION_FEATURES = _RULES["region_features"]
_CAREER_TAG_MAP = _RULES["career_tag_map"]
_TH = _RULES["thresholds"]
_PHYSIQUE = _RULES["physique"]


# ---------------------------------------------------------------------------
# 归一化小工具
# ---------------------------------------------------------------------------

def _norm_province(p):
    if not p:
        return None
    p = str(p).strip()
    if p == "不参加内地高考":
        return p
    for suf in _PROVINCE_SUFFIX:
        if p.endswith(suf):
            return p[: -len(suf)]
    return p


def _norm_subjects(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip().replace("，", ",").replace(" ", ",").replace("、", ",")
        if "," not in s and len(s) <= 3 and all(ch in _SUBJECT_ALIAS for ch in s):
            return [_SUBJECT_ALIAS[ch] for ch in s]
        parts = [x for x in s.split(",") if x]
    else:
        parts = list(raw)
    out = []
    for x in parts:
        x = str(x).strip()
        if not x:
            continue
        out.append(_SUBJECT_ALIAS.get(x, x))
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


def _norm_opt(v):
    if v is None:
        return None
    return str(v).strip()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _province_mode(p):
    if p in _G3PLUS3:
        return "g3plus3"
    if p in _LEGACY:
        return "legacy"
    return "g312"


# ---------------------------------------------------------------------------
# 分数线解析与分数档（阈值只在内部使用，不随响应输出）
# ---------------------------------------------------------------------------

def _resolve_lines(province, subjects=None, tibet_class=None):
    """返回 (ok, info)。info 内部含 tl/bl/zl/full_marks，供引擎比较，禁止外发。"""
    subjects = _norm_subjects(subjects)
    province = _norm_province(province)
    if not province or province == "不参加内地高考":
        return False, {"error": "province_not_applicable"}

    mode = _province_mode(province)

    if mode == "legacy":
        # 西藏 / 新疆：科类由 subject_choice 判定（含物理→理科，含历史→文科）
        if province == "西藏":
            klass = _norm_opt(tibet_class)
            if klass not in ("A类", "B类"):
                return False, {"error": "need_tibet_class", "missing": ["tibet_class"]}
            prov_key = "西藏（" + klass + "）"
        else:
            prov_key = "新疆"
        if "物理" in subjects:
            key = "物理"
        elif "历史" in subjects:
            key = "历史"
        else:
            return False, {"error": "need_subject_track", "missing": ["subject_choice"]}
        row = next((r for r in _LINES if r["province"] == prov_key and r["subject"] == key), None)
        if row is None:
            return False, {"error": "province_unsupported"}
        full = int(row["full_marks"])
        return True, {"mode": mode, "province": province, "full_marks": full,
                      "category": "理科" if key == "物理" else "文科",
                      "tl": int(row["TL"]), "bl": int(row["BL"]), "zl": int(row["ZL"])}

    rows = [r for r in _LINES if r["province"] == province]
    if not rows:
        return False, {"error": "province_unsupported"}

    if mode == "g3plus3":
        row = rows[0]  # subject == "不限"
        full = int(row["full_marks"])
        return True, {"mode": mode, "province": province, "full_marks": full,
                      "category": "统一", "tl": int(row["TL"]), "bl": int(row["BL"]), "zl": int(row["ZL"])}

    # g312
    if "物理" in subjects:
        key = "物理"
    elif "历史" in subjects:
        key = "历史"
    else:
        return False, {"error": "need_first_choice", "missing": ["subject_choice"]}
    row = next((r for r in rows if r["subject"] == key), None)
    if row is None:
        return False, {"error": "province_unsupported"}
    full = int(row["full_marks"])
    return True, {"mode": mode, "province": province, "full_marks": full,
                  "category": "物理类" if key == "物理" else "历史类",
                  "tl": int(row["TL"]), "bl": int(row["BL"]), "zl": int(row["ZL"])}


def _compute_tier(score, lines):
    """tier_key: tl_up / benke / zhuanke / below。"""
    if score >= lines["tl"]:
        return "tl_up"
    if score >= lines["bl"]:
        return "benke"
    if score >= lines["zl"]:
        return "zhuanke"
    return "below"


def _tier_of(profile):
    if profile.get("gaokao_province") == "不参加内地高考":
        return True, {"skip_tier": True}
    if profile.get("estimated_score") in (None, ""):
        return False, {"need_facts": ["gaokao_province", "subject_choice", "estimated_score"],
                       "message": "缺少预估总分，无法计算分数档"}
    ok, lines = _resolve_lines(profile.get("gaokao_province"), profile.get("subject_choice"),
                               profile.get("tibet_class"))
    if not ok:
        err = lines.get("error")
        if err == "need_first_choice" or err == "need_subject_track":
            return False, {"need_facts": ["subject_choice"], "message": "需先明确首选物理或历史"}
        if err == "need_tibet_class":
            return False, {"need_facts": lines.get("missing", []), "message": "西藏考生需补充 A/B 类"}
        return False, {"need_facts": ["gaokao_province"], "message": "无法计算分数档"}
    tier_key = _compute_tier(profile.get("estimated_score"), lines)
    return True, {
        "tier_key": tier_key, "category": lines["category"], "mode": lines["mode"],
        "scale": lines["full_marks"], "needs_recheck": profile.get("grade") == "高一",
        "tl": lines["tl"], "bl": lines["bl"], "zl": lines["zl"], "skip_tier": False,
    }


# ---------------------------------------------------------------------------
# 画像构造
# ---------------------------------------------------------------------------

def _build_profile(payload):
    p = {
        "student_type": _norm_opt(payload.get("student_type")),
        "gaokao_province": _norm_province(payload.get("gaokao_province")),
        "grade": _norm_opt(payload.get("grade")),
        "subject_choice": _norm_subjects(payload.get("subject_choice")),
        "estimated_score": payload.get("estimated_score"),
        "foreign_lang": _norm_opt(payload.get("foreign_lang")),
        "english_score": payload.get("english_score"),
        "ethnicity": _norm_opt(payload.get("ethnicity")),
        "border_guard_child": _norm_opt(payload.get("border_guard_child")),
        "region_preference": _norm_opt(payload.get("region_preference")),
        "education_budget": _norm_opt(payload.get("education_budget")),
        "aspiration_goal": payload.get("aspiration_goal") or [],
        "career_priority": payload.get("career_priority") or [],
        "zhejiang_xuekao": _norm_opt(payload.get("zhejiang_xuekao")),
        "olympiad_level": _norm_opt(payload.get("olympiad_level")),
        "gender": _norm_opt(payload.get("gender")),
        "height_cm": payload.get("height_cm"),
        "vision_left": payload.get("vision_left"),
        "vision_right": payload.get("vision_right"),
        "tibet_class": _norm_opt(payload.get("tibet_class")),
    }
    if isinstance(p["career_priority"], str):
        p["career_priority"] = [x.strip() for x in p["career_priority"].replace("，", ",").split(",") if x.strip()]
    if isinstance(p["aspiration_goal"], str):
        p["aspiration_goal"] = [x.strip() for x in p["aspiration_goal"].replace("，", ",").split(",") if x.strip()]
    tags = []
    for x in p["career_priority"]:
        t = _CAREER_TAG_MAP.get(_norm_opt(x))
        if t and t not in tags:
            tags.append(t)
    p["job_tags"] = tags
    return p


def _has_value(profile, key):
    v = profile.get(key)
    if key == "subject_choice":
        return bool(v)
    return v not in (None, "")


# ---------------------------------------------------------------------------
# 依据文本（R1 安全：只给用户侧事实 + 达标结论，不出现阈值数字）
# ---------------------------------------------------------------------------

def _score_fact(profile):
    return "成绩达该路径对应层次" if profile.get("estimated_score") not in (None, "") else "分数待确认"


def _recheck(basis, profile):
    if profile.get("grade") == "高一":
        return list(basis) + ["按倾向选科预判，正式选科后需复核"]
    return list(basis)


def _phys_basic(profile):
    h = _num(profile.get("height_cm"))
    vl = _num(profile.get("vision_left"))
    vr = _num(profile.get("vision_right"))
    vision = min(vl, vr) if (vl is not None and vr is not None) else None
    return {"gender": profile.get("gender"), "height": h, "vision": vision,
            "foreign": profile.get("foreign_lang"), "english": _num(profile.get("english_score")),
            "missing": [k for k in ("gender", "height_cm", "vision_left", "vision_right")
                        if (k == "gender" and profile.get("gender") is None)
                        or (k == "height_cm" and h is None)
                        or (k in ("vision_left", "vision_right") and vision is None)]}


# ---------------------------------------------------------------------------
# 路径判定
# ---------------------------------------------------------------------------

def _mk(path_id, profile, basis=None, note=None):
    meta = _PATH_META.get(path_id, {})
    return {"id": path_id, "name": path_id, "badge": "matched",
            "rank": meta.get("rank", 60), "loc": meta.get("loc", ""),
            "next": meta.get("next", ""), "basis": _recheck(basis or [], profile),
            "note": note or [], "unmet": [], "fix": [], "uncertain": False, "need_fields": []}


def _mk_c(path_id, profile, unmet, fix, uncertain=False, need_fields=None):
    meta = _PATH_META.get(path_id, {})
    return {"id": path_id, "name": path_id, "badge": "caution",
            "rank": 70, "loc": meta.get("loc", ""), "next": meta.get("next", ""),
            "basis": [], "note": [], "unmet": [unmet] if unmet else [],
            "fix": list(fix or []), "uncertain": uncertain, "need_fields": list(need_fields or [])}


def _eval_paths(profile):
    matched, caution = [], []
    meta = {"watch_region": [], "watch_arts_sports": True, "tier_note": None}
    S = _num(profile.get("estimated_score"))
    st = profile.get("student_type")

    # 身份驱动
    if st in ("港澳台考生", "华侨身份考生"):
        matched.append(_mk("港澳台华侨招生", profile, basis=["身份符合港澳台华侨招生范围"]))
    if profile.get("ethnicity") == "少数民族":
        matched.append(_mk("少数民族招生", profile, basis=["民族信息符合少数民族招生范围"]))
    if profile.get("border_guard_child") == "是":
        matched.append(_mk("边防军人子女预科班", profile, basis=["身份信息符合边防军人子女预科班范围"]))

    # 不参加内地高考：身份类保留；另评「不以内地分数线 / 竞赛为前提」的偏好类路径（海外升学）
    if profile.get("gaokao_province") == "不参加内地高考":
        if profile.get("region_preference") == "接受出国或港澳升学" and profile.get("education_budget") == "5万以上":
            matched.append(_mk("海外升学", profile, basis=["可接受出国/港澳方向且预算满足"]))
        meta["watch_region"] = _REGION_FEATURES.get(profile.get("gaokao_province"), [])
        return matched, caution, meta

    # 普通高考保底
    matched.append(_mk("普通高考", profile, basis=["常规高考路径无附加门槛"]))

    if S is None:
        meta["watch_region"] = _REGION_FEATURES.get(profile.get("gaokao_province"), [])
        return matched, caution, meta

    ok, ti = _tier_of(profile)
    if not ok:
        meta["tier_note"] = {"need_facts": ti.get("need_facts", []), "message": ti.get("message", "")}
        return matched, caution, meta
    tl, bl, zl = ti["tl"], ti["bl"], ti["zl"]

    # 高分自动
    if S >= tl:
        matched.append(_mk("强基计划", profile, basis=[_score_fact(profile)]))
        matched.append(_mk("综合评价", profile, basis=[_score_fact(profile)]))

    # 少年班 / 保送生
    comp = profile.get("olympiad_level")
    if comp == "国集":
        matched.append(_mk("少年班", profile, basis=["竞赛成绩达少年班相关层次"]))
        matched.append(_mk("保送生", profile, basis=["竞赛成绩达保送资格相关层次"]))
    elif comp in ("省一", "省二", "省三", "国奖") and S >= tl:
        matched.append(_mk("少年班", profile, basis=["竞赛获奖且成绩达对应层次"]))

    # 省属三位一体（浙江）
    if profile.get("gaokao_province") == "浙江":
        lo, hi = _TH["三位一体_lower_offset"], _TH["三位一体_upper_offset"]
        if (bl + lo) <= S < (tl + hi):
            xk = profile.get("zhejiang_xuekao")
            if xk not in ("有未合格", "未参加"):
                basis = ["成绩处于三位一体招生对应区间"]
                note = [] if xk == "全部合格" else ["学考成绩以实际是否全部合格为准"]
                matched.append(_mk("省属三位一体", profile, basis=basis, note=note))

    # 小语种高考
    fl = profile.get("foreign_lang")
    eng = _num(profile.get("english_score"))
    grade = profile.get("grade")
    if fl == "英语":
        if grade in ("高一", "高二") and eng is not None and eng <= _TH["小语种_english_max"]:
            matched.append(_mk("小语种高考", profile, basis=["英语成绩偏弱且仍处可调整阶段，可评估转小语种"]))
    elif fl and fl != "英语":
        matched.append(_mk("小语种高考", profile, basis=["高考外语为非英语语种"]))

    # 高职单招
    if S < bl + _TH["高职单招_bl_offset"]:
        matched.append(_mk("高职单招", profile, basis=["预估总分区间与高职单招较匹配"]))

    # 偏好驱动：预算 / 地域
    budget_ok = profile.get("education_budget") == "5万以上"
    region = profile.get("region_preference")
    if budget_ok:
        note = ["可关注可全程国内就读的4+0模式"] if region == "仅考虑内地升学" else []
        matched.append(_mk("中外合作办学", profile, basis=["教育预算满足该类项目通常学费水平"], note=note))
        if fl == "英语" and eng is not None and eng >= _TH["港澳升学_english_min"] and S >= bl and region != "仅考虑内地升学":
            matched.append(_mk("港澳升学", profile, basis=["英语与成绩达港澳高校本科通常门槛", "教育预算满足"]))
    if region == "接受出国或港澳升学" and budget_ok:
        matched.append(_mk("海外升学", profile, basis=["可接受出国/港澳方向且预算满足"]))

    # 中外合作值得关注分支（tier_3 + 预算未达 + 冲刺院校层次）→ 由 SKILL.md 编排层处理，
    # 引擎在 match 结果中带出标记
    meta["watch_sino_foreign"] = (
        ti["tier_key"] == "zhuanke"
        and profile.get("education_budget") != "5万以上"
        and "冲刺院校层次" in profile.get("aspiration_goal", [])
    )

    # 偏好驱动：职业兴趣
    _pref_driven(profile, S, tl, bl, matched, caution)

    meta["watch_region"] = _REGION_FEATURES.get(profile.get("gaokao_province"), [])
    return matched, caution, meta


def _pref_driven(profile, S, tl, bl, matched, caution):
    tags = profile.get("job_tags", [])
    pb = _phys_basic(profile)
    vision, h, gender, foreign, eng = pb["vision"], pb["height"], pb["gender"], pb["foreign"], pb["english"]
    phys_unknown = len(pb["missing"]) > 0

    def _add_or_caution(path_id, okk, unmet, fix, need_unknown=False, need_fields=None):
        if okk:
            matched.append(_mk(path_id, profile, basis=["硬性体检与外语条件符合"]))
        elif need_unknown:
            caution.append(_mk_c(path_id, profile, unmet, [], uncertain=True, need_fields=need_fields))
        else:
            caution.append(_mk_c(path_id, profile, unmet, fix))

    if "军警类" in tags:
        for name in ("军校招生", "公安警校招生", "司法警校招生", "定向培养军士"):
            phys = _PHYSIQUE[name]
            # 档位门槛：军校/公安/司法 <BL 不适用（军士无门槛）；军校还需 ≥TL
            if name == "军校招生":
                if S < bl:
                    continue  # 不适用，不输出
                if foreign != "英语":
                    _add_or_caution(name, False, "高考外语科目需为英语", ["外语科目不符合则需重新评估该路径"])
                    continue
                if phys_unknown:
                    _add_or_caution(name, False, "需补充性别/身高/双眼裸眼视力后再确认", [],
                                    need_unknown=True, need_fields=["gender", "height_cm", "vision_left", "vision_right"])
                    continue
                if S < tl:
                    _add_or_caution(name, False, "成绩未达该路径对应层次", ["建议提升预估成绩后再评估"])
                    continue
            elif name in ("公安警校招生", "司法警校招生"):
                if S < bl:
                    continue
                if phys_unknown:
                    _add_or_caution(name, False, "需补充性别/身高/双眼裸眼视力后再确认", [],
                                    need_unknown=True, need_fields=["gender", "height_cm", "vision_left", "vision_right"])
                    continue
            else:  # 定向培养军士：无分数门槛
                if phys_unknown:
                    _add_or_caution(name, False, "需补充性别/身高/双眼裸眼视力后再确认", [],
                                    need_unknown=True, need_fields=["gender", "height_cm", "vision_left", "vision_right"])
                    continue
            okk = (vision is not None and vision >= phys["vision_min"]
                   and h >= (phys["height_male"] if gender == "男" else phys["height_female"]))
            _add_or_caution(name, okk, "硬性体检条件未全部满足",
                            ["可对照当年官方体检/政审标准自查，矫正视力等达标的可再确认"])

    if "师范类" in tags:
        if S < bl:
            pass  # 不适用
        elif S >= tl + _TH["公费师范_tl_offset"]:
            matched.append(_mk("公费师范生", profile, basis=["成绩达公费师范生对应层次", "职业方向为师范"]))
        elif S >= tl:
            caution.append(_mk_c("公费师范生", profile, "成绩距该路径较高门槛尚有差距",
                                 ["可先锁定师范类院校，再关注公费计划动态"]))
        else:
            caution.append(_mk_c("公费师范生", profile, "成绩未达该路径对应层次", ["建议提升预估成绩后再评估"]))

    if "飞行员" in tags:
        scale = _resolve_lines(profile.get("gaokao_province"), profile.get("subject_choice"),
                               profile.get("tibet_class"))
        scale = scale[1]["full_marks"] if scale[0] else 750
        if phys_unknown or eng is None:
            _add_or_caution("三大招飞", False, "需补充性别/身高/双眼裸眼视力/英语成绩后再确认", [],
                            need_unknown=True,
                            need_fields=["gender", "height_cm", "vision_left", "vision_right", "english_score"])
            return
        t = _TH
        cand1 = (S >= scale * t["招飞1_score_ratio"] and foreign == "英语" and eng >= t["招飞1_english_min"]
                 and t["招飞1_height_min"] <= h <= t["招飞1_height_max"] and vision >= t["招飞1_vision_min"]
                 and "物理" in profile["subject_choice"] and "化学" in profile["subject_choice"])
        cand2 = (S >= tl and foreign in ("英语", "俄语") and t["招飞2_height_min"] <= h <= t["招飞2_height_max"]
                 and vision >= t["招飞2_vision_min"] and "物理" in profile["subject_choice"])
        if cand1 or cand2:
            matched.append(_mk("三大招飞", profile, basis=["身高、视力、外语与选科条件符合", "成绩达对应层次"]))
        else:
            caution.append(_mk_c("三大招飞", profile, "硬性条件未全部满足",
                                 ["招飞体检与选拔标准严格，可先对照当年选拔要求自查"]))

    if "农科类" in tags:
        if S < bl:
            pass
        elif S >= bl:
            matched.append(_mk("定向农科", profile, basis=[_score_fact(profile)],
                               note=["部分省份要求农村户籍或定向就业县域户籍，报考前请核实"]))
        else:
            caution.append(_mk_c("定向农科", profile, "成绩未达标", ["建议提升预估成绩后再评估；户籍等条件以当地当年政策为准"]))

    if "医学类" in tags:
        if S < bl:
            pass
        elif S >= bl:
            matched.append(_mk("定向医学", profile, basis=[_score_fact(profile)],
                               note=["部分省份要求农村户籍或定向就业县域户籍，报考前请核实"]))
        else:
            caution.append(_mk_c("定向医学", profile, "成绩未达标", ["建议提升预估成绩后再评估；户籍等条件以当地当年政策为准"]))


# 单路径匹配最小字段集（键名对齐 questionnaire id）
_PATH_MIN_FIELDS = _RULES["single_path_min_fields"]
_PATH_IMPLIED_TAG = _RULES["path_implied_tag"]
_PATH_ALIAS = _RULES["path_alias"]


def _normalize_path(raw):
    key = _norm_opt(raw)
    if not key:
        return None
    if key in _PATH_ALIAS:
        return _PATH_ALIAS[key]
    for alias, real in _PATH_ALIAS.items():
        if key.startswith(alias):
            return real
    return key if key in _PATH_MIN_FIELDS else None


def _dedup(xs):
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------------------
# 动作实现
# ---------------------------------------------------------------------------

def _act_compute_tier(payload):
    profile = _build_profile(payload)
    ok, res = _tier_of(profile)
    if not ok:
        return {"ok": False, "action": "compute-tier",
                "need_facts": res.get("need_facts", []), "message": res.get("message", "")}
    if res.get("skip_tier"):
        return {"ok": True, "action": "compute-tier", "skip_tier": True}
    return {"ok": True, "action": "compute-tier", "tier_key": res["tier_key"],
            "category": res["category"], "mode": res["mode"], "needs_recheck": res["needs_recheck"]}


def _act_match(payload):
    profile = _build_profile(payload)
    if profile.get("gaokao_province") != "不参加内地高考":
        missing = []
        for k in ("gaokao_province", "subject_choice", "estimated_score"):
            if not _has_value(profile, k):
                missing.append(k)
        if missing:
            return {"ok": False, "action": "match", "need_facts": missing, "message": "缺少匹配必需字段"}
    matched, caution, meta = _eval_paths(profile)
    return {
        "ok": True, "action": "match",
        "matched": sorted(matched, key=lambda x: x["rank"]),
        "caution": sorted(caution, key=lambda x: x["rank"]),
        "watch_region": meta.get("watch_region", []),
        "watch_sino_foreign": meta.get("watch_sino_foreign", False),
        "arts_sports_teaser": meta.get("watch_arts_sports", True),
        "tier_note": meta.get("tier_note"),
    }


def _act_match_single(payload):
    path = _normalize_path(payload.get("path"))
    if path is None:
        return {"ok": False, "action": "match-single", "unsupported": True,
                "message": "该路径不在内置匹配集内，按科普作答，不做匹配判定"}
    profile = _build_profile(payload)

    implied = _PATH_IMPLIED_TAG.get(path)
    if implied and implied not in profile.get("job_tags", []):
        profile["job_tags"] = profile.get("job_tags", []) + [implied]

    missing = []
    for k in _PATH_MIN_FIELDS.get(path, []):
        if not _has_value(profile, k):
            if k in ("vision_left", "vision_right") and "vision_left" not in missing:
                missing += ["vision_left", "vision_right"]
            else:
                missing.append(k)
    if profile.get("gaokao_province") not in (None, "不参加内地高考"):
        if profile.get("gaokao_province") in _LEGACY:
            if profile.get("gaokao_province") == "西藏" and _norm_opt(payload.get("tibet_class")) not in ("A类", "B类"):
                missing.append("tibet_class")
    missing = _dedup(missing)
    if missing:
        return {"ok": False, "action": "match-single", "path": path,
                "need_facts": missing, "message": "还需补充字段后即可判定"}

    matched, caution, _meta = _eval_paths(profile)
    verdicts = [x for x in matched + caution if x["id"] == path]
    if not verdicts:
        return {"ok": True, "action": "match-single", "path": path,
                "verdicts": [], "matched": False, "message": "当前信息下该路径未命中匹配条件"}
    matched_flag = any(x["badge"] == "matched" for x in verdicts)
    return {"ok": True, "action": "match-single", "path": path,
            "verdicts": verdicts, "matched": matched_flag}


def _act_arts_sports(payload):
    # 艺体子流程规则见 references/art_sports_rules.md；引擎仅回传占位，由 SKILL.md 编排层驱动文本判定
    return {"ok": True, "action": "arts-sports", "verdicts": [],
            "note": "艺体判定由 SKILL.md 编排层按 references/art_sports_rules.md 执行"}


# ---------------------------------------------------------------------------
# verify 回归自检
# ---------------------------------------------------------------------------

def _act_verify():
    cases = [
        ("tier-浙江600", "compute-tier",
         {"gaokao_province": "浙江", "subject_choice": ["物理", "化学"], "estimated_score": 600},
         {"tier_key": "tl_up"}),
        ("tier-河北450物理", "compute-tier",
         {"gaokao_province": "河北", "subject_choice": ["物理", "化学"], "estimated_score": 450},
         {"tier_key": "benke"}),
        ("tier-广东410历史", "compute-tier",
         {"gaokao_province": "广东", "subject_choice": ["历史", "政治", "地理"], "estimated_score": 410},
         {"tier_key": "zhuanke"}),
        ("tier-天津300", "compute-tier",
         {"gaokao_province": "天津", "subject_choice": ["物理", "化学"], "estimated_score": 300},
         {"tier_key": "zhuanke"}),
        ("tier-西藏A类文320", "compute-tier",
         {"gaokao_province": "西藏", "subject_choice": ["政治", "历史", "地理"], "tibet_class": "A类",
          "estimated_score": 320},
         {"tier_key": "benke"}),
        ("tier-新疆理310", "compute-tier",
         {"gaokao_province": "新疆", "subject_choice": ["物理", "化学", "生物"], "estimated_score": 310},
         {"tier_key": "benke"}),
        ("match-浙江620军警师范", "match",
         {"student_type": "内地考生", "gaokao_province": "浙江", "grade": "高三",
          "subject_choice": ["物理", "化学"], "estimated_score": 620, "foreign_lang": "英语",
          "english_score": 125, "ethnicity": "汉族", "border_guard_child": "否",
          "region_preference": "仅考虑内地升学", "education_budget": "5万以上",
          "career_priority": ["军警", "师范"], "gender": "男", "height_cm": 178,
          "vision_left": 4.9, "vision_right": 4.9, "zhejiang_xuekao": "全部合格"},
         {"contains": ["军校招生", "公安警校招生", "司法警校招生", "定向培养军士",
                       "省属三位一体", "强基计划", "综合评价", "中外合作办学", "普通高考"],
          "caution_contains": ["公费师范生"]}),
        ("match-河北500无职业兴趣", "match",
         {"student_type": "内地考生", "gaokao_province": "河北", "grade": "高三",
          "subject_choice": ["物理", "化学"], "estimated_score": 500, "foreign_lang": "英语",
          "english_score": 110, "ethnicity": "汉族", "border_guard_child": "否",
          "region_preference": "仅考虑内地升学", "education_budget": "1万以下"},
         {"contains": ["普通高考"], "caution_empty": True}),
        ("match-澳门不参加内地高考", "match",
         {"student_type": "港澳台考生", "gaokao_province": "不参加内地高考", "grade": "高三"},
         {"contains": ["港澳台华侨招生"], "not_contains": ["普通高考", "强基计划"]}),
        ("match-内地不参加内地高考无偏好", "match",
         {"student_type": "内地考生", "gaokao_province": "不参加内地高考", "grade": "高三"},
         {"not_contains": ["普通高考", "强基计划", "综合评价", "港澳台华侨招生"]}),
        ("match-内地不参加内地高考海外升学", "match",
         {"student_type": "内地考生", "gaokao_province": "不参加内地高考", "grade": "高三",
          "region_preference": "接受出国或港澳升学", "education_budget": "5万以上"},
         {"contains": ["海外升学"], "not_contains": ["普通高考", "中外合作办学", "港澳升学"]}),
        ("match-内地不参加内地高考预算不足", "match",
         {"student_type": "内地考生", "gaokao_province": "不参加内地高考", "grade": "高三",
          "region_preference": "接受出国或港澳升学", "education_budget": "1万以下"},
         {"not_contains": ["海外升学", "普通高考"]}),
        ("match-港澳台不参加内地高考海外升学", "match",
         {"student_type": "港澳台考生", "gaokao_province": "不参加内地高考", "grade": "高三",
          "region_preference": "接受出国或港澳升学", "education_budget": "5万以上"},
         {"contains": ["港澳台华侨招生", "海外升学"], "not_contains": ["普通高考"]}),
        ("match-少数民族特控线上", "match",
         {"student_type": "内地考生", "gaokao_province": "河南", "grade": "高三",
          "subject_choice": ["物理", "化学"], "estimated_score": 560, "ethnicity": "少数民族",
          "border_guard_child": "否"},
         {"contains": ["少数民族招生", "强基计划", "普通高考"]}),
        ("single-强基缺分数", "match-single",
         {"path": "强基计划", "gaokao_province": "江苏", "subject_choice": ["物理", "化学"]},
         {"need_facts": True}),
        ("single-公安警校达标", "match-single",
         {"path": "公安警校", "gaokao_province": "广东", "grade": "高三",
          "subject_choice": ["物理", "化学"], "estimated_score": 500, "foreign_lang": "英语",
          "gender": "男", "height_cm": 175, "vision_left": 4.9, "vision_right": 4.9},
         {"verdicts_matched": ["公安警校招生"]}),
    ]
    results = []
    for name, action, payload, expect in cases:
        try:
            got = _dispatch(payload.copy(), action)
            results.append({"name": name, "pass": _check_expect(got, expect), "got": got})
        except Exception as e:  # noqa: BLE001
            results.append({"name": name, "pass": False, "error": str(e)})
    passed = sum(1 for r in results if r["pass"])
    return {"ok": True, "action": "verify", "total": len(results), "passed": passed,
            "all_pass": passed == len(results), "results": results}


def _check_expect(got, expect):
    for k, v in expect.items():
        if k == "tier_key":
            if got.get("tier_key") != v:
                return False
        elif k == "contains":
            ids = {x["id"] for x in got.get("matched", [])}
            if not set(v).issubset(ids):
                return False
        elif k == "caution_contains":
            ids = {x["id"] for x in got.get("caution", [])}
            if not set(v).issubset(ids):
                return False
        elif k == "caution_empty":
            if got.get("caution"):
                return False
        elif k == "not_contains":
            ids = {x["id"] for x in got.get("matched", [])}
            if set(v) & ids:
                return False
        elif k == "need_facts":
            if not got.get("need_facts"):
                return False
        elif k == "verdicts_matched":
            ids = {x["id"] for x in got.get("verdicts", []) if x.get("badge") == "matched"}
            if not set(v).issubset(ids):
                return False
    return True


def _dispatch(payload, action=None):
    action = action or payload.get("action")
    if action == "compute-tier":
        return _act_compute_tier(payload)
    if action == "match":
        return _act_match(payload)
    if action == "match-single":
        return _act_match_single(payload)
    if action == "arts-sports":
        return _act_arts_sports(payload)
    if action == "verify":
        return _act_verify()
    return {"ok": False, "error": "unknown_action", "action": action}


def main():
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        raw = None
    if raw is None or not raw.strip():
        out = _act_verify()
    else:
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            out = {"ok": False, "error": "invalid_json", "message": str(e)}
        else:
            if not isinstance(req, dict):
                out = {"ok": False, "error": "invalid_payload", "message": "请求需为 JSON 对象"}
            else:
                out = _dispatch(req)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
