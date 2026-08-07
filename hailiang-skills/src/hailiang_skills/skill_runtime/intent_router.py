from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import math
from pathlib import Path
import re
from typing import Any

from hailiang_skills.skill_runtime.embedding_cache import (
    EmbeddingCacheRecord,
    FileEmbeddingCache,
    build_text_hash,
    normalize_embedding_text,
)
from hailiang_skills.skill_runtime.intent_tracker import (
    looks_like_profile_slot_answer,
    looks_like_structured_fact_update,
)
from hailiang_skills.skill_runtime.models import ChatMessage, SessionState, SkillBundle
from hailiang_skills.skill_runtime.runtime_router import parse_json_object
from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID, GENERAL_CHAT_SKILL_ID, LEGACY_MAIN_PLANNER_SKILL_ID, canonical_skill_id


DEFAULT_UNCLEAR_INTENT_PATTERNS = (
    "不知道从哪里开始",
    "想规划一下",
    "有点迷茫",
    "帮我看看孩子情况",
    "不知道适合什么",
    "没有方向",
    "随便聊聊",
    "想咨询升学规划",
    "不知道该怎么规划",
    "不知道怎么规划",
    "拿不准",
    "有点迷糊",
)
SWITCH_INTENT_KEYWORDS = (
    "先看",
    "先看看",
    "换到",
    "切到",
    "进入",
    "直接看",
    "直接帮我",
    "我想做",
    "想做",
    "看看",
    "再看看",
    "不聊这个",
)
CROSS_SKILL_SUGGESTION_MIN_CONFIDENCE = 0.92
_NUMBERED_REPLY_PATTERN = re.compile(
    r"^(?:[1-9]\d*|[一二三四五六七八九十]+|第\s*(?:[1-9]\d*|[一二三四五六七八九十]+)\s*(?:个|项|条)?|选\s*(?:[1-9]\d*|[一二三四五六七八九十]+))$"
)
_SHORT_CONFIRMATION_REPLIES = {"好", "好的", "可以", "不可以", "是", "不是", "对", "不对", "继续", "确认"}
MAIN_PLANNER_REQUEST_KEYWORDS = (
    "回到顾问",
    "回到主入口",
    "整体规划",
    "重新规划",
    "从头规划",
    "帮我整体看看",
)
REQUEST_KEYWORDS = (
    "怎么",
    "如何",
    "能上",
    "能报",
    "有什么",
    "哪些",
    "看看",
    "想做",
    "规划",
    "适合",
    "路径",
    "选科",
    "提分",
    "兴趣",
    "培养",
    "职业",
    "专业",
    "学校",
    "大学",
    "院校",
    "冲稳保",
)
GRADE_KEYWORDS = ("小学", "初中", "高中", "初一", "初二", "初三", "高一", "高二", "高三", "七年级", "八年级", "九年级")
PROFILE_KEYWORDS = ("成绩", "中等", "一般", "上游", "下游", "喜欢", "特长", "美术", "音乐", "体育", "编程", "画画")
LONG_PROFILE_SIGNAL_KEYWORDS = (
    "孩子",
    "男孩",
    "女孩",
    "年级",
    "初一",
    "初二",
    "初三",
    "高一",
    "高二",
    "高三",
    "成绩",
    "分数",
    "排名",
    "中等",
    "上游",
    "下游",
    "兴趣",
    "喜欢",
    "特长",
    "美术",
    "音乐",
    "体育",
    "编程",
    "画画",
    "性格",
    "压力",
    "学习",
    "选科",
    "目标",
    "规划",
    "迷茫",
    "不知道",
)


def is_short_contextual_reply(message: str) -> bool:
    """Whether a reply must stay in the current Skill rather than route."""
    normalized = re.sub(r"[\s，。！？、,.!?：:；;\"“”'‘’（）()【】\[\]]+", "", str(message or ""))
    return bool(
        normalized
        and (_NUMBERED_REPLY_PATTERN.fullmatch(normalized) or normalized in _SHORT_CONFIRMATION_REPLIES)
    )


