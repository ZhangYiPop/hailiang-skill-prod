from __future__ import annotations

from datetime import datetime, timezone
import inspect
import json
import re
import time
from typing import Any

from hailiang_skills.core.fact_service import serialize_known_facts
from hailiang_skills.core.logging import make_event
from hailiang_skills.core.skill_display import build_skill_display
from hailiang_skills.core.skill_ids import (
    CAREER_PLAN_SKILL_ID,
    GENERAL_CHAT_SKILL_ID,
    LEGACY_MAIN_PLANNER_SKILL_ID,
    canonical_skill_id,
)
from hailiang_skills.core.message_interactions import ensure_message_interactions
from hailiang_skills.skill_runtime.skill_contract import get_stage_contract
from hailiang_skills.skill_runtime.models import ChatMessage


MIN_ROUTE_SUGGESTION_CONFIDENCE = 0.72
MIN_ROUTE_SUGGESTION_CONFIDENCE_WITHOUT_CONTEXT = 0.72
MIN_GENERAL_CHAT_CARD_CONFIDENCE = 0.72
MIN_SPECIALIST_SWITCH_CARD_CONFIDENCE = 0.92
ROUTE_SUGGESTION_SOURCE_LLM = "llm_reply_analysis"
ROUTE_SUGGESTION_SOURCE_FALLBACK = "strong_format_fallback"


