from __future__ import annotations

from dataclasses import dataclass

from hailiang_skills.skill_runtime.models import SessionState, SkillBundle
from hailiang_skills.skill_runtime.skill_contract import get_stage_contract
from hailiang_skills.skill_runtime.state_tracker import (
    SATISFACTION_CONFIRMED,
    SATISFACTION_PENDING,
    SATISFACTION_REJECTED,
    ensure_runtime_state,
)

NEGATIVE_KEYWORDS = ("不对", "不是", "要改", "改一下", "不满意", "不太对", "不准确")
POSITIVE_KEYWORDS = ("可以", "就按这个", "没问题", "满意", "认可", "对的", "是这样")
SCENE_HINTS = (
    (
        "多元路径规划",
        (
            "多元路径规划",
            "多元路径",
            "多元升学路径",
            "替代路径",
            "路径收敛",
            "升学路径",
            "还有什么路",
            "还有哪些路",
            "还有别的路",
            "除了普通高考",
            "除了高考",
            "别的升学路",
            "其他升学路径",
        ),
    ),
    ("模拟升学", ("模拟升学", "模拟录取", "能上什么大学", "可报学校", "学校层次", "分数能上")),
    ("提分", ("提分", "提分规划", "提升成绩", "怎么补分")),
    ("兴趣探索", ("兴趣探索", "兴趣方向", "兴趣培养")),
    ("前景探路", ("前景探路", "就业前景", "专业前景", "职业前景")),
    ("选科参谋", ("选科参谋", "选科", "科目组合")),
)
STRUCTURED_FACT_FIELD_LABELS = (
    "当前分数",
    "最近三次大考均分",
    "最近三次大考平均分",
    "最近三次模考均分",
    "高考省份",
    "所在省份",
    "选科组合",
    "选科",
    "科目组合",
)
PROFILE_SLOT_KEYWORDS = (
    "孩子",
    "男孩",
    "女孩",
    "儿子",
    "女儿",
    "小学",
    "初中",
    "高中",
    "年级",
    "初一",
    "初二",
    "初三",
    "高一",
    "高二",
    "高三",
    "成绩",
    "排名",
    "班级",
    "年级",
    "前",
    "中等",
    "上游",
    "下游",
    "喜欢",
    "爱",
    "擅长",
    "特长",
    "画画",
    "美术",
    "编程",
    "钢琴",
    "舞蹈",
    "篮球",
    "足球",
    "阅读",
    "科幻",
    "乐高",
)
PROFILE_SLOT_REQUEST_KEYWORDS = (
    "兴趣探索",
    "特长判断",
    "前景探路",
    "职业方向",
    "专业前景",
    "长期发展",
    "选科",
    "模拟升学",
    "多元路径",
    "命理",
    "agent",
    "我想做",
    "想做",
    "进入",
    "切到",
    "换到",
    "打开",
    "看看",
    "讲讲",
    "分析",
    "规划",
    "能上",
    "能报",
    "哪个",
    "哪些",
    "怎么",
    "如何",
    "适合",
    "长期发展",
)


@dataclass(slots=True, frozen=True)
class IntentUpdate:
    satisfaction_status: str = SATISFACTION_PENDING
    needs_revise_conclusion: bool = False
    selected_scene: str = ""
    resume_to_skill_id: str = ""


def looks_like_structured_fact_update(latest_user: str) -> bool:
    normalized = latest_user.replace("：", ":")
    labeled_fields = sum(1 for label in STRUCTURED_FACT_FIELD_LABELS if f"{label}:" in normalized)
    separators = normalized.count(":")
    return labeled_fields >= 2 or (labeled_fields >= 1 and separators >= 2)


def looks_like_profile_slot_answer(latest_user: str) -> bool:
    normalized = str(latest_user or "").replace(" ", "").strip()
    if not normalized:
        return False
    if looks_like_structured_fact_update(latest_user):
        return True
    if "?" in normalized or "？" in normalized:
        return False
    if len(normalized) > 80:
        return False
    if any(keyword in normalized for keyword in PROFILE_SLOT_REQUEST_KEYWORDS):
        return False
    return any(keyword in normalized for keyword in PROFILE_SLOT_KEYWORDS)


def track_user_intent(bundle: SkillBundle, state: SessionState) -> IntentUpdate:
    ensure_runtime_state(state, bundle)
    latest_user = next((item.content for item in reversed(state.messages) if item.role == "user"), "").strip()
    if not latest_user:
        return IntentUpdate(satisfaction_status=str(state.status_flags.get("user_satisfied") or SATISFACTION_PENDING))
    current_stage = get_stage_contract(bundle.contract, state.stage)
    stage_kind = current_stage.kind if current_stage else "default"
    if stage_kind == "collection":
        return _build_scene_update(state, latest_user, SATISFACTION_PENDING, False)
    if any(keyword in latest_user for keyword in NEGATIVE_KEYWORDS):
        return _build_scene_update(state, latest_user, SATISFACTION_REJECTED, True)
    if any(keyword in latest_user for keyword in POSITIVE_KEYWORDS):
        return _build_scene_update(state, latest_user, SATISFACTION_CONFIRMED, False)
    return _build_scene_update(
        state,
        latest_user,
        str(state.status_flags.get("user_satisfied") or SATISFACTION_PENDING),
        bool(state.status_flags.get("needs_revise_conclusion", False)),
    )


def apply_intent_update(state: SessionState, update: IntentUpdate) -> None:
    state.status_flags["user_satisfied"] = update.satisfaction_status
    state.status_flags["needs_revise_conclusion"] = update.needs_revise_conclusion
    if update.satisfaction_status == SATISFACTION_CONFIRMED:
        state.status_flags["conclusion_confirmed"] = True
    elif update.satisfaction_status == SATISFACTION_REJECTED:
        state.status_flags["conclusion_confirmed"] = False
    if update.selected_scene:
        state.status_flags["pending_route_scene"] = update.selected_scene
    if update.resume_to_skill_id:
        state.status_flags["resume_to_skill_id"] = update.resume_to_skill_id


def _build_scene_update(
    state: SessionState,
    latest_user: str,
    satisfaction_status: str,
    needs_revise_conclusion: bool,
) -> IntentUpdate:
    if looks_like_structured_fact_update(latest_user):
        return IntentUpdate(
            satisfaction_status=satisfaction_status,
            needs_revise_conclusion=needs_revise_conclusion,
            selected_scene="",
            resume_to_skill_id="",
        )
    selected_scene = next(
        (scene for scene, keywords in SCENE_HINTS if any(keyword in latest_user for keyword in keywords)),
        "",
    )
    resume_to_skill_id = ""
    if any(keyword in latest_user for keyword in ("回到刚才", "继续刚才", "继续之前", "回到上一个")):
        resume_to_skill_id = str(state.status_flags.get("resume_to_skill_id") or "")
    return IntentUpdate(
        satisfaction_status=satisfaction_status,
        needs_revise_conclusion=needs_revise_conclusion,
        selected_scene=selected_scene,
        resume_to_skill_id=resume_to_skill_id,
    )