@dataclass(slots=True, frozen=True)
class IntentRouteDecision:
    route_mode: str = "main_planner"
    target_skill_id: str = CAREER_PLAN_SKILL_ID
    confidence: float = 0.0
    intent_clear: bool = False
    reason: str = ""
    matched_examples: tuple[str, ...] = ()
    requires_user_choice: bool = False
    scene_name: str = ""
    candidate_skills: tuple[dict[str, Any], ...] = ()
    debug_payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "route_mode": self.route_mode,
            "target_skill_id": self.target_skill_id,
            "confidence": self.confidence,
            "intent_clear": self.intent_clear,
            "reason": self.reason,
            "matched_examples": list(self.matched_examples),
            "requires_user_choice": self.requires_user_choice,
            "scene_name": self.scene_name,
            "candidate_skills": [dict(item) for item in self.candidate_skills],
            "debug_payload": self.debug_payload,
        }


@dataclass(slots=True, frozen=True)
class RouteExample:
    skill_id: str
    scene_name: str
    text: str
    source: str
    vector: tuple[float, ...] = ()


@dataclass(slots=True)
class _Candidate:
    skill_id: str
    scene_name: str
    score: float
    examples: list[str] = field(default_factory=list)
    reason: str = ""


class IntentRouter:
    """Config-driven skill intent router with optional embedding recall."""

    def __init__(
        self,
        *,
        bundles: dict[str, SkillBundle],
        main_skill_id: str = CAREER_PLAN_SKILL_ID,
        embedding_client=None,
        llm_client=None,
        embedding_cache_enabled: bool = True,
        embedding_cache_dir: str = "",
    ) -> None:
        self.bundles = bundles
        self.main_skill_id = canonical_skill_id(main_skill_id, default=CAREER_PLAN_SKILL_ID)
        self.main_bundle = bundles.get(main_skill_id)
        self.embedding_client = embedding_client
        self.llm_client = llm_client
        self.embedding_cache_enabled = embedding_cache_enabled
        self.embedding_cache_dir = embedding_cache_dir
        self._examples = self._build_examples()
        self._embedding_error = ""
        self._embedding_cache_error = ""
        self._example_embedding_batch_count = 0
        self._embedding_cache = self._build_embedding_cache()
        self._embedding_cache_stats = {
            "cache_enabled": bool(self._embedding_cache),
            "cache_root": str(self._embedding_cache.root_dir) if self._embedding_cache else "",
            "cache_hit_count": 0,
            "cache_miss_count": 0,
            "cache_stale_removed_count": 0,
            "embedding_request_batches": 0,
        }
        self._embed_examples_if_available()

    @property
    def embedding_error(self) -> str:
        return self._embedding_error

    def route(self, user_message: str, state: SessionState) -> IntentRouteDecision:
        self.last_llm_error: Exception | None = None
        normalized = _normalize(user_message)
        active_skill_id = canonical_skill_id(state.active_skill_id, default=self.main_skill_id)
        route_debug = self._base_route_debug(normalized, active_skill_id)

        def _mark_short_circuit(
            *,
            stage: str,
            rule: str,
            reason: str,
            details: dict[str, Any] | None = None,
        ) -> None:
            route_debug["routing_short_circuit"] = {
                "stage": stage,
                "rule": rule,
                "reason": reason,
                "details": details or {},
            }

        def _decision(**kwargs: Any) -> IntentRouteDecision:
            return IntentRouteDecision(debug_payload=dict(route_debug), **kwargs)

        if not normalized:
            _mark_short_circuit(
                stage="precheck",
                rule="empty_message",
                reason="空消息保持当前 skill。",
            )
            return _decision(
                route_mode="stay",
                target_skill_id=active_skill_id,
                reason="空消息保持当前 skill。",
            )

        if is_short_contextual_reply(user_message):
            _mark_short_circuit(
                stage="precheck",
                rule="short_contextual_reply",
                reason="编号、表单或追问短回答保持当前 skill，不参与跨场景路由。",
            )
            return _decision(
                route_mode="stay",
                target_skill_id=active_skill_id,
                confidence=1.0,
                intent_clear=False,
                reason="短回答保持当前场景。",
            )

        if _looks_like_resume_request(normalized) and state.status_flags.get("resume_to_skill_id"):
            _mark_short_circuit(
                stage="precheck",
                rule="resume_request",
                reason="用户要求回到之前的 skill。",
                details={"resume_to_skill_id": str(state.status_flags.get("resume_to_skill_id") or "")},
            )
            return _decision(
                route_mode="stay",
                target_skill_id=active_skill_id,
                confidence=1.0,
                intent_clear=False,
                reason="文本请求不自动恢复其他 skill。",
            )

        if looks_like_structured_fact_update(user_message):
            _mark_short_circuit(
                stage="precheck",
                rule="structured_fact_update",
                reason="结构化补充 facts，保持当前场景。",
            )
            return _decision(
                route_mode="stay",
                target_skill_id=active_skill_id,
                confidence=1.0,
                intent_clear=False,
                reason="结构化补充 facts，保持当前场景。",
            )

        if (
            active_skill_id == self.main_skill_id
            and (
                state.status_flags.get("consultative_lock")
                or state.status_flags.get("scene_lock") == "consultative"
                or not bool(state.status_flags.get("collection_complete", False))
            )
            and looks_like_profile_slot_answer(user_message)
        ):
            _mark_short_circuit(
                stage="precheck",
                rule="consultative_profile_slot_answer",
                reason="main_planner 问诊收集阶段收到画像补充，保持当前顾问问诊，不触发子 skill 跳转。",
                details={
                    "consultative_lock": bool(state.status_flags.get("consultative_lock")),
                    "scene_lock": str(state.status_flags.get("scene_lock") or ""),
                    "collection_complete": bool(state.status_flags.get("collection_complete", False)),
                },
            )
            return _decision(
                route_mode="main_planner",
                target_skill_id=self.main_skill_id,
                confidence=0.9,
                intent_clear=False,
                reason="main_planner 问诊收集阶段收到画像补充，保持当前顾问问诊，不触发子 skill 跳转。",
            )

        long_profile_details = _long_profile_match_details(normalized, self._intent_router_config())
        if long_profile_details["matched"]:
            _mark_short_circuit(
                stage="precheck",
                rule="long_profile_message",
                reason="用户一次性提供了较长、多维画像信息，先进入 main_planner 做画像简历和场景推荐。",
                details=long_profile_details,
            )
            if active_skill_id != self.main_skill_id:
                return _decision(
                    route_mode="stay",
                    target_skill_id=active_skill_id,
                    confidence=0.88,
                    intent_clear=False,
                    reason="当前专项 Skill 保持场景，不因画像补充自动回到主顾问。",
                )
            return _decision(
                route_mode="main_planner",
                target_skill_id=self.main_skill_id,
                confidence=0.88,
                intent_clear=False,
                reason="用户一次性提供了较长、多维画像信息，先在主顾问中整理。",
            )

        if active_skill_id != self.main_skill_id and _looks_like_main_planner_request(normalized):
            _mark_short_circuit(
                stage="precheck",
                rule="explicit_main_planner_request",
                reason="用户明确要求回到整体规划顾问。",
            )
            return _decision(
                route_mode="stay",
                target_skill_id=active_skill_id,
                confidence=0.95,
                intent_clear=False,
                reason="文本请求不自动切换到主顾问，请通过退出 Skill 操作确认。",
            )

        if _matches_unclear_intent(normalized, self._unclear_patterns()) or _looks_like_profile_only(normalized):
            if active_skill_id == self.main_skill_id:
                _mark_short_circuit(
                    stage="precheck",
                    rule="unclear_intent_or_profile_only",
                    reason="用户表达的是整体规划或零散画像信息，进入顾问问诊。",
                )
                return _decision(
                    route_mode="main_planner",
                    target_skill_id=self.main_skill_id,
                    confidence=0.86,
                    intent_clear=False,
                    reason="用户表达的是整体规划或零散画像信息，进入顾问问诊。",
                )
            if not _looks_like_switch_request(normalized):
                _mark_short_circuit(
                    stage="precheck",
                    rule="child_task_lock_unclear_intent",
                    reason="child skill 场景锁生效，泛化规划表达不自动跳转。",
                )
                return _decision(
                    route_mode="stay",
                    target_skill_id=active_skill_id,
                    confidence=0.78,
                    intent_clear=False,
                    reason="child skill 场景锁生效，泛化规划表达不自动跳转。",
                )

        candidate, candidate_debug = self._best_candidate(normalized)
        route_debug.update(candidate_debug)
        candidate_skills = self._general_chat_candidate_skills(candidate_debug)
        if candidate is None and self._llm_enabled():
            llm_decision = self._route_with_llm(normalized, state)
            if llm_decision:
                route_debug["llm_fallback_used"] = True
                return llm_decision
        if candidate is None:
            if active_skill_id == self.main_skill_id:
                return _decision(
                    route_mode="main_planner",
                    target_skill_id=self.main_skill_id,
                    confidence=0.5,
                    intent_clear=False,
                    reason="没有明确命中子场景，进入 main_planner 兜底。",
                    candidate_skills=candidate_skills,
                )
            return _decision(
                route_mode="stay",
                target_skill_id=active_skill_id,
                confidence=0.72,
                intent_clear=False,
                reason="task lock 生效，没有明确跨场景请求。",
                candidate_skills=candidate_skills,
            )

        if candidate.skill_id == self.main_skill_id:
            return _decision(
                route_mode="clarify" if active_skill_id == self.main_skill_id else "stay",
                target_skill_id=active_skill_id if active_skill_id != self.main_skill_id else self.main_skill_id,
                confidence=candidate.score,
                intent_clear=False,
                reason=candidate.reason,
                matched_examples=tuple(candidate.examples[:3]),
                requires_user_choice=True,
                candidate_skills=candidate_skills,
            )

        if active_skill_id != self.main_skill_id:
            if candidate.skill_id == active_skill_id:
                return _decision(
                    route_mode="stay",
                    target_skill_id=active_skill_id,
                    confidence=candidate.score,
                    intent_clear=True,
                    reason=f"用户仍在当前场景内：{candidate.reason}",
                    matched_examples=tuple(candidate.examples[:3]),
                    scene_name=candidate.scene_name,
                    candidate_skills=candidate_skills,
                )
            if candidate.score < CROSS_SKILL_SUGGESTION_MIN_CONFIDENCE:
                return _decision(
                    route_mode="stay",
                    target_skill_id=active_skill_id,
                    confidence=candidate.score,
                    intent_clear=False,
                    reason="task lock 生效，未达到跨场景建议阈值。",
                    matched_examples=tuple(candidate.examples[:3]),
                    scene_name=candidate.scene_name,
                    candidate_skills=candidate_skills,
                )

        return _decision(
            route_mode="recommend_switch" if candidate.skill_id != active_skill_id else "stay",
            target_skill_id=candidate.skill_id,
            confidence=candidate.score,
            intent_clear=False,
            reason=candidate.reason,
            matched_examples=tuple(candidate.examples[:3]),
            requires_user_choice=True,
            scene_name=candidate.scene_name,
            candidate_skills=candidate_skills,
        )

    def _general_chat_candidate_skills(
        self,
        candidate_debug: dict[str, Any],
    ) -> tuple[dict[str, Any], ...]:
        """Return configured ranked child-skill choices for general_chat."""
        config = self._intent_router_config()
        raw_scores = candidate_debug.get("per_skill_scores")
        if not isinstance(raw_scores, list):
            return ()
        candidates: list[dict[str, Any]] = []
        for item in raw_scores:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("skill_id") or "").strip()
            if not skill_id or skill_id in {GENERAL_CHAT_SKILL_ID}:
                continue
            try:
                confidence = float(item.get("final_score", 0) or 0)
            except (TypeError, ValueError):
                continue
            if confidence < config.general_chat_choice_threshold:
                continue
            candidates.append(
                {
                    "target_skill_id": skill_id,
                    "scene_name": str(item.get("scene_name") or "").strip(),
                    "confidence": confidence,
                    "reason": str(item.get("best_reason") or item.get("best_example") or "").strip(),
                }
            )
        candidates.sort(key=lambda item: float(item["confidence"]), reverse=True)
        return tuple(candidates[: config.general_chat_choice_max_candidates])

    def _build_examples(self) -> tuple[RouteExample, ...]:
        route_scenes_by_skill: dict[str, list[str]] = {}
        if self.main_bundle:
            for route in self.main_bundle.contract.routes:
                route_scenes_by_skill.setdefault(route.target_skill_id, []).append(route.scene)

        examples: list[RouteExample] = []
        for skill_id, bundle in sorted(self.bundles.items()):
            if skill_id in {LEGACY_MAIN_PLANNER_SKILL_ID, GENERAL_CHAT_SKILL_ID}:
                continue
            routing = bundle.runtime_metadata.routing
            scene_name = (
                routing.scene_name
                or str(bundle.contract.metadata.get("scene_name") or "")
                or (bundle.runtime_metadata.accepts_scenes[0] if bundle.runtime_metadata.accepts_scenes else "")
                or (bundle.contract.accepts_scenes[0] if bundle.contract.accepts_scenes else "")
                or bundle.runtime_metadata.name
                or skill_id
            )
            for text, source in self._routing_texts_for_bundle(skill_id, bundle, route_scenes_by_skill):
                examples.append(RouteExample(skill_id=skill_id, scene_name=scene_name, text=text, source=source))
        return tuple(examples)

    def _routing_texts_for_bundle(
        self,
        skill_id: str,
        bundle: SkillBundle,
        route_scenes_by_skill: dict[str, list[str]],
    ) -> tuple[tuple[str, str], ...]:
        texts: list[tuple[str, str]] = []
        routing = bundle.runtime_metadata.routing
        for item in routing.routing_examples:
            texts.append((item, "routing_examples"))
        for item in bundle.runtime_metadata.triggers:
            texts.append((item, "triggers"))
        for item in bundle.runtime_metadata.accepts_scenes:
            texts.append((item, "accepts_scenes"))
        for item in bundle.contract.accepts_scenes:
            texts.append((item, "contract.accepts_scenes"))
        for item in route_scenes_by_skill.get(skill_id, []):
            texts.append((item, "main_planner.routes"))
        if routing.scene_name:
            texts.append((routing.scene_name, "routing.scene_name"))

        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for text, source in texts:
            normalized = str(text).strip()
            if normalized and normalized not in seen:
                unique.append((normalized, source))
                seen.add(normalized)
        return tuple(unique)

    def _embed_examples_if_available(self) -> None:
        if not self.embedding_client or not self._embedding_enabled() or not self._examples:
            return
        cache_hits = 0
        cache_misses = 0
        stale_removed_count = 0
        current_records: dict[str, EmbeddingCacheRecord] = {}
        snapshot_records: dict[str, EmbeddingCacheRecord] = {}
        embedded: list[RouteExample] = []
        pending: list[RouteExample] = []
        if self._embedding_cache:
            snapshot_records = self._embedding_cache.load().records
        for item in self._examples:
            cached = self._cached_record_for_example(snapshot_records, item)
            if cached:
                cache_hits += 1
                current_records[cached.text_hash] = self._build_cache_record(item, cached.vector)
                embedded.append(
                    RouteExample(
                        skill_id=item.skill_id,
                        scene_name=item.scene_name,
                        text=item.text,
                        source=item.source,
                        vector=cached.vector,
                    )
                )
                continue
            cache_misses += 1
            pending.append(item)
        try:
            if pending:
                vectors = self.embedding_client.embed([item.text for item in pending])
                self._example_embedding_batch_count = int(getattr(self.embedding_client, "last_batch_count", 0) or 0)
                if len(vectors) != len(pending):
                    raise ValueError("embedding 返回向量数量与待处理样本数量不一致")
                for item, vector in zip(pending, vectors, strict=True):
                    normalized_vector = tuple(float(value) for value in vector)
                    embedded.append(
                        RouteExample(
                            skill_id=item.skill_id,
                            scene_name=item.scene_name,
                            text=item.text,
                            source=item.source,
                            vector=normalized_vector,
                        )
                    )
                    if self._embedding_cache:
                        record = self._build_cache_record(item, normalized_vector)
                        current_records[record.text_hash] = record
        except Exception as exc:  # noqa: BLE001
            self._embedding_error = str(exc)
            self._embedding_cache_stats = {
                **self._embedding_cache_stats,
                "cache_hit_count": cache_hits,
                "cache_miss_count": cache_misses,
                "cache_stale_removed_count": 0,
                "embedding_request_batches": self._example_embedding_batch_count,
            }
            return
        if self._embedding_cache:
            stale_removed_count = len(set(snapshot_records) - set(current_records))
            try:
                self._embedding_cache.write(
                    records=current_records,
                    model=self.embedding_client.model,
                    base_url=self.embedding_client.base_url,
                    example_count=len(self._examples),
                    stale_removed_count=stale_removed_count,
                )
            except OSError as exc:
                self._embedding_cache_error = str(exc)
        if embedded:
            embedded.sort(key=lambda item: (item.skill_id, item.text, item.source))
            original_order = {(item.skill_id, item.scene_name, item.text, item.source): index for index, item in enumerate(self._examples)}
            embedded.sort(key=lambda item: original_order.get((item.skill_id, item.scene_name, item.text, item.source), 0))
            self._examples = tuple(embedded)
        self._embedding_cache_stats = {
            **self._embedding_cache_stats,
            "cache_hit_count": cache_hits,
            "cache_miss_count": cache_misses,
            "cache_stale_removed_count": stale_removed_count,
            "embedding_request_batches": self._example_embedding_batch_count,
        }

    def _best_candidate(self, normalized_message: str) -> tuple[_Candidate | None, dict[str, Any]]:
        candidates: dict[str, _Candidate] = {}
        per_skill_debug: dict[str, dict[str, Any]] = {}
        for example in self._examples:
            per_skill_debug.setdefault(
                example.skill_id,
                {
                    "skill_id": example.skill_id,
                    "scene_name": example.scene_name,
                    "best_text_score": 0.0,
                    "best_embedding_score": 0.0,
                    "final_score": 0.0,
                    "best_example": "",
                    "best_source": "",
                    "best_reason": "",
                    "top_examples": [],
                },
            )
        query_vector = self._embed_query(normalized_message)
        for example in self._examples:
            text_score = _text_match_score(normalized_message, _normalize(example.text))
            embedding_score = _cosine(query_vector, example.vector) if query_vector and example.vector else 0.0
            score = max(text_score, embedding_score)
            detail = per_skill_debug.setdefault(
                example.skill_id,
                {
                    "skill_id": example.skill_id,
                    "scene_name": example.scene_name,
                    "best_text_score": 0.0,
                    "best_embedding_score": 0.0,
                    "final_score": 0.0,
                    "best_example": "",
                    "best_source": "",
                    "best_reason": "",
                    "top_examples": [],
                },
            )
            if text_score > detail["best_text_score"]:
                detail["best_text_score"] = round(text_score, 4)
            if embedding_score > detail["best_embedding_score"]:
                detail["best_embedding_score"] = round(embedding_score, 4)
            if score > 0:
                detail["top_examples"].append(
                    {
                        "text": example.text,
                        "source": example.source,
                        "text_score": round(text_score, 4),
                        "embedding_score": round(embedding_score, 4),
                        "final_score": round(score, 4),
                    }
                )
            if score > detail["final_score"]:
                detail["final_score"] = round(score, 4)
                detail["best_example"] = example.text
                detail["best_source"] = example.source
                detail["best_reason"] = (
                    "embedding"
                    if embedding_score >= text_score and embedding_score > 0
                    else ("text" if text_score > 0 else "")
                )
            if score <= 0:
                continue
            current = candidates.get(example.skill_id)
            if current is None or score > current.score:
                candidates[example.skill_id] = _Candidate(
                    skill_id=example.skill_id,
                    scene_name=example.scene_name,
                    score=score,
                    examples=[example.text],
                    reason=(
                        "routing_examples 语义命中。"
                        if embedding_score >= text_score and embedding_score > 0
                        else "routing metadata 文本命中。"
                    ),
                )
            elif abs(score - current.score) < 0.04 and example.text not in current.examples:
                current.examples.append(example.text)

        per_skill_scores = []
        for detail in per_skill_debug.values():
            top_examples = sorted(
                detail["top_examples"],
                key=lambda item: item["final_score"],
                reverse=True,
            )[:3]
            per_skill_scores.append(
                {
                    "skill_id": detail["skill_id"],
                    "scene_name": detail["scene_name"],
                    "best_text_score": detail["best_text_score"],
                    "best_embedding_score": detail["best_embedding_score"],
                    "final_score": detail["final_score"],
                    "best_example": detail["best_example"],
                    "best_source": detail["best_source"],
                    "best_reason": detail["best_reason"],
                    "top_examples": top_examples,
                }
            )
        per_skill_scores.sort(key=lambda item: (item["final_score"], item["best_embedding_score"]), reverse=True)
        debug_payload = {
            "query_embedding_used": bool(query_vector),
            "query_embedding_dimensions": len(query_vector),
            "direct_threshold": self._intent_router_config().direct_threshold,
            "ambiguity_margin": self._intent_router_config().ambiguity_margin,
            "per_skill_scores": per_skill_scores,
        }
        if not candidates:
            return None, debug_payload
        ranked = sorted(candidates.values(), key=lambda item: item.score, reverse=True)
        best = ranked[0]
        config = self._intent_router_config()
        if best.score < config.direct_threshold:
            return None, debug_payload
        if len(ranked) > 1 and best.score - ranked[1].score < config.ambiguity_margin:
            return (
                _Candidate(
                    skill_id=self.main_skill_id,
                    scene_name="",
                    score=best.score,
                    examples=best.examples,
                    reason="多个子场景命中接近，交给 main_planner 澄清。",
                ),
                debug_payload,
            )
        return best, debug_payload

    def _embed_query(self, normalized_message: str) -> tuple[float, ...]:
        if not self.embedding_client or not self._embedding_enabled() or not any(item.vector for item in self._examples):
            return ()
        try:
            vectors = self.embedding_client.embed(normalized_message)
        except Exception as exc:  # noqa: BLE001
            self._embedding_error = str(exc)
            return ()
        if not vectors:
            return ()
        return tuple(float(value) for value in vectors[0])

    def _llm_enabled(self) -> bool:
        return bool(self.llm_client and self._intent_router_config().enable_llm_fallback)

    def _route_with_llm(self, normalized_message: str, state: SessionState) -> IntentRouteDecision | None:
        if not self.llm_client:
            return None
        examples_by_skill: dict[str, list[str]] = {}
        for example in self._examples:
            examples_by_skill.setdefault(example.skill_id, []).append(example.text)
        lines = []
        for skill_id, examples in sorted(examples_by_skill.items()):
            lines.append(f"- {skill_id}: {'；'.join(examples[:6])}")
        prompt = (
            "你是一个升学规划系统的轻量意图路由器。只输出 JSON："
            '{"route_mode":"direct|main_planner|clarify|stay|switch",'
            '"target_skill_id":"...","confidence":0.0,"intent_clear":true|false,'
            '"reason":"...","matched_examples":["..."],"requires_user_choice":false}\n'
            "如果用户只是给零散画像、表达迷茫或不知道从哪里开始，target_skill_id 必须是 career_plan_entity。\n"
            "不要输出 missing facts。\n\n"
            f"# Skills\n{chr(10).join(lines)}\n"
        )
        try:
            messages = [
                ChatMessage(role="system", content=prompt),
                ChatMessage(role="user", content=normalized_message),
            ]
            complete_kwargs: dict[str, Any] = {}
            try:
                if "request_purpose" in inspect.signature(self.llm_client.complete).parameters:
                    complete_kwargs["request_purpose"] = "intent_router_fallback"
            except (TypeError, ValueError):
                pass
            raw = self.llm_client.complete(messages, **complete_kwargs)
        except Exception as exc:  # noqa: BLE001
            self.last_llm_error = exc
            return None
        payload = parse_json_object(raw)
        if not payload:
            return None
        target = canonical_skill_id(payload.get("target_skill_id"))
        if target and target not in self.bundles:
            target = self.main_skill_id if self.main_skill_id in self.bundles else canonical_skill_id(state.active_skill_id)
        if not target or target not in self.bundles:
            target = GENERAL_CHAT_SKILL_ID
        return IntentRouteDecision(
            route_mode=str(payload.get("route_mode") or "main_planner").strip() or "main_planner",
            target_skill_id=canonical_skill_id(target, default=self.main_skill_id),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            intent_clear=bool(payload.get("intent_clear", False)),
            reason=str(payload.get("reason") or "LLM intent router fallback.").strip(),
            matched_examples=tuple(
                str(item).strip()
                for item in payload.get("matched_examples", [])
                if str(item).strip()
            )
            if isinstance(payload.get("matched_examples"), list)
            else (),
            requires_user_choice=bool(payload.get("requires_user_choice", False)),
        )

    def _unclear_patterns(self) -> tuple[str, ...]:
        configured = self._intent_router_config().unclear_intent_patterns
        return configured or DEFAULT_UNCLEAR_INTENT_PATTERNS

    def _embedding_enabled(self) -> bool:
        return self._intent_router_config().enable_embedding

    def _intent_router_config(self):
        if self.main_bundle is None:
            from hailiang_skills.skill_runtime.models import IntentRouterConfig

            return IntentRouterConfig()
        return self.main_bundle.runtime_metadata.planner.intent_router

    def _build_embedding_cache(self) -> FileEmbeddingCache | None:
        if not self.embedding_cache_enabled or not self.embedding_client or not self._embedding_enabled():
            return None
        root_dir = self.embedding_cache_dir or str(Path.cwd() / ".skill_runtime_cache" / "intent_router_embeddings")
        return FileEmbeddingCache(root_dir)

    def _cached_record_for_example(
        self,
        records: dict[str, EmbeddingCacheRecord],
        example: RouteExample,
    ) -> EmbeddingCacheRecord | None:
        if not records or not self.embedding_client:
            return None
        text_hash = build_text_hash(
            skill_id=example.skill_id,
            source=example.source,
            text=normalize_embedding_text(example.text),
            model=self.embedding_client.model,
            base_url=self.embedding_client.base_url,
        )
        return records.get(text_hash)

    def _build_cache_record(self, example: RouteExample, vector: tuple[float, ...]) -> EmbeddingCacheRecord:
        if not self.embedding_client:
            raise RuntimeError("embedding client unavailable")
        text_hash = build_text_hash(
            skill_id=example.skill_id,
            source=example.source,
            text=normalize_embedding_text(example.text),
            model=self.embedding_client.model,
            base_url=self.embedding_client.base_url,
        )
        return EmbeddingCacheRecord(
            text_hash=text_hash,
            skill_id=example.skill_id,
            scene_name=example.scene_name,
            source=example.source,
            text=example.text,
            vector=vector,
            model=self.embedding_client.model,
            base_url=self.embedding_client.base_url,
            updated_at=_router_now_iso(),
        )

    def _base_route_debug(self, normalized_message: str, active_skill_id: str) -> dict[str, Any]:
        return {
            "normalized_message": normalized_message,
            "active_skill_id": active_skill_id,
            "embedding": {
                "enabled": self._embedding_enabled(),
                "client_available": bool(self.embedding_client),
                "example_count": len(self._examples),
                "embedded_example_count": sum(1 for item in self._examples if item.vector),
                "embedding_error": self._embedding_error,
                "cache_error": self._embedding_cache_error,
                **self._embedding_cache_stats,
            },
            "llm_fallback_enabled": self._llm_enabled(),
            "llm_fallback_used": False,
        }