def build_finalizing_started_payload(context, *, runtime_registry=None) -> dict[str, Any]:
    skill_display = build_skill_display(context, runtime_registry=runtime_registry)
    return {
        "type": "finalizing_started",
        "session_id": context.session_id,
        "turn_id": str((context.session_meta or {}).get("active_turn_id") or ""),
        "active_skill": skill_display["skill_id"],
        "active_skill_label": skill_display["active_skill_label"],
        "agent_label": skill_display["agent_label"],
        "skill_brief": skill_display.get("brief", ""),
        "skill_info": skill_display.get("info", ""),
        "scene_name": skill_display["scene_name"],
        "stage": "finalizing",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def build_skill_invitation(
    context,
    *,
    assistant_message: str,
    runtime_registry=None,
    card_threshold: float | None = None,
) -> str:
    """Return a short, user-facing invitation for high-confidence choices.

    This is deliberately deterministic: the router has already identified a
    candidate, while the user still has to click the resulting card before a
    transition can occur.
    """
    active_skill = build_skill_display(context, runtime_registry=runtime_registry).get("skill_id")
    if active_skill != GENERAL_CHAT_SKILL_ID:
        return ""
    candidates = _limit_general_chat_route_suggestions(
        _build_intent_route_suggestions(
            context,
            active_skill=active_skill,
            handoff_notes="",
            runtime_registry=runtime_registry,
        ),
        runtime_registry=runtime_registry,
        threshold=card_threshold,
    )
    if not candidates:
        return ""
    visible_names: list[str] = []
    for item in candidates:
        name = str(item.get("agent_label") or item.get("skill_name") or item.get("scene_name") or "").strip()
        if name and name not in visible_names:
            visible_names.append(name)
    if not visible_names:
        return ""
    if any(name in assistant_message for name in visible_names):
        return ""
    if len(visible_names) == 1:
        return f"\n\n如果你希望继续深入，我可以带你进入「{visible_names[0]}」。你可以点击下方按钮选择。"
    joined = "、".join(f"「{name}」" for name in visible_names)
    return f"\n\n如果你希望继续深入，可以从 {joined} 中选择一个方向，点击下方按钮即可进入。"


def build_finalized_payload(
    context,
    *,
    assistant_message: str,
    facts_delta: list[dict[str, Any]] | None = None,
    runtime_registry=None,
    route_suggestion_client=None,
    monitor_route_suggestions_every_turn: bool = False,
    general_chat_card_threshold: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    skill_display = build_skill_display(context, runtime_registry=runtime_registry)
    active_skill = skill_display["skill_id"] or CAREER_PLAN_SKILL_ID
    runtime_state = context.skill_states.get("skill_runtime", {})
    status_flags = runtime_state.get("status_flags", {}) if isinstance(runtime_state, dict) else {}
    skill_facts_map = runtime_state.get("skill_facts", {}) if isinstance(runtime_state, dict) else {}
    current_skill_facts = (
        dict(skill_facts_map.get(active_skill) or {})
        if isinstance(skill_facts_map, dict) and isinstance(skill_facts_map.get(active_skill), dict)
        else {}
    )
    conclusion_summary = _build_conclusion_summary(assistant_message)
    is_final_summary = _is_final_summary_turn(
        context,
        active_skill=active_skill,
        assistant_message=assistant_message,
        runtime_registry=runtime_registry,
    )
    facts_snapshot = serialize_known_facts(context.known_facts)
    handoff_notes = _build_handoff_notes(
        agent_label=skill_display["agent_label"],
        conclusion_summary=conclusion_summary,
        facts_delta=facts_delta or [],
    )
    context_compression = {
        "conversation_summary": conclusion_summary,
        "facts_snapshot": facts_snapshot,
        "facts_delta": facts_delta or [],
        "skill_facts": current_skill_facts,
        "handoff_notes": handoff_notes,
        "source_skill_id": active_skill,
    }
    is_specialist = active_skill not in {GENERAL_CHAT_SKILL_ID, CAREER_PLAN_SKILL_ID}
    is_skill_transition_entry_turn = bool(
        (context.session_meta or {}).get("include_internal_transition_turn")
    ) and active_skill != GENERAL_CHAT_SKILL_ID
    if is_skill_transition_entry_turn:
        # The user has just confirmed this Skill by clicking its card or the
        # toolbar. Its entry reply must establish that consultation, rather
        # than immediately offering the alternatives the user just declined.
        route_suggestions = []
        route_analysis_event = {
            "active_skill": active_skill,
            "suggestion_count": 0,
            "suggestion_source": "",
            "llm_available": route_suggestion_client is not None,
            "fallback_used": False,
            "monitor_every_turn": monitor_route_suggestions_every_turn,
            "skipped_reason": "skill_transition_entry_turn",
            "analysis_reason": "用户刚刚确认进入当前 Skill，首条回复不生成其它 Skill 建议。",
            "error": "",
            "duration_ms": 0,
        }
    else:
        route_suggestions, route_analysis_event = build_route_suggestions(
            context,
            active_skill=active_skill,
            assistant_message=assistant_message,
            handoff_notes=handoff_notes,
            runtime_registry=runtime_registry,
            # Specialist transitions are already validated by Intent Router. Avoid
            # making an optional second model call after the main reply succeeds.
            route_suggestion_client=None if is_specialist else route_suggestion_client,
            allow_llm_without_route_context=(
                not is_specialist
                and (
                    is_final_summary
                    or monitor_route_suggestions_every_turn
                    or active_skill in {GENERAL_CHAT_SKILL_ID, CAREER_PLAN_SKILL_ID}
                )
            ),
            monitor_every_turn=monitor_route_suggestions_every_turn,
            general_chat_card_threshold=general_chat_card_threshold,
        )
    _persist_finalization_to_runtime_state(
        context,
        active_skill=active_skill,
        conclusion_summary=conclusion_summary,
        context_compression=context_compression,
        route_suggestions=route_suggestions,
        is_final_summary=is_final_summary,
    )
    payload = {
        "type": "finalized",
        "session_id": context.session_id,
        "turn_id": str((context.session_meta or {}).get("active_turn_id") or ""),
        "active_skill": active_skill,
        "active_skill_label": skill_display["active_skill_label"],
        "agent_label": skill_display["agent_label"],
        "scene_name": skill_display["scene_name"],
        "stage": "finalized",
        "is_final_summary": is_final_summary,
        "conclusion_summary": conclusion_summary,
        "context_compression": context_compression,
        "route_suggestions": route_suggestions,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    events = [
        make_event(
            "context_compression_created",
            {
                "active_skill": active_skill,
                "source_skill_id": active_skill,
                "conversation_summary": conclusion_summary,
                "facts_delta_count": len(facts_delta or []),
                "facts_source_of_truth": "effective_facts",
                "context_compression": context_compression,
                "is_final_summary": is_final_summary,
            },
        )
    ]
    events.append(make_event("route_suggestions_analyzed", route_analysis_event))
    if is_final_summary:
        events.append(make_event("skill_finalized", payload))
    if is_final_summary or route_suggestions:
        suggestion_source = (
            str(route_suggestions[0].get("suggestion_source") or "")
            if route_suggestions
            else ("final_summary" if is_final_summary else "")
        )
        events.append(
            make_event(
                "route_suggestions_created",
                {
                    "active_skill": active_skill,
                    "suggestion_count": len(route_suggestions),
                    "route_suggestions": route_suggestions,
                    "suggestion_source": suggestion_source,
                    "is_final_summary": is_final_summary,
                },
            )
        )
    previous_compression = status_flags.get("context_compression") if isinstance(status_flags, dict) else None
    if isinstance(previous_compression, dict):
        previous_facts = previous_compression.get("facts_snapshot")
        if isinstance(previous_facts, dict) and previous_facts != facts_snapshot:
            events.append(
                make_event(
                    "context_compression_fact_conflict",
                    {
                        "active_skill": active_skill,
                        "resolution": "current_effective_facts_wins",
                    },
                )
            )
    return payload, events


def attach_finalization_metadata(context, payload: dict[str, Any]) -> None:
    for message in reversed(context.messages):
        if message.get("role") != "assistant":
            continue
        metadata = dict(message.get("metadata") or {})
        metadata.update(
            {
                "conclusion_summary": payload.get("conclusion_summary"),
                "context_compression": payload.get("context_compression"),
                "route_suggestions": payload.get("route_suggestions") or [],
                "selected_route_suggestion": "",
            }
        )
        message["metadata"] = metadata
        message["conclusion_summary"] = payload.get("conclusion_summary")
        message["context_compression"] = payload.get("context_compression")
        message["route_suggestions"] = payload.get("route_suggestions") or []
        message.setdefault("selected_route_suggestion", "")
        ensure_message_interactions(message)
        return


def build_route_suggestions_exposed_event(
    context,
    payload: dict[str, Any],
    finalized_events: list[dict[str, Any]],
    *,
    run_id: str = "",
) -> dict[str, Any] | None:
    """Create one analytics exposure event for the final assistant reply."""
    suggestions = payload.get("route_suggestions") if isinstance(payload.get("route_suggestions"), list) else []
    skill_ids = sorted(
        {
            canonical_skill_id(item.get("target_skill_id"))
            for item in suggestions
            if isinstance(item, dict) and canonical_skill_id(item.get("target_skill_id"))
        }
    )
    if not skill_ids:
        return None
    message_id = ""
    for message in reversed(context.messages):
        if message.get("role") == "assistant":
            message_id = str(message.get("message_id") or "")
            break
    legacy_event_ids = [
        str(event.get("event_id") or "")
        for event in finalized_events
        if event.get("event_type") == "route_suggestions_created"
    ]
    return make_event(
        "skill_route_suggestions_exposed",
        {
            "skill_ids": skill_ids,
            "session_id": str(context.session_id),
            "message_id": message_id,
            "run_id": run_id,
            "dedupe_key": f"route_suggestions_exposed:{run_id}:{message_id}:{','.join(skill_ids)}",
            "legacy_event_ids": [item for item in legacy_event_ids if item],
        },
    )


def build_route_suggestions(
    context,
    *,
    active_skill: str,
    assistant_message: str,
    handoff_notes: str,
    runtime_registry=None,
    route_suggestion_client=None,
    allow_llm_without_route_context: bool = False,
    monitor_every_turn: bool = False,
    general_chat_card_threshold: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    available_catalog = _visible_skill_catalog(context, runtime_registry=runtime_registry)
    available = {str(item["skill_id"]) for item in available_catalog}
    route_choice_context = _has_route_choice_context(assistant_message)
    thresholds = _route_suggestion_thresholds(runtime_registry)
    card_threshold = (
        thresholds["card_threshold"]
        if general_chat_card_threshold is None
        else float(general_chat_card_threshold)
    )
    base_event: dict[str, Any] = {
        "active_skill": active_skill,
        "suggestion_count": 0,
        "suggestion_source": "",
        "llm_available": route_suggestion_client is not None,
        "fallback_used": False,
        "route_choice_context": route_choice_context,
        "monitor_every_turn": monitor_every_turn,
        "allow_llm_without_route_context": allow_llm_without_route_context,
        "confidence_threshold": _route_suggestion_confidence_threshold(
            route_choice_context, runtime_registry=runtime_registry
        ),
        "raw_response_preview": "",
        "analysis_reason": "",
        "error": "",
    }
    if not available:
        base_event["error"] = "runtime_registry_unavailable"
        return [], base_event
    deterministic_suggestions = _build_intent_route_suggestions(
        context,
        active_skill=active_skill,
        handoff_notes=handoff_notes,
        runtime_registry=runtime_registry,
    )
    explicit_general_chat_suggestions: list[dict[str, Any]] = []
    if active_skill == GENERAL_CHAT_SKILL_ID:
        # The main reply may explicitly offer a visible Skill while the
        # optional LLM post-processor is temporarily unavailable. Keep this
        # narrow, validated fallback separate from the normal score threshold.
        explicit_general_chat_suggestions = _explicit_general_chat_route_suggestions(
            deterministic_suggestions,
            assistant_message=assistant_message,
        )
        intent_route = _current_intent_route(context)
        raw_candidates = intent_route.get("candidate_skills") if isinstance(intent_route, dict) else []
        base_event["router_candidate_count"] = len(raw_candidates) if isinstance(raw_candidates, list) else 0
        base_event["card_threshold"] = card_threshold
        deterministic_suggestions = _limit_general_chat_route_suggestions(
            deterministic_suggestions,
            runtime_registry=runtime_registry,
            threshold=card_threshold,
        )
    if deterministic_suggestions:
        base_event["deterministic_candidate_count"] = len(deterministic_suggestions)
        base_event["deterministic_threshold"] = (
            card_threshold
            if active_skill == GENERAL_CHAT_SKILL_ID
            else thresholds["specialist_switch_threshold"]
        )

    if (
        active_skill != GENERAL_CHAT_SKILL_ID
        and not route_choice_context
        and not (allow_llm_without_route_context and route_suggestion_client is not None)
    ):
        base_event["suggestion_source"] = "intent_router" if deterministic_suggestions else ROUTE_SUGGESTION_SOURCE_LLM
        base_event["skipped_reason"] = "no_route_choice_context"
        base_event["duration_ms"] = 0
        base_event["suggestion_count"] = len(deterministic_suggestions)
        return deterministic_suggestions, base_event

    if route_suggestion_client is not None:
        suggestions, analysis = RouteSuggestionAnalyzer(
            route_suggestion_client,
            runtime_registry=runtime_registry,
            min_confidence=thresholds["min_confidence"],
            min_confidence_without_context=thresholds["min_confidence_without_context"],
        ).analyze(
            context,
            active_skill=active_skill,
            assistant_message=assistant_message,
            handoff_notes=handoff_notes,
            route_choice_context=route_choice_context,
            router_candidates=_visible_router_candidates(
                context,
                active_skill=active_skill,
                available_skill_ids=available,
            ),
            skill_catalog=available_catalog,
        )
        analysis["monitor_every_turn"] = monitor_every_turn
        analysis["allow_llm_without_route_context"] = allow_llm_without_route_context
        if suggestions:
            analysis["suggestion_count"] = len(suggestions)
            analysis["suggestion_source"] = ROUTE_SUGGESTION_SOURCE_LLM
            merged = _merge_route_suggestions(deterministic_suggestions, suggestions)
            analysis["suggestion_count"] = len(merged)
            if deterministic_suggestions:
                analysis["suggestion_source"] = "intent_router+llm_reply_analysis"
            return merged, analysis
        if explicit_general_chat_suggestions:
            analysis["suggestion_count"] = len(explicit_general_chat_suggestions)
            analysis["suggestion_source"] = ROUTE_SUGGESTION_SOURCE_FALLBACK
            analysis["fallback_used"] = True
            analysis["analysis_reason"] = (
                "保留主回复中明确点名并邀请点击的已校验 Skill，"
                "不受可选路由分析器的置信度过滤影响。"
            )
            return explicit_general_chat_suggestions, analysis
        if not analysis.get("error"):
            if deterministic_suggestions:
                analysis["suggestion_count"] = len(deterministic_suggestions)
                analysis["suggestion_source"] = "intent_router"
                analysis["analysis_reason"] = (
                    str(analysis.get("analysis_reason") or "")
                    + " 保留高置信度意图路由候选。"
                ).strip()
                return deterministic_suggestions, analysis
            return [], analysis
        # Route suggestions are an optional post-processing enhancement.  A
        # malformed or empty model response is retained in the audit event,
        # but must not turn a successfully completed answer into a client
        # visible model error. An explicit, user-facing Skill invitation in
        # that reply is safe to preserve when it is already backed by a
        # visible Intent Router candidate.
        base_event.update(analysis)
        if explicit_general_chat_suggestions:
            base_event["suggestion_count"] = len(explicit_general_chat_suggestions)
            base_event["suggestion_source"] = ROUTE_SUGGESTION_SOURCE_FALLBACK
            base_event["fallback_used"] = True
            base_event["analysis_reason"] = (
                "路由分析器未返回有效 JSON；保留主回复中明确点名并邀请点击的已校验 Skill。"
            )
            return explicit_general_chat_suggestions, base_event
    # A model failure must not turn heuristic/keyword output into clickable
    # cards.  The SSE error callback above keeps the failure observable.
    if deterministic_suggestions:
        base_event["suggestion_count"] = len(deterministic_suggestions)
        base_event["suggestion_source"] = "intent_router"
        return deterministic_suggestions, base_event
    return [], base_event


class RouteSuggestionAnalyzer:
    def __init__(
        self,
        client,
        *,
        runtime_registry,
        min_confidence: float = MIN_ROUTE_SUGGESTION_CONFIDENCE,
        min_confidence_without_context: float = MIN_ROUTE_SUGGESTION_CONFIDENCE_WITHOUT_CONTEXT,
    ) -> None:
        self.client = client
        self.runtime_registry = runtime_registry
        self.min_confidence = min_confidence
        self.min_confidence_without_context = min_confidence_without_context

    def _confidence_threshold(self, route_choice_context: bool) -> float:
        return self.min_confidence if route_choice_context else self.min_confidence_without_context

    def analyze(
        self,
        context,
        *,
        active_skill: str,
        assistant_message: str,
        handoff_notes: str,
        route_choice_context: bool,
        router_candidates: list[dict[str, Any]],
        skill_catalog: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        event: dict[str, Any] = {
            "active_skill": active_skill,
            "suggestion_count": 0,
            "suggestion_source": ROUTE_SUGGESTION_SOURCE_LLM,
            "llm_available": True,
            "fallback_used": False,
            "route_choice_context": route_choice_context,
            "confidence_threshold": self._confidence_threshold(route_choice_context),
            "raw_response_preview": "",
            "analysis_reason": "",
            "error": "",
        }
        try:
            started = time.perf_counter()
            messages = self._build_messages(
                context,
                active_skill=active_skill,
                assistant_message=assistant_message,
                route_choice_context=route_choice_context,
                router_candidates=router_candidates,
                skill_catalog=skill_catalog,
            )
            complete_kwargs: dict[str, Any] = {}
            try:
                if "request_purpose" in inspect.signature(self.client.complete).parameters:
                    complete_kwargs["request_purpose"] = "route_suggestion_analysis"
            except (TypeError, ValueError):
                pass
            raw_response = self.client.complete(messages, **complete_kwargs)
            event["duration_ms"] = int((time.perf_counter() - started) * 1000)
            event["request_purpose"] = "route_suggestion_analysis"
            event["prompt_chars"] = sum(len(str(item.content or "")) for item in messages)
            last_metrics = getattr(self.client, "last_request_metrics", None)
            if callable(last_metrics):
                metrics = last_metrics()
                event["input_tokens"] = metrics.get("input_tokens")
                event["output_tokens"] = metrics.get("output_tokens")
                event["ttft_ms"] = metrics.get("ttft_ms")
                event["llm_duration_ms"] = metrics.get("duration_ms")
            event["raw_response_preview"] = _truncate_text(raw_response, limit=1200)
            payload = _extract_json_object(raw_response)
        except Exception as exc:  # noqa: BLE001
            event["duration_ms"] = int((time.perf_counter() - started) * 1000) if "started" in locals() else 0
            event["error"] = f"{type(exc).__name__}: {exc}"
            return [], event

        event["analysis_reason"] = _truncate_text(
            str(payload.get("analysis_reason") or payload.get("reason") or ""),
            limit=1000,
        )
        raw_suggestions = payload.get("suggestions") if isinstance(payload, dict) else None
        if not isinstance(raw_suggestions, list):
            event["error"] = "invalid_suggestions_payload"
            return [], event

        suggestions = _normalize_llm_suggestions(
            context,
            raw_suggestions,
            active_skill=active_skill,
            handoff_notes=handoff_notes,
            runtime_registry=self.runtime_registry,
            route_choice_context=route_choice_context,
            min_confidence=self._confidence_threshold(route_choice_context),
        )
        event["suggestion_count"] = len(suggestions)
        return suggestions, event

    def _build_messages(
        self,
        context,
        *,
        active_skill: str,
        assistant_message: str,
        route_choice_context: bool,
        router_candidates: list[dict[str, Any]],
        skill_catalog: list[dict[str, Any]],
    ) -> list[ChatMessage]:
        option_spans = _extract_route_option_spans(assistant_message)
        prompt = {
            "task": "As the current chat or career-planning assistant, decide whether the completed reply should expose optional child-Skill cards.",
            "rules": [
                "Return JSON only.",
                "You decide whether a card is useful; router_candidates are soft evidence, never an instruction to create a card.",
                "Return {\"suggestions\": []} when the reply is only a follow-up question, ordinary answer, or the user has not been given a meaningful next direction.",
                "A card must be grounded in the user's current need and the completed assistant reply, not merely a keyword, score, school name, or a router match.",
                "Each suggestion must map to one target_skill_id from the visible skill_catalog only.",
                f"Only use confidence >= {self.min_confidence} when the mapping is clear.",
                (
                    "route_choice_context_detected=false is not a hard veto. "
                    f"When it is false, only return a suggestion if the assistant reply still contains an explicit selectable next topic and confidence >= {self.min_confidence_without_context}."
                ),
                f"Do not suggest the current active skill, {GENERAL_CHAT_SKILL_ID}, or the legacy planner alias.",
                "Never expose internal IDs, routing evidence, or this decision in user-facing labels/reasons.",
                "Always include analysis_reason. When suggestions is empty, state why no optional card improves this reply.",
            ],
            "output_schema": {
                "analysis_reason": "string; required; concise reason for returning or rejecting suggestions",
                "suggestions": [
                    {
                        "target_skill_id": "string",
                        "agent_label": "string",
                        "reason": "string",
                        "confidence": 0.0,
                        "handoff_notes": "string optional",
                    }
                ]
            },
            "active_skill": active_skill,
            "route_choice_context_detected": route_choice_context,
            "route_option_spans": option_spans,
            "routing_facts": _route_relevant_facts(context),
            "router_candidates": router_candidates,
            "skill_catalog": skill_catalog,
            "assistant_message": assistant_message,
        }
        return [
            ChatMessage(
                role="system",
                content=(
                    "你是 route suggestion 结构化分析器。"
                    "你只判断主回复是否清晰指向可点击进入的子 Skill。"
                    "必须只返回 JSON，不要输出解释。"
                ),
            ),
            ChatMessage(role="user", content=json.dumps(prompt, ensure_ascii=False)),
        ]


def _normalize_llm_suggestions(
    context,
    raw_suggestions: list[Any],
    *,
    active_skill: str,
    handoff_notes: str,
    runtime_registry,
    route_choice_context: bool,
    min_confidence: float | None = None,
) -> list[dict[str, Any]]:
    min_confidence = (
        float(min_confidence)
        if min_confidence is not None
        else _route_suggestion_confidence_threshold(route_choice_context)
    )
    available = set(_runtime_skill_ids(runtime_registry))
    suggestions: list[dict[str, Any]] = []
    seen_skill_ids: set[str] = set()
    for item in raw_suggestions:
        if not isinstance(item, dict):
            continue
        try:
            confidence = float(item.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            continue
        if confidence < min_confidence:
            continue
        target_skill_id = _normalize_target_skill_id(
            context,
            str(item.get("target_skill_id") or ""),
            runtime_registry=runtime_registry,
        )
        if (
            not target_skill_id
            or target_skill_id not in available
            or not _is_skill_visible_for_context(context, target_skill_id, runtime_registry=runtime_registry)
            or target_skill_id == active_skill
            or target_skill_id in seen_skill_ids
        ):
            continue
        seen_skill_ids.add(target_skill_id)
        display = build_skill_display(context, active_skill=target_skill_id, runtime_registry=runtime_registry)
        suggestions.append(
            {
                "target_skill_id": target_skill_id,
                "skill_id": target_skill_id,
                "skill_name": display["skill_name"],
                # The post-processing model may echo an outdated display name.
                # Product-facing labels always come from the current SKILL.md.
                "agent_label": display["agent_label"],
                "brief": display.get("brief", ""),
                "info": display.get("info", ""),
                "description": display.get("description", ""),
                "scene_name": display.get("scene_name", ""),
                "skill_theme": display.get("skill_theme", ""),
                "reason": str(item.get("reason") or "主回复中清晰指向该规划主题。"),
                "confidence": confidence,
                "handoff_notes": str(item.get("handoff_notes") or handoff_notes),
                "suggestion_source": ROUTE_SUGGESTION_SOURCE_LLM,
            }
        )
    return suggestions


def _route_suggestion_thresholds(runtime_registry=None) -> dict[str, float]:
    main_bundle = runtime_registry.get_raw(CAREER_PLAN_SKILL_ID) if runtime_registry is not None else None
    config = getattr(
        getattr(getattr(main_bundle, "runtime_metadata", None), "planner", None),
        "intent_router",
        None,
    )
    return {
        "min_confidence": float(
            getattr(config, "route_suggestion_min_confidence", MIN_ROUTE_SUGGESTION_CONFIDENCE)
        ),
        "min_confidence_without_context": float(
            getattr(
                config,
                "route_suggestion_min_confidence_without_context",
                MIN_ROUTE_SUGGESTION_CONFIDENCE_WITHOUT_CONTEXT,
            )
        ),
        "card_threshold": float(
            getattr(config, "route_suggestion_card_threshold", MIN_GENERAL_CHAT_CARD_CONFIDENCE)
        ),
        "specialist_switch_threshold": float(
            getattr(config, "specialist_switch_threshold", MIN_SPECIALIST_SWITCH_CARD_CONFIDENCE)
        ),
    }


def _route_suggestion_confidence_threshold(route_choice_context: bool, *, runtime_registry=None) -> float:
    thresholds = _route_suggestion_thresholds(runtime_registry)
    return (
        thresholds["min_confidence"]
        if route_choice_context
        else thresholds["min_confidence_without_context"]
    )


def _build_strong_format_suggestions(
    context,
    *,
    active_skill: str,
    assistant_message: str,
    handoff_notes: str,
    runtime_registry=None,
    route_choice_context: bool | None = None,
) -> list[dict[str, Any]]:
    if route_choice_context is False:
        return []
    main_bundle = runtime_registry.get(CAREER_PLAN_SKILL_ID) if runtime_registry is not None else None
    routes = tuple(getattr(getattr(main_bundle, "contract", None), "routes", ()) or ())
    route_by_scene = {str(route.scene): str(route.target_skill_id) for route in routes}
    if not route_by_scene:
        return []

    suggestions: list[dict[str, Any]] = []
    seen_skill_ids: set[str] = set()
    for scene_name in _extract_ordered_scene_choices(assistant_message):
        target_skill_id = _target_skill_for_scene_choice(
            context,
            scene_name,
            route_by_scene,
            runtime_registry=runtime_registry,
        )
        if not target_skill_id or target_skill_id == active_skill or target_skill_id in seen_skill_ids:
            continue
        if runtime_registry is not None and runtime_registry.get(target_skill_id) is None:
            continue
        seen_skill_ids.add(target_skill_id)
        display = build_skill_display(context, active_skill=target_skill_id, runtime_registry=runtime_registry)
        suggestions.append(
            {
                "target_skill_id": target_skill_id,
                "skill_id": target_skill_id,
                "skill_name": display["skill_name"],
                "agent_label": display["agent_label"],
                "brief": display.get("brief", ""),
                "info": display.get("info", ""),
                "description": display.get("description", ""),
                "scene_name": display.get("scene_name", ""),
                "skill_theme": display.get("skill_theme", ""),
                "reason": f"用户可从升学规划顾问推荐方向「{scene_name}」继续深入。",
                "confidence": 0.92,
                "handoff_notes": handoff_notes,
                "suggestion_source": ROUTE_SUGGESTION_SOURCE_FALLBACK,
            }
        )
    return suggestions


def _build_intent_route_suggestions(
    context,
    *,
    active_skill: str,
    handoff_notes: str,
    runtime_registry=None,
) -> list[dict[str, Any]]:
    main_state = (getattr(context, "skill_states", {}) or {}).get(CAREER_PLAN_SKILL_ID, {})
    if not main_state:
        main_state = (getattr(context, "skill_states", {}) or {}).get(LEGACY_MAIN_PLANNER_SKILL_ID, {})
    intent_route = main_state.get("intent_route", {}) if isinstance(main_state, dict) else {}
    if not isinstance(intent_route, dict):
        return []
    route_mode = str(intent_route.get("route_mode") or "")
    if route_mode and route_mode != "recommend_switch":
        return []
    available = set(_runtime_skill_ids(runtime_registry))
    raw_candidates = (
        intent_route.get("candidate_skills")
        if active_skill in {GENERAL_CHAT_SKILL_ID, CAREER_PLAN_SKILL_ID}
        else None
    )
    candidates = raw_candidates if isinstance(raw_candidates, list) else []
    if not candidates:
        candidates = [
            {
                "target_skill_id": intent_route.get("target_skill_id"),
                "confidence": intent_route.get("confidence"),
                "reason": intent_route.get("reason"),
            }
        ]
    suggestions: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        target_skill_id = str(candidate.get("target_skill_id") or "").strip()
        if not target_skill_id or target_skill_id not in available or target_skill_id == active_skill:
            continue
        try:
            confidence = float(candidate.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            continue
        minimum_confidence = (
            _route_suggestion_thresholds(runtime_registry)["specialist_switch_threshold"]
            if active_skill not in {CAREER_PLAN_SKILL_ID, GENERAL_CHAT_SKILL_ID}
            else _route_suggestion_thresholds(runtime_registry)["card_threshold"]
        )
        if confidence < minimum_confidence:
            continue
        display = build_skill_display(context, active_skill=target_skill_id, runtime_registry=runtime_registry)
        suggestions.append(
            {
                "target_skill_id": target_skill_id,
                "skill_id": target_skill_id,
                "skill_name": display["skill_name"],
                "agent_label": display["agent_label"],
                "brief": display.get("brief", ""),
                "info": display.get("info", ""),
                "description": display.get("description", ""),
                "scene_name": display.get("scene_name", ""),
                "skill_theme": display.get("skill_theme", ""),
                "reason": str(candidate.get("reason") or intent_route.get("reason") or "识别到该问题适合进入对应规划 Skill。"),
                "confidence": confidence,
                "handoff_notes": handoff_notes,
                "suggestion_source": "intent_router",
            }
        )
    return suggestions


def _current_intent_route(context) -> dict[str, Any]:
    states = getattr(context, "skill_states", {}) or {}
    state = states.get(CAREER_PLAN_SKILL_ID, {}) if isinstance(states, dict) else {}
    if not state and isinstance(states, dict):
        state = states.get(LEGACY_MAIN_PLANNER_SKILL_ID, {})
    route = state.get("intent_route", {}) if isinstance(state, dict) else {}
    return route if isinstance(route, dict) else {}


def _limit_general_chat_route_suggestions(
    suggestions: list[dict[str, Any]],
    *,
    runtime_registry=None,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    main_bundle = runtime_registry.get(CAREER_PLAN_SKILL_ID) if runtime_registry is not None else None
    router_config = getattr(getattr(getattr(main_bundle, "runtime_metadata", None), "planner", None), "intent_router", None)
    max_candidates = max(int(getattr(router_config, "general_chat_choice_max_candidates", 3) or 3), 1)
    threshold = float(
        threshold
        if threshold is not None
        else getattr(
            router_config,
            "route_suggestion_card_threshold",
            getattr(router_config, "general_chat_choice_threshold", MIN_GENERAL_CHAT_CARD_CONFIDENCE),
        )
    )
    filtered = [
        item
        for item in suggestions
        if isinstance(item, dict) and float(item.get("confidence", 0) or 0) >= threshold
    ]
    filtered.sort(key=lambda item: float(item.get("confidence", 0) or 0), reverse=True)
    return filtered[:max_candidates]


def _explicit_general_chat_route_suggestions(
    suggestions: list[dict[str, Any]],
    *,
    assistant_message: str,
) -> list[dict[str, Any]]:
    """Keep only router candidates explicitly offered as a clickable next step."""
    if not re.search(r"(?:点击|进入|选择)", assistant_message):
        return []
    explicit: list[dict[str, Any]] = []
    for item in suggestions:
        names = (
            str(item.get("agent_label") or "").strip(),
            str(item.get("skill_name") or "").strip(),
            str(item.get("scene_name") or "").strip(),
        )
        if any(name and name in assistant_message for name in names):
            fallback_item = dict(item)
            fallback_item["suggestion_source"] = ROUTE_SUGGESTION_SOURCE_FALLBACK
            explicit.append(fallback_item)
    return explicit


def _merge_route_suggestions(
    primary: list[dict[str, Any]],
    supplemental: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not supplemental:
        return primary
    merged: list[dict[str, Any]] = []
    supplemental_by_skill = {
        str(item.get("target_skill_id") or ""): item
        for item in supplemental
        if isinstance(item, dict) and item.get("target_skill_id")
    }
    primary_by_skill = {
        str(item.get("target_skill_id") or ""): item
        for item in primary
        if isinstance(item, dict) and item.get("target_skill_id")
    }
    for item in supplemental:
        target_skill_id = str(item.get("target_skill_id") or "")
        if target_skill_id and target_skill_id in primary_by_skill:
            merged.append(primary_by_skill[target_skill_id])
        elif target_skill_id:
            merged.append(item)
    for item in primary:
        target_skill_id = str(item.get("target_skill_id") or "")
        if target_skill_id and target_skill_id not in supplemental_by_skill:
            merged.append(item)
    merged.sort(key=lambda item: float(item.get("confidence", 0) or 0), reverse=True)
    return merged


def _build_explicit_option_suggestions(
    context,
    *,
    active_skill: str,
    assistant_message: str,
    handoff_notes: str,
    runtime_registry=None,
    route_choice_context: bool,
) -> list[dict[str, Any]]:
    if not route_choice_context:
        return []
    available = set(_runtime_skill_ids(runtime_registry))
    suggestions: list[dict[str, Any]] = []
    seen_skill_ids: set[str] = set()
    for option_text in _extract_route_option_spans(assistant_message):
        target_skill_id = _match_target_skill_from_option(
            context,
            option_text,
            runtime_registry=runtime_registry,
        )
        if (
            not target_skill_id
            or target_skill_id not in available
            or target_skill_id == active_skill
            or target_skill_id in seen_skill_ids
        ):
            continue
        seen_skill_ids.add(target_skill_id)
        display = build_skill_display(context, active_skill=target_skill_id, runtime_registry=runtime_registry)
        suggestions.append(
            {
                "target_skill_id": target_skill_id,
                "agent_label": display["agent_label"],
                "reason": f"主回复明确给出可选规划方向：{_truncate_text(option_text, limit=80)}",
                "confidence": 0.88,
                "handoff_notes": handoff_notes,
                "suggestion_source": ROUTE_SUGGESTION_SOURCE_LLM,
            }
        )
    return suggestions


def _build_continuation_offer_suggestions(
    context,
    *,
    active_skill: str,
    assistant_message: str,
    handoff_notes: str,
    runtime_registry=None,
) -> list[dict[str, Any]]:
    if active_skill == "junior_multi_path_planning":
        return []
    if _infer_school_stage(context) != "junior":
        return []
    available = set(_runtime_skill_ids(runtime_registry))
    if "junior_multi_path_planning" not in available:
        return []
    if not _looks_like_junior_talent_path_continuation(assistant_message):
        return []
    display = build_skill_display(
        context,
        active_skill="junior_multi_path_planning",
        runtime_registry=runtime_registry,
    )
    return [
        {
            "target_skill_id": "junior_multi_path_planning",
            "agent_label": display["agent_label"],
            "reason": "主回复明确邀请继续深入初中美术/艺体特长生路径准备。",
            "confidence": 0.91,
            "handoff_notes": handoff_notes,
            "suggestion_source": ROUTE_SUGGESTION_SOURCE_LLM,
        }
    ]


def _looks_like_junior_talent_path_continuation(text: str) -> bool:
    content = " ".join(str(text or "").split())
    if not content:
        return False
    talent_path_pattern = r"(美术特长生|美术生|艺体|艺术特长生|体育特长生|特长生通道|特长生招生|特色高中|艺体班)"
    if not re.search(talent_path_pattern, content):
        return False
    offer_pattern = r"(要不要我|要不我|我接着|接着跟你|继续聊|详细讲讲|具体怎么走|怎么准备|做什么准备|时间怎么安排|要关注哪些信息|哪些高中|招生简章|专业测试)"
    if not re.search(offer_pattern, content):
        return False
    collection_pattern = r"(什么程度|随便画着玩|正经学过|参加过比赛|考过级|画画到什么程度)"
    if re.search(collection_pattern, content) and not re.search(
        r"(具体怎么走|怎么准备|做什么准备|时间怎么安排|招生简章|哪些高中|专业测试)",
        content,
    ):
        return False
    return True


def _has_route_choice_context(text: str) -> bool:
    content = str(text or "").strip()
    if not content:
        return False
    if re.search(r"进入[【「].+?[】」]", content):
        return True
    option_spans = _extract_route_option_spans(content)
    if len(option_spans) >= 2:
        return True
    choice_markers = (
        "可聚焦",
        "聚焦两条主线",
        "聚焦三条",
        "供你选择",
        "选择主攻方向",
        "可选择的",
        "可选的",
        "下一步可选择",
        "下一步可以继续",
        "可以继续看",
        "你想先从哪",
        "你更想优先",
        "优先深入",
        "先从哪一条",
        "哪个方向开始",
        "哪个 agent",
        "哪个规划主题",
        "哪个子场景",
        "哪一个开始深入",
    )
    if not any(marker in content for marker in choice_markers):
        return False
    followup_markers = (
        "接下来我想快速确认",
        "接下来我想确认",
        "我想快速确认",
        "请先告诉我",
        "先告诉我",
        "需要先补充",
        "需了解两个关键信息",
        "这两点确认后",
        "这两点将帮",
        "你简单说说",
        "请你补充",
        "请补充",
    )
    if any(marker in content for marker in followup_markers) and len(option_spans) < 2:
        return False
    return True


def _extract_route_option_spans(text: str) -> list[str]:
    content = str(text or "")
    spans: list[str] = []
    for line in content.splitlines():
        normalized = line.strip()
        if not normalized:
            continue
        if re.search(r"(【\s*(主推|备选|推荐|可选)|\b(主推|备选)\b|主推方向|备选支持|进入[【「])", normalized):
            spans.append(normalized)
            continue
        if re.match(r"^[-*•🔹🔸]?\s*\d+[.、)]\s*", normalized) and re.search(
            r"(提分|学业|兴趣|特长|路径|升学|选科|模拟|前景|专业|职业|进入)",
            normalized,
        ):
            spans.append(normalized)
    compact_content = " ".join(content.split())
    for match in re.finditer(r"进入[【「].+?[】」]", compact_content):
        span = _slice_sentence(compact_content, match.start(), match.end())
        if span:
            spans.append(span)
    result: list[str] = []
    seen: set[str] = set()
    for span in spans:
        normalized = _truncate_text(span.strip(), limit=220)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _slice_sentence(text: str, start: int, end: int) -> str:
    left = max(text.rfind(delimiter, 0, start) for delimiter in ("。", "！", "？", "\n", ";", "；"))
    right_candidates = [text.find(delimiter, end) for delimiter in ("。", "！", "？", "\n", ";", "；")]
    right_candidates = [index for index in right_candidates if index >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1 : right].strip()


def _match_target_skill_from_option(context, option_text: str, *, runtime_registry=None) -> str:
    text = str(option_text or "")
    if not text:
        return ""
    main_bundle = runtime_registry.get(CAREER_PLAN_SKILL_ID) if runtime_registry is not None else None
    routes = tuple(getattr(getattr(main_bundle, "contract", None), "routes", ()) or ())
    route_by_scene = {str(route.scene): str(route.target_skill_id) for route in routes}
    for scene_name, target_skill_id in route_by_scene.items():
        if scene_name and scene_name in text:
            return _target_skill_for_scene_choice(
                context,
                scene_name,
                route_by_scene,
                runtime_registry=runtime_registry,
            )

    alias_scores: dict[str, int] = {}
    for skill_id, aliases in _route_aliases_by_skill(runtime_registry).items():
        score = sum(len(alias) for alias in aliases if alias and alias in text)
        if score:
            alias_scores[skill_id] = score
    if not alias_scores:
        return ""
    target_skill_id = max(alias_scores.items(), key=lambda item: item[1])[0]
    return _normalize_target_skill_id(context, target_skill_id, runtime_registry=runtime_registry)


def _route_aliases_by_skill(runtime_registry=None) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {
        "score_improve": {
            "提分",
            "提分规划",
            "学业稳基",
            "学科提分",
            "补弱科",
            "补短板",
            "成绩提升",
            "提升成绩",
            "学习方法",
            "学习习惯",
            "基础学习",
            "稳住优势学科",
        },
        "interest_explore": {
            "兴趣探索",
            "兴趣收敛",
            "艺术方向探索",
            "特长方向",
            "特长判断",
            "兴趣方向",
            "能否持续投入",
            "潜在优势",
            "培养建议",
        },
        "junior_multi_path_planning": {
            "初中多元路径规划",
            "初中多元",
            "初中多元升学",
            "多元升学路径初筛",
            "中考保底",
            "职教",
            "普职融通",
            "美术特色高中",
            "美术特长生",
            "美术生招生",
            "高中特长生招生",
            "特长生招生",
            "招生简章",
            "专业测试",
            "艺体班",
            "特色校",
            "特长生通道",
        },
        "multi_path_planning": {
            "高中多元路径",
            "多元路径规划",
            "多元升学路径",
            "普通高考之外",
            "强基计划",
            "综合评价",
            "竞赛",
        },
        "future_explore": {
            "前景探路",
            "专业职业探索",
            "专业前景",
            "职业方向",
            "未来发展",
            "长期发展",
        },
        "subject_advisor": {
            "选科参谋",
            "选科",
            "科目组合",
            "目标专业约束",
        },
        "mock_admission": {
            "模拟升学",
            "模拟录取",
            "可报学校",
            "学校层次",
            "能上什么大学",
            "分数对应学校",
            "冲稳保",
        },
    }
    for skill_id, bundle in _iter_runtime_bundles(runtime_registry) or ():
        if bundle is None:
            continue
        target_aliases = aliases.setdefault(str(skill_id), set())
        runtime_meta = getattr(bundle, "runtime_metadata", None)
        routing = getattr(runtime_meta, "routing", None)
        for value in (
            str(getattr(runtime_meta, "name", "") or ""),
            str(getattr(routing, "scene_name", "") or "") if routing is not None else "",
            str(skill_id),
        ):
            if value:
                target_aliases.add(value)
        target_aliases.update(str(value) for value in getattr(runtime_meta, "accepts_scenes", ()) or () if value)
        target_aliases.update(str(value) for value in getattr(runtime_meta, "triggers", ()) or () if value)
    return aliases


def _extract_ordered_scene_choices(text: str) -> list[str]:
    scenes: list[str] = []
    for match in re.finditer(r"进入[【「](.+?)[】」]", str(text or "")):
        scene_name = match.group(1).strip()
        if scene_name and scene_name not in scenes:
            scenes.append(scene_name)
    return scenes


def _target_skill_for_scene_choice(
    context,
    scene_name: str,
    route_by_scene: dict[str, str],
    *,
    runtime_registry=None,
) -> str:
    if _is_multi_path_scene(scene_name):
        school_stage = _infer_school_stage(context)
        if school_stage == "junior" or "初中" in scene_name:
            return "junior_multi_path_planning"
        if school_stage == "senior":
            return "multi_path_planning"
    target_skill_id = route_by_scene.get(scene_name, "")
    if target_skill_id == "multi_path_planning" and _infer_school_stage(context) == "junior":
        return "junior_multi_path_planning"
    return _normalize_target_skill_id(context, target_skill_id, runtime_registry=runtime_registry)


def _normalize_target_skill_id(context, target_skill_id: str, *, runtime_registry=None) -> str:
    skill_id = canonical_skill_id(target_skill_id)
    if not skill_id:
        return ""
    if skill_id == "multi_path_planning" and _infer_school_stage(context) == "junior":
        return "junior_multi_path_planning"
    if _is_multi_path_scene(skill_id) and _infer_school_stage(context) == "junior":
        return "junior_multi_path_planning"
    if runtime_registry is not None and runtime_registry.get(skill_id) is not None:
        return skill_id
    return skill_id


def _is_multi_path_scene(scene_name: str) -> bool:
    return scene_name in {"多元路径规划", "多元路径", "多元升学路径", "初中多元路径规划"} or "多元路径" in scene_name


def _infer_school_stage(context) -> str:
    candidates: list[str] = []
    known_facts = getattr(context, "known_facts", None)
    if known_facts is not None and hasattr(known_facts, "get_value"):
        for key in ("grade", "school_stage"):
            value = known_facts.get_value(key)
            if value is not None:
                candidates.append(str(value))
    runtime_state = (getattr(context, "skill_states", {}) or {}).get("skill_runtime", {})
    if isinstance(runtime_state, dict):
        global_facts = runtime_state.get("global_facts", {})
        if isinstance(global_facts, dict):
            for key in ("grade", "school_stage"):
                value = global_facts.get(key)
                if value is not None:
                    candidates.append(str(value))
    for message in reversed(getattr(context, "messages", []) or []):
        if isinstance(message, dict) and message.get("role") == "user":
            candidates.append(str(message.get("content") or ""))
    for candidate in candidates:
        stage = _infer_school_stage_from_text(candidate)
        if stage:
            return stage
    return ""


def _infer_school_stage_from_text(text: str) -> str:
    normalized = str(text or "")
    if any(keyword in normalized for keyword in ("小学", "一年级", "二年级", "三年级", "四年级", "五年级", "六年级")):
        return "primary"
    if any(keyword in normalized for keyword in ("初中", "初一", "初二", "初三", "七年级", "八年级", "九年级", "中考")):
        return "junior"
    if any(keyword in normalized for keyword in ("高中", "高一", "高二", "高三", "高考")):
        return "senior"
    return ""


def _school_stage_scope_values(raw_scope: object) -> set[str]:
    normalized = str(raw_scope or "").strip().lower().replace("-", "_")
    if not normalized or normalized == "all":
        return set()
    aliases = {
        "primary": {"primary"},
        "junior": {"junior"},
        "senior": {"senior"},
        "primary_junior": {"primary", "junior"},
        "junior_senior": {"junior", "senior"},
        "primary_senior": {"primary", "senior"},
    }
    if normalized in aliases:
        return aliases[normalized]
    return {part for part in re.split(r"[,/|\\s]+", normalized) if part in {"primary", "junior", "senior"}}


def _is_skill_visible_for_context(context, skill_id: str, *, runtime_registry=None) -> bool:
    if skill_id in {GENERAL_CHAT_SKILL_ID, LEGACY_MAIN_PLANNER_SKILL_ID}:
        return False
    bundle = runtime_registry.get(skill_id) if runtime_registry is not None else None
    if bundle is None:
        return False
    runtime_meta = getattr(bundle, "runtime_metadata", None)
    routing = getattr(runtime_meta, "routing", None)
    scope = getattr(routing, "school_stage_scope", "") if routing is not None else ""
    allowed_stages = _school_stage_scope_values(scope)
    if not allowed_stages:
        return True
    current_stage = _infer_school_stage(context)
    # When a profile has not supplied a grade yet, keep the catalog usable and
    # let the Skill ask for the missing fact.  Once known, hide incompatible
    # specialist cards from the model's recommendation catalog.
    return not current_stage or current_stage in allowed_stages


def _visible_skill_catalog(context, *, runtime_registry=None) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for skill_id, bundle in _iter_runtime_bundles(runtime_registry):
        skill_id = str(skill_id)
        if bundle is None or not _is_skill_visible_for_context(context, skill_id, runtime_registry=runtime_registry):
            continue
        runtime_meta = getattr(bundle, "runtime_metadata", None)
        routing = getattr(runtime_meta, "routing", None)
        display = build_skill_display(
            _DisplayContext(),
            active_skill=str(skill_id),
            runtime_registry=runtime_registry,
        )
        catalog.append(
            {
                "skill_id": skill_id,
                "agent_label": display["agent_label"],
                "name": str(getattr(runtime_meta, "name", "") or ""),
                "description": _truncate_text(str(getattr(runtime_meta, "description", "") or ""), limit=120),
                "scene_name": str(getattr(routing, "scene_name", "") or "") if routing is not None else "",
                "accepts_scenes": list(getattr(runtime_meta, "accepts_scenes", ()) or ()),
                "triggers": list(getattr(runtime_meta, "triggers", ()) or ())[:8],
                "routing_examples": list(getattr(routing, "routing_examples", ()) or ())[:5] if routing is not None else [],
                "school_stage_scope": str(getattr(routing, "school_stage_scope", "") or "") if routing is not None else "",
            }
        )
    return catalog


def _runtime_skill_catalog(runtime_registry) -> list[dict[str, Any]]:
    """Compatibility helper for diagnostics that do not have a session."""
    return [
        item
        for item in _visible_skill_catalog(_DisplayContext(), runtime_registry=runtime_registry)
    ]


def _visible_router_candidates(
    context,
    *,
    active_skill: str,
    available_skill_ids: set[str],
) -> list[dict[str, Any]]:
    main_state = (getattr(context, "skill_states", {}) or {}).get(CAREER_PLAN_SKILL_ID, {})
    if not main_state:
        main_state = (getattr(context, "skill_states", {}) or {}).get(LEGACY_MAIN_PLANNER_SKILL_ID, {})
    intent_route = main_state.get("intent_route", {}) if isinstance(main_state, dict) else {}
    raw_candidates = intent_route.get("candidate_skills", []) if isinstance(intent_route, dict) else []
    if not isinstance(raw_candidates, list):
        return []
    candidates: list[dict[str, Any]] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        target_skill_id = _normalize_target_skill_id(
            context,
            str(item.get("target_skill_id") or ""),
            runtime_registry=None,
        )
        if not target_skill_id or target_skill_id == active_skill or target_skill_id not in available_skill_ids:
            continue
        try:
            confidence = float(item.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            continue
        candidates.append(
            {
                "target_skill_id": target_skill_id,
                "confidence": confidence,
                "reason": _truncate_text(str(item.get("reason") or ""), limit=240),
                "scene_name": _truncate_text(str(item.get("scene_name") or ""), limit=80),
                "matched_examples": list(item.get("matched_examples") or [])[:3],
            }
        )
    return sorted(candidates, key=lambda item: item["confidence"], reverse=True)[:3]


def _route_relevant_facts(context) -> dict[str, Any]:
    keys = ("grade", "school_stage", "score_level", "talent", "gender")
    facts: dict[str, Any] = {}
    known_facts = getattr(context, "known_facts", None)
    if known_facts is not None and hasattr(known_facts, "get_value"):
        for key in keys:
            value = known_facts.get_value(key)
            if value is not None:
                facts[key] = value
    runtime_state = (getattr(context, "skill_states", {}) or {}).get("skill_runtime", {})
    if isinstance(runtime_state, dict):
        global_facts = runtime_state.get("global_facts", {})
        if isinstance(global_facts, dict):
            for key in keys:
                if key not in facts and global_facts.get(key) is not None:
                    facts[key] = global_facts.get(key)
    inferred_stage = _infer_school_stage(context)
    if inferred_stage:
        facts.setdefault("school_stage_inferred", inferred_stage)
    return facts


class _DisplayContext:
    skill_states: dict[str, Any] = {}
    interaction_state: dict[str, Any] = {}


def _iter_runtime_bundles(runtime_registry):
    if runtime_registry is None:
        return
    enabled_bundles = getattr(runtime_registry, "enabled_bundles", None)
    if callable(enabled_bundles):
        yield from enabled_bundles().items()
        return
    public_bundles = getattr(runtime_registry, "bundles", None)
    if isinstance(public_bundles, dict):
        yield from public_bundles.items()
        return
    bundles = getattr(runtime_registry, "_bundles", None)
    if isinstance(bundles, dict):
        yield from bundles.items()
        return
    names = getattr(runtime_registry, "names", None)
    getter = getattr(runtime_registry, "get", None)
    if callable(names) and callable(getter):
        for name in names():
            yield name, getter(name)


def _recent_history(context, *, limit: int = 6) -> list[dict[str, str]]:
    messages = list(getattr(context, "messages", []) or [])[-limit:]
    history: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        history.append(
            {
                "role": str(message.get("role") or ""),
                "content": _truncate_text(str(message.get("content") or ""), limit=500),
            }
        )
    return history


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("route suggestion analyzer must return a JSON object")
    return payload


def _truncate_text(value: str, *, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(truncated)"


def _persist_finalization_to_runtime_state(
    context,
    *,
    active_skill: str,
    conclusion_summary: str,
    context_compression: dict[str, Any],
    route_suggestions: list[dict[str, Any]],
    is_final_summary: bool,
) -> None:
    runtime_state = context.skill_states.setdefault("skill_runtime", {})
    status_flags = runtime_state.setdefault("status_flags", {})
    if isinstance(status_flags, dict):
        if is_final_summary:
            status_flags["conclusion_presented"] = True
        status_flags["context_compression"] = context_compression
        status_flags["route_suggestions"] = route_suggestions
    skill_facts = runtime_state.setdefault("skill_facts", {})
    if isinstance(skill_facts, dict):
        skill_facts.setdefault(active_skill, {})
        if is_final_summary and isinstance(skill_facts[active_skill], dict):
            skill_facts[active_skill]["conclusion_summary"] = conclusion_summary


def _is_final_summary_turn(context, *, active_skill: str, assistant_message: str, runtime_registry=None) -> bool:
    runtime_state = context.skill_states.get("skill_runtime", {})
    if not isinstance(runtime_state, dict):
        return False
    status_flags = runtime_state.get("status_flags", {})
    if isinstance(status_flags, dict) and (
        status_flags.get("conclusion_confirmed") is True
        or str(status_flags.get("user_satisfied") or "").strip() == "confirmed"
    ):
        return True
    bundle = runtime_registry.get(active_skill) if runtime_registry is not None and active_skill else None
    stage = str(runtime_state.get("stage") or "")
    stage_contract = get_stage_contract(bundle.contract, stage) if bundle is not None else None
    if stage_contract is not None and stage_contract.kind == "summary":
        return True
    return _looks_like_final_summary_text(assistant_message)


def _looks_like_final_summary_text(text: str) -> bool:
    content = str(text or "").strip()
    if len(content) < 80:
        return False
    final_markers = (
        "总结",
        "综上",
        "整体来看",
        "阶段结论",
        "最后",
        "下一步可以",
        "后续可以",
        "方案如下",
        "主推方向",
        "备选支持",
        "备选场景",
        "可聚焦两条主线",
        "我们可聚焦",
        "需要我先",
    )
    if not any(marker in content for marker in final_markers):
        return False
    followup_markers = (
        "请告诉我",
        "可以先告诉我",
        "先告诉我",
        "先确认",
        "还需要",
        "缺少",
        "接下来，我们需了解",
        "接下来，我们需要了解",
        "需了解两个关键信息",
        "这两点将帮",
        "你简单说说",
        "想确认一下",
        "还想确认",
        "请你补充",
        "请补充",
        "接下来想确认",
    )
    return not any(marker in content for marker in followup_markers)


def _build_conclusion_summary(assistant_message: str) -> str:
    text = " ".join(str(assistant_message or "").split())
    if len(text) <= 220:
        return text
    return f"{text[:220]}..."


def _build_handoff_notes(*, agent_label: str, conclusion_summary: str, facts_delta: list[dict[str, Any]]) -> str:
    changed_keys = [str(item.get("key") or "") for item in facts_delta if isinstance(item, dict) and item.get("key")]
    facts_text = f"；本轮更新 facts：{', '.join(changed_keys)}" if changed_keys else ""
    return f"来自{agent_label or '当前规划主题'}的阶段总结：{conclusion_summary}{facts_text}"


def _runtime_skill_ids(runtime_registry) -> list[str]:
    if runtime_registry is None:
        return []
    enabled_bundles = getattr(runtime_registry, "enabled_bundles", None)
    if callable(enabled_bundles):
        public_bundles = enabled_bundles()
    else:
        public_bundles = getattr(runtime_registry, "bundles", None)
    if isinstance(public_bundles, dict):
        return [
            str(key)
            for key in public_bundles
            if str(key) not in {LEGACY_MAIN_PLANNER_SKILL_ID, GENERAL_CHAT_SKILL_ID}
        ]
    bundles = getattr(runtime_registry, "_bundles", None)
    if isinstance(bundles, dict):
        return [str(key) for key in bundles if str(key) not in {LEGACY_MAIN_PLANNER_SKILL_ID, GENERAL_CHAT_SKILL_ID}]
    names = getattr(runtime_registry, "names", None)
    if callable(names):
        return [str(item) for item in names() if str(item) not in {LEGACY_MAIN_PLANNER_SKILL_ID, GENERAL_CHAT_SKILL_ID}]
    return []