def _router_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").strip().lower())


def _matches_unclear_intent(normalized_message: str, patterns: tuple[str, ...]) -> bool:
    return any(_normalize(pattern) in normalized_message for pattern in patterns)


def _looks_like_profile_only(normalized_message: str) -> bool:
    has_profile = any(keyword in normalized_message for keyword in GRADE_KEYWORDS) and any(
        keyword in normalized_message for keyword in PROFILE_KEYWORDS
    )
    if not has_profile:
        return False
    return not any(keyword in normalized_message for keyword in REQUEST_KEYWORDS)


def _long_profile_match_details(normalized_message: str, config) -> dict[str, Any]:
    matched_keywords = [keyword for keyword in LONG_PROFILE_SIGNAL_KEYWORDS if keyword in normalized_message]
    punctuation_signals = sum(normalized_message.count(item) for item in ("，", "。", "；", ",", ";"))
    char_count = len(normalized_message)
    return {
        "matched": (
            char_count >= config.long_profile_message_min_chars
            and len(matched_keywords) >= config.long_profile_signal_threshold
            and punctuation_signals >= 2
        ),
        "char_count": char_count,
        "min_chars": config.long_profile_message_min_chars,
        "signal_count": len(matched_keywords),
        "signal_threshold": config.long_profile_signal_threshold,
        "matched_keywords": matched_keywords,
        "punctuation_count": punctuation_signals,
        "punctuation_threshold": 2,
    }


def _looks_like_switch_request(normalized_message: str) -> bool:
    return any(keyword in normalized_message for keyword in SWITCH_INTENT_KEYWORDS)


def _looks_like_main_planner_request(normalized_message: str) -> bool:
    return any(keyword in normalized_message for keyword in MAIN_PLANNER_REQUEST_KEYWORDS)


def _looks_like_resume_request(normalized_message: str) -> bool:
    return any(keyword in normalized_message for keyword in ("回到刚才", "继续刚才", "继续之前", "回到上一个"))


def _text_match_score(normalized_message: str, normalized_example: str) -> float:
    if not normalized_message or not normalized_example:
        return 0.0
    if normalized_example in normalized_message or normalized_message in normalized_example:
        return 0.95 if len(normalized_example) >= 4 else 0.82
    overlap = _char_bigram_jaccard(normalized_message, normalized_example)
    if overlap >= 0.62:
        return min(0.9, 0.55 + overlap * 0.35)
    return 0.0


def _char_bigram_jaccard(left: str, right: str) -> float:
    left_items = _char_bigrams(left)
    right_items = _char_bigrams(right)
    if not left_items or not right_items:
        return 0.0
    return len(left_items & right_items) / len(left_items | right_items)


def _char_bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)
