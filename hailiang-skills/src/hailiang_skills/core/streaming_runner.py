from __future__ import annotations

import json
from contextvars import copy_context
from queue import Empty, Full, Queue
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterator
from uuid import uuid4

from hailiang_skills.core.fact_service import FactService, serialize_known_facts
from hailiang_skills.core.fact_prompt_builder import build_missing_fact_form_block
from hailiang_skills.core.path_action_builder import (
    build_citations_block,
    build_path_actions_block,
)
from hailiang_skills.core.logging import make_event, utc_now_iso
from hailiang_skills.core.skill_lifecycle import (
    attach_finalization_metadata,
    build_skill_invitation,
    build_finalized_payload,
    build_route_suggestions_exposed_event,
)
from hailiang_skills.core.skill_display import build_skill_display
from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID, GENERAL_CHAT_SKILL_ID, canonical_skill_id
from hailiang_skills.core.message_interactions import ensure_message_interactions
from hailiang_skills.core.message_interactions import ACTIVE, SELECTED, expire_active_interactions, update_interaction
from hailiang_skills.core.model_errors import public_model_error
from hailiang_skills.core.concurrency import TurnCoordinator, TurnLease
from hailiang_skills.core.conversation_state import get_conversation_state, record_security_result
from hailiang_skills.core.session_logging import append_sse_record
from hailiang_skills.core.sse_protocol import SseEnvelopeBuilder, UNIFIED_PROTOCOL, normalize_protocol
from hailiang_skills.core.status_labels import normalize_status_label
from hailiang_skills.core.telemetry import SSE_ACTIVE, SSE_TTFT, bind_telemetry, current_telemetry, reset_telemetry, span
from hailiang_skills.runtime_bridge.facts import RUNTIME_STATE_KEY
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository
from hailiang_skills.storage.repositories.postgres_repo import SessionVersionConflict
from hailiang_skills.security.models import ModerationBlockedError


def format_sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# A stop request must wake the original response iterator immediately.  It is
# deliberately an internal marker and never crosses the SSE wire.
_CANCEL_WAKEUP = object()


def _diff_facts(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        before_item = before.get(key)
        after_item = after.get(key)
        before_value = before_item.get("value") if isinstance(before_item, dict) else None
        after_value = after_item.get("value") if isinstance(after_item, dict) else None
        if before_value == after_value and before_item == after_item:
            continue
        changes.append(
            {
                "key": key,
                "before": before_value,
                "after": after_value,
                "scope": (after_item or before_item or {}).get("scope"),
                "source": {
                    "type": (after_item or before_item or {}).get("source_type"),
                    "source_id": (after_item or before_item or {}).get("source_id"),
                    "source_label": (after_item or before_item or {}).get("source_label"),
                },
                "updated_at": (after_item or before_item or {}).get("updated_at"),
            }
        )
    return changes


def _split_reply_delta(text: str, *, chunk_size: int = 18) -> list[str]:
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for char in text:
        current += char
        if char in "。！？；\n" or len(current) >= chunk_size:
            chunks.append(current)
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _status_timeline_block(items: list[dict[str, Any]], summary: str = "") -> dict | None:
    if not items:
        return None
    return {
        "type": "status_timeline",
        "payload": {
            "title": "推理进度",
            "summary": summary,
            "collapsed": len(items) > 3,
            "items": items,
        },
    }


def _latest_assistant_message_id(context) -> str | None:
    for message in reversed(context.messages):
        if message.get("role") == "assistant":
            return str(message.get("message_id") or "") or None
    return None


def _message_block_key(block: dict[str, Any]) -> tuple[str, str]:
    block_type = str(block.get("type") or "")
    payload = block.get("payload") if isinstance(block.get("payload"), dict) else {}
    if block_type == "fact_form":
        return block_type, str(payload.get("form_id") or "")
    return block_type, ""


def _append_message_blocks_to_latest_assistant(
    context,
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for message in reversed(context.messages):
        if message.get("role") != "assistant":
            continue
        existing = [item for item in message.get("blocks", []) if isinstance(item, dict)]
        # Native Skill forms are attached before stream finalization. Keep them
        # instead of replacing them with the generic missing-Facts form.
        if any(item.get("type") == "fact_form" for item in existing):
            blocks = [item for item in blocks if item.get("type") != "fact_form"]
        incoming_keys = {_message_block_key(item) for item in blocks}
        preserved = [item for item in existing if _message_block_key(item) not in incoming_keys]
        status_blocks = [item for item in blocks if item.get("type") == "status_timeline"]
        other_blocks = [item for item in blocks if item.get("type") != "status_timeline"]
        merged = [*status_blocks, *preserved, *other_blocks]
        message["blocks"] = merged
        metadata = dict(message.get("metadata") or {})
        metadata["blocks"] = merged
        message["metadata"] = metadata
        ensure_message_interactions(message)
        return merged
    return blocks


def _expert_state_payload(context, orchestrator, switch: dict[str, Any] | None = None) -> dict[str, Any]:
    team_id = str((context.session_meta or {}).get("expert_team_id") or "").strip()
    expert_id = str(
        (context.session_meta or {}).get("active_expert_id")
        or (context.session_meta or {}).get("expert_id")
        or ""
    ).strip()
    expert_registry = getattr(orchestrator, "expert_registry", None)
    definition = expert_registry.get(expert_id) if expert_id and expert_registry is not None else None
    active: dict[str, Any] = {}
    team_payload: dict[str, Any] = {}
    mode = "none"
    if team_id:
        team_registry = getattr(orchestrator, "expert_team_registry", None)
        team = team_registry.get(team_id) if team_registry is not None else None
        if team is not None:
            mode = "team"
            member = team.member_for_expert(expert_id)
            team_payload = {
                "team_id": team.team_id,
                "name": team.name,
                "coordinator_expert_id": team.coordinator_expert_id,
            }
            active = {
                "expert_id": expert_id,
                "name": definition.name if definition is not None else expert_id,
                "mention_name": member.mention_name if member is not None else "",
                "is_coordinator": expert_id == team.coordinator_expert_id,
            }
    elif definition is not None:
        mode = "single"
        active = {
            "expert_id": definition.agent_id,
            "name": definition.name,
            "mention_name": "",
            "is_coordinator": False,
        }
    transition: dict[str, Any] = {}
    if isinstance(switch, dict):
        transition = {
            "status": "completed",
            "source": str(switch.get("source") or ""),
            "from_expert_id": str(switch.get("from_expert_id") or ""),
            "to_expert_id": str(switch.get("target_expert_id") or expert_id),
            "source_message_id": switch.get("source_message_id"),
        }
    return {"mode": mode, "team": team_payload, "active": active, "transition": transition}


def _preactivate_requested_target_skill(
    context,
    requested_target_skill_id: str | None,
    *,
    runtime_registry=None,
    previous_skill_id_override: str | None = None,
) -> None:
    # Normalize the historical ``main_planner`` spelling at the API/runtime
    # boundary.  Persisted snapshots may still contain it, but all new turns
    # and SSE payloads must use the canonical career-planning Skill ID.
    requested = canonical_skill_id(requested_target_skill_id)
    if not requested:
        return
    if runtime_registry is not None and not runtime_registry.get(requested):
        return
    previous_runtime_state = context.skill_states.get(RUNTIME_STATE_KEY, {})
    previous_skill_id = ""
    if isinstance(previous_runtime_state, dict):
        previous_skill_id = str(previous_runtime_state.get("active_skill_id") or "").strip()
    previous_skill_id = previous_skill_id or str(context.interaction_state.get("active_skill") or "").strip()
    previous_skill_id = str(previous_skill_id_override or previous_skill_id or "").strip()
    context.session_meta["preactivated_requested_target_skill_id"] = requested
    context.session_meta["preactivated_previous_skill_id"] = previous_skill_id
    display = build_skill_display(context, active_skill=requested, runtime_registry=runtime_registry)
    runtime_state = context.skill_states.setdefault(RUNTIME_STATE_KEY, {})
    if not isinstance(runtime_state, dict):
        runtime_state = {}
        context.skill_states[RUNTIME_STATE_KEY] = runtime_state
    runtime_state["active_skill_id"] = requested
    runtime_state.setdefault("stage", "init")
    skill_facts = runtime_state.setdefault("skill_facts", {})
    if isinstance(skill_facts, dict):
        skill_facts.setdefault(requested, {})
    stage_facts = runtime_state.setdefault("stage_facts", {})
    if isinstance(stage_facts, dict):
        stage_facts.setdefault(requested, {}).setdefault(str(runtime_state.get("stage") or "init"), {})
    context.interaction_state["active_skill"] = requested
    if display.get("scene_name"):
        context.interaction_state["current_scenario"] = display["scene_name"]
    context.skill_states.setdefault(CAREER_PLAN_SKILL_ID, {}).update(
        {
            "target_skill": requested,
            "pending_requested_target_skill_id": requested,
        }
    )


def _reset_runtime_context_for_general_chat(context, *, transition_message_id: str) -> None:
    """Keep persisted history/Facts, but discard all pre-exit model state."""
    _reset_runtime_context_for_skill(
        context,
        target_skill_id="general_chat",
        transition_message_id=transition_message_id,
    )


def _promote_exit_memory_global_facts(context, memory_context: dict[str, Any], *, source_skill: str) -> list[str]:
    """Persist missing cross-Skill facts extracted during exit compaction.

    Memory remains a continuity aid, while the session Fact layer is the
    authoritative source rebuilt into every freshly entered Skill. Never
    overwrite a value already present in the three persistent Fact scopes.
    """
    facts = memory_context.get("facts") if isinstance(memory_context, dict) else {}
    global_facts = facts.get("global") if isinstance(facts, dict) else {}
    if not isinstance(global_facts, dict):
        return []
    promoted: list[str] = []
    for key, value in global_facts.items():
        if value in (None, "", [], {}):
            continue
        if context.known_facts.get_value(str(key)) not in (None, "", [], {}):
            continue
        context.update_fact(
            str(key),
            value,
            source_skill=source_skill,
            confidence=0.7,
            source_type="conversation_memory",
            source_id="skill_exit",
            scope="session",
        )
        promoted.append(str(key))
    return promoted


def _reset_runtime_context_for_skill(
    context,
    *,
    target_skill_id: str,
    transition_message_id: str,
) -> None:
    """Reset model state while retaining durable history and three-layer Facts."""
    context.refresh_effective_facts()
    runtime_facts = {
        key: record.value
        for key, record in context.known_facts.facts.items()
        if record.value not in (None, "", [], {})
    }
    context.skill_states = {
        RUNTIME_STATE_KEY: {
            "session_id": context.session_id,
            "stage": "init",
            "collected_info": {},
            "active_skill_id": target_skill_id,
            "global_facts": runtime_facts,
            "skill_facts": {target_skill_id: {}},
            "stage_facts": {target_skill_id: {"init": {}}},
            "status_flags": {},
            "route_history": [],
            "conversation_memory": {},
        },
        CAREER_PLAN_SKILL_ID: {
            "target_skill": target_skill_id,
            "stage": "init",
            "status_flags": {},
            "route_history_count": 0,
            "intent_route": {},
        },
    }
    context.skill_states["main_planner"] = context.skill_states[CAREER_PLAN_SKILL_ID]
    context.interaction_state = {"active_skill": target_skill_id}
    context.candidate_paths = []
    context.risk_signals = []
    context.session_meta["runtime_context_start_message_id"] = transition_message_id
    for key in (
        "skill_intro",
        "requested_target_skill_id",
        "handoff_context",
        "preactivated_requested_target_skill_id",
        "preactivated_previous_skill_id",
        "hide_next_user_message",
    ):
        context.session_meta.pop(key, None)


def _transition_facts_snapshot(context) -> dict[str, Any]:
    """Build the server-authoritative Facts bundle for a Skill handoff."""
    return {
        "shared_facts": serialize_known_facts(context.shared_facts),
        "profile_facts": serialize_known_facts(context.profile_facts),
        "session_facts": serialize_known_facts(context.session_facts),
        "effective_facts": serialize_known_facts(context.known_facts),
    }


def _message_context_for_transition(
    context,
    *,
    source_message: dict[str, Any] | None,
    target_skill_id: str,
) -> tuple[dict[str, Any], list[str]]:
    """Return only the source reply and its immediately preceding user turn."""
    source_message = source_message or {}
    source_id = str(source_message.get("message_id") or "")
    message_ids: list[str] = []
    previous_user: dict[str, Any] | None = None
    source_index = next(
        (
            index
            for index, item in enumerate(context.messages)
            if item is source_message
            or str(item.get("message_id") or "") == source_id
        ),
        -1,
    )
    if source_index >= 0:
        for item in reversed(context.messages[:source_index]):
            item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if item.get("role") == "user" and not item_metadata.get("hidden"):
                previous_user = item
                break
    if previous_user:
        message_ids.append(str(previous_user.get("message_id") or ""))
    if source_id:
        message_ids.append(source_id)
    metadata = source_message.get("metadata") if isinstance(source_message.get("metadata"), dict) else {}
    compression = source_message.get("context_compression")
    if not isinstance(compression, dict):
        compression = metadata.get("context_compression") if isinstance(metadata.get("context_compression"), dict) else {}
    suggestions = source_message.get("route_suggestions")
    if not isinstance(suggestions, list):
        suggestions = metadata.get("route_suggestions") if isinstance(metadata.get("route_suggestions"), list) else []
    selected = next(
        (
            item for item in suggestions
            if isinstance(item, dict) and str(item.get("target_skill_id") or "") == target_skill_id
        ),
        None,
    )
    return (
        {
            "source_message_id": source_id,
            "source_user_message": str((previous_user or {}).get("content") or ""),
            "source_assistant_message": str(source_message.get("content") or ""),
            "context_compression": compression,
            "route_suggestion": selected or {},
        },
        [item for item in message_ids if item],
    )


def _public_transition(transition: dict[str, Any]) -> dict[str, Any]:
    """Remove server-only Facts/context payloads from history and SSE."""
    return {
        key: value
        for key, value in transition.items()
        if key not in {"facts_snapshot", "handoff_context", "context_message_ids"}
    }


def _transition_skill_payload(display: dict[str, Any]) -> dict[str, str]:
    return {
        "skill_id": str(display.get("skill_id") or ""),
        "name": str(display.get("skill_name") or ""),
        "label": str(display.get("agent_label") or ""),
        "brief": str(display.get("brief") or ""),
        "info": str(display.get("info") or ""),
        "description": str(display.get("description") or ""),
        "scene_name": str(display.get("scene_name") or ""),
        "skill_theme": str(display.get("skill_theme") or ""),
    }


class StreamingRunner:
    def __init__(
        self,
        repository: InMemorySessionRepository,
        fact_service: FactService,
        orchestrator,
        turn_coordinator: TurnCoordinator | None = None,
    ) -> None:
        self.repository = repository
        self.fact_service = fact_service
        self.orchestrator = orchestrator
        self.turn_coordinator = turn_coordinator or TurnCoordinator()
        self._executor = ThreadPoolExecutor(
            max_workers=int(__import__("os").getenv("HAILIANG_STREAM_WORKERS", "100")),
            thread_name_prefix="hailiang-sse",
        )
        self._active_stream_queues: dict[tuple[str, str], Queue] = {}
        self._active_stream_queues_lock = threading.Lock()

    def reserve_turn(self, session_id: str, user_id: str, *, run_id: str | None = None) -> TurnLease:
        return self.turn_coordinator.acquire(session_id, user_id, run_id=run_id)

    def cancel_run(self, session_id: str, user_id: str, run_id: str) -> bool:
        """Cancel only the active generation for this session/user pair."""
        context = self.repository.get(session_id)
        if str(context.user_id or "") != str(user_id or ""):
            return False
        cancelled = self.turn_coordinator.cancel(session_id, run_id)
        if cancelled:
            # The runtime bridge checks this marker while model tokens are
            # flowing. Set it here instead of waiting for the SSE heartbeat.
            context.session_meta["cancelled_stream_generation"] = run_id
            # Wake the original SSE response instead of waiting for its
            # heartbeat timeout. This also makes the stop endpoint effective
            # when the upstream model is currently between tokens.
            with self._active_stream_queues_lock:
                queue = self._active_stream_queues.get((session_id, run_id))
            if queue is not None:
                try:
                    queue.put_nowait(_CANCEL_WAKEUP)
                except Full:
                    # A slow downstream must not prevent cancellation from
                    # reaching the response iterator. Drop only buffered,
                    # not-yet-sent events and prioritize the stop marker.
                    try:
                        while True:
                            queue.get_nowait()
                    except Empty:
                        pass
                    try:
                        queue.put_nowait(_CANCEL_WAKEUP)
                    except Full:
                        pass
                except Exception:
                    pass
        return cancelled

    def supersede_active_run(self, session_id: str, user_id: str, *, next_run_id: str) -> str | None:
        """Record that a newer user action has replaced the active generation."""
        previous = self.turn_coordinator.supersede(session_id, next_run_id=next_run_id)
        if not previous:
            return None
        for _ in range(3):
            context = self.repository.get(session_id)
            if str(context.user_id or "") != str(user_id or ""):
                return None
            ledger = context.session_meta.setdefault("run_ledger", {})
            if isinstance(ledger, dict) and isinstance(ledger.get(previous), dict):
                ledger[previous]["status"] = "superseded"
            context.session_meta["superseded_run_id"] = previous
            try:
                self.repository.save(context)
                break
            except SessionVersionConflict:
                continue
        return previous

    def stream_stop(self, session_id: str, user_id: str, *, run_id: str, source_endpoint: str) -> Iterator[str]:
        """Stop an active run and return its immediate v2 terminal snapshot."""
        context = self.repository.get(session_id)
        if str(context.user_id or "") != str(user_id or "") or not self.cancel_run(session_id, user_id, run_id):
            raise RuntimeError("RUN_NOT_ACTIVE")
        snapshots = context.session_meta.get("sse_v2_runs")
        snapshot = snapshots.get(run_id) if isinstance(snapshots, dict) else None
        builder = SseEnvelopeBuilder(run_id=run_id, session_id=session_id)
        if isinstance(snapshot, dict):
            builder.restore(snapshot)
        raw = self._encode_and_record_sse(
            builder=builder,
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            source_endpoint=source_endpoint,
            internal_event="run_cancelled",
            data={"session_id": session_id, "run_id": run_id},
        )
        snapshots = context.session_meta.setdefault("sse_v2_runs", {})
        if isinstance(snapshots, dict):
            snapshots[run_id] = builder.snapshot()
        ledger = context.session_meta.setdefault("run_ledger", {})
        if isinstance(ledger, dict):
            item = ledger.setdefault(run_id, {})
            if isinstance(item, dict):
                item["status"] = "stopped"
        self.repository.save(context)
        if raw:
            yield raw

    def _encode_and_record_sse(
        self,
        *,
        builder: SseEnvelopeBuilder,
        session_id: str,
        run_id: str,
        user_id: str,
        source_endpoint: str,
        internal_event: str,
        data: dict[str, Any],
    ) -> str | None:
        raw_sse, metadata = builder.encode_with_metadata(internal_event, data)
        if raw_sse is None:
            return None
        try:
            append_sse_record(
                session_id,
                run_id,
                {
                    "internal_event": internal_event,
                    "wire_event": metadata.get("wire_event"),
                    "raw_sse": raw_sse,
                    "payload": metadata.get("payload"),
                    "protocol": metadata.get("protocol"),
                    "seq": metadata.get("seq"),
                    "ts": metadata.get("ts"),
                    "message_id": metadata.get("message_id"),
                    "source_endpoint": source_endpoint,
                },
                user_id=user_id,
            )
        except Exception:
            # Observability writes must never affect the streaming path.
            pass
        return raw_sse

    def stream_message(
        self,
        session_id: str,
        user_id: str,
        content: str,
        *,
        enable_thinking: bool = False,
        return_reasoning: bool = False,
        requested_target_skill_id: str | None = None,
        handoff_context: dict[str, Any] | None = None,
        team_member_switch: dict[str, Any] | None = None,
        lease: TurnLease | None = None,
        protocol: str = "legacy",
        source_endpoint: str = "sessions/chat/stream",
        initial_events: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> Iterator[str]:
        lease = lease or self.reserve_turn(session_id, user_id)
        parent_telemetry = current_telemetry()
        context = self.repository.get(session_id)
        context.user_id = user_id
        if isinstance(team_member_switch, dict):
            # Preserve the structured switch across this repository reload.
            # Expert routing never depends on parsing the visible user text.
            context.session_meta["team_member_switch"] = dict(team_member_switch)
        if context.profile_id:
            try:
                profile = self.fact_service.profile_repo.get_profile(user_id, context.profile_id)
                context.profile_name = str(profile.get("name") or context.profile_name or "")
            except KeyError:
                pass
        self.fact_service.hydrate_context(context)
        before_facts = serialize_known_facts(context.known_facts)
        queue: Queue = Queue(maxsize=int(__import__("os").getenv("HAILIANG_SSE_EVENT_QUEUE_SIZE", "512")))
        sse_push_lock = threading.Lock()
        status_items: list[dict[str, Any]] = []
        stable_status_labels: dict[str, str] = {}
        status_summary = ""
        stream_generation = lease.generation
        stream_queue_key = (session_id, stream_generation)
        with self._active_stream_queues_lock:
            self._active_stream_queues[stream_queue_key] = queue
        protocol = normalize_protocol(protocol)
        envelope_builder = SseEnvelopeBuilder(
            run_id=stream_generation,
            session_id=session_id,
            protocol=protocol,
        )
        context.session_meta["active_stream_generation"] = stream_generation
        context.session_meta.pop("cancelled_stream_generation", None)
        context.session_meta["stream_cancel_check"] = lambda: self.turn_coordinator.is_cancelled(lease)
        generation_by_thread = context.session_meta.setdefault("stream_generation_by_thread", {})
        if not isinstance(generation_by_thread, dict):
            generation_by_thread = {}
            context.session_meta["stream_generation_by_thread"] = generation_by_thread
        _preactivate_requested_target_skill(
            context,
            requested_target_skill_id,
            runtime_registry=getattr(self.orchestrator, "runtime_registry", None),
            previous_skill_id_override=(
                ((handoff_context or {}).get("skill_transition") or {}).get("from_skill_id")
                if isinstance(handoff_context, dict)
                else None
            ),
        )

        def push(event: str, data: dict[str, Any]) -> None:
            if not is_current_stream():
                return
            with sse_push_lock:
                try:
                    encoded = self._encode_and_record_sse(
                        builder=envelope_builder,
                        session_id=session_id,
                        run_id=stream_generation,
                        user_id=user_id,
                        source_endpoint=source_endpoint,
                        internal_event=event,
                        data=data,
                    )
                    if encoded is None:
                        return
                    snapshots = context.session_meta.setdefault("sse_v2_runs", {})
                    if isinstance(snapshots, dict):
                        snapshots[stream_generation] = envelope_builder.snapshot()
                    if event in {"run_completed", "run_cancelled", "run_superseded", "run_failed"}:
                        ledger = context.session_meta.setdefault("run_ledger", {})
                        if isinstance(ledger, dict):
                            item = ledger.setdefault(stream_generation, {})
                            if isinstance(item, dict):
                                item["status"] = str(data.get("status") or {
                                    "run_cancelled": "stopped",
                                    "run_superseded": "superseded",
                                    "run_failed": "failed",
                                }.get(event, "completed"))
                    queue.put(encoded, timeout=1)
                except Exception:
                    # A disconnected/slow client must not let unbounded events grow.
                    context.session_meta["active_stream_generation"] = "client_backpressure"

        def is_current_stream() -> bool:
            return (
                context.session_meta.get("active_stream_generation") == stream_generation
                and self.turn_coordinator.is_current(lease)
            )

        cancelled_lock = threading.Lock()
        cancelled_finalized = False
        progress_stop = threading.Event()
        first_text_seen = threading.Event()
        progress_started_at = __import__("time").monotonic()
        progress_config = getattr(self.orchestrator, "runtime_bridge_config", None)
        progress_simulation_enabled = progress_config is not None and bool(
            getattr(progress_config, "progress_simulation_enabled", True)
        )
        progress_simulation_interval_s = float(
            getattr(progress_config, "progress_simulation_interval_s", 0.45)
        )
        progress_simulation_jitter_s = float(
            getattr(progress_config, "progress_simulation_jitter_s", 0.0)
        )
        progress_simulation_min_duration_s = float(
            getattr(progress_config, "progress_simulation_min_duration_s", 1.2)
        )
        summary_emitted = False
        progress_completed_emitted = False
        summary_lock = threading.Lock()

        def finalize_cancelled_response() -> None:
            """Persist what the browser has already received, exactly once."""
            nonlocal cancelled_finalized
            with cancelled_lock:
                if cancelled_finalized:
                    return
                cancelled_finalized = True
            progress_stop.set()
            context.session_meta["cancelled_stream_generation"] = stream_generation
            active_turn_id = ""
            turn_by_generation = context.session_meta.get("turn_id_by_stream_generation")
            if isinstance(turn_by_generation, dict):
                active_turn_id = str(turn_by_generation.get(stream_generation) or "")
            partial = "".join(
                str(item) for item in (context.session_meta.get("streamed_reply_parts") or [])
            )
            # A synchronous/runtime worker may already have appended its final
            # assistant message.  Never retain that unreviewed tail after the
            # user explicitly stopped generation.
            if active_turn_id:
                prune_stale_assistant_turn(active_turn_id)
            message_id = ""
            if partial:
                context.add_message(
                    "assistant",
                    partial,
                    metadata={
                        "run_id": stream_generation,
                        "generation_status": "cancelled",
                        "is_complete": False,
                        "cancelled_at": utc_now_iso(),
                    },
                )
                message = context.messages[-1]
                message["generation_status"] = "cancelled"
                message["is_complete"] = False
                message["cancelled_at"] = message["metadata"]["cancelled_at"]
                message_id = str(message.get("message_id") or "")
            clear_runtime_callbacks()
            self.repository.save(context)
            cancelled_at = utc_now_iso()
            push(
                "run_cancelled",
                {
                    "session_id": session_id,
                    "run_id": stream_generation,
                    "message_id": message_id,
                    "saved_characters": len(partial),
                    "cancelled_at": cancelled_at,
                },
            )
            push(
                "run_completed",
                {
                    "session_id": session_id,
                    "run_id": stream_generation,
                    "status": "cancelled",
                    "finish_reason": "cancelled",
                    "message_id": message_id,
                    "saved_characters": len(partial),
                    "cancelled_at": cancelled_at,
                    "conversation_state": get_conversation_state(context),
                },
            )

        def prune_stale_assistant_turn(turn_id: str) -> None:
            if not turn_id:
                return
            context.messages = [
                message
                for message in context.messages
                if not (
                    message.get("role") == "assistant"
                    and isinstance(message.get("metadata"), dict)
                    and message["metadata"].get("turn_id") == turn_id
                )
            ]

        def push_reply_delta(chunk: str) -> None:
            if self.turn_coordinator.is_cancelled(lease):
                return
            if not emit_summary_before_body():
                return
            streamed = context.session_meta.setdefault("streamed_reply_parts", [])
            for part in _split_reply_delta(chunk):
                first_text_seen.set()
                if isinstance(streamed, list):
                    streamed.append(part)
                # Text is emitted as the model produces it.  Reasoning remains
                # buffered below so the UI can animate it independently.
                push("final_text_delta", {"delta": part})

        def emit_summary_before_body() -> bool:
            nonlocal progress_completed_emitted, summary_emitted
            if protocol != UNIFIED_PROTOCOL:
                return True
            with summary_lock:
                if not is_current_stream() or self.turn_coordinator.is_cancelled(lease):
                    return False
                if progress_simulation_enabled and not summary_emitted:
                    elapsed = __import__("time").monotonic() - progress_started_at
                    remaining = progress_simulation_min_duration_s - elapsed
                    if remaining > 0 and progress_stop.wait(remaining):
                        return False
                    if not is_current_stream() or self.turn_coordinator.is_cancelled(lease):
                        return False
                    push_status(
                        {
                            "stage": "progress_summary",
                            "label": "正在总结信息",
                            "detail": "整理本轮信息并准备输出正文",
                            "source": "server_progress",
                        }
                    )
                    summary_emitted = True
                if not progress_completed_emitted:
                    push("progress_completed", {"status": "completed"})
                    progress_completed_emitted = True
                progress_stop.set()
                return True

        def push_reasoning_delta(chunk: str) -> None:
            if protocol == UNIFIED_PROTOCOL:
                return
            streamed = context.session_meta.setdefault("streamed_reasoning_parts", [])
            if isinstance(streamed, list):
                streamed.append(chunk)

        def push_security(payload: dict[str, Any]) -> None:
            push("security", payload)

        def push_model_error(exc: BaseException, *, terminal: bool) -> None:
            payload = public_model_error(exc, terminal=terminal)
            push("model_error", {"error": payload})
            if hasattr(self.orchestrator, "_record_events"):
                self.orchestrator._record_events(
                    context,
                    [make_event("model_error", payload)],
                )

        def emit_buffered_output() -> None:
            if protocol == UNIFIED_PROTOCOL:
                return
            for part in context.session_meta.get("streamed_reasoning_parts", []) or []:
                push("reasoning_delta", {"delta": part})

        def push_status(payload: dict[str, Any]) -> None:
            nonlocal status_summary
            label = normalize_status_label(payload.get("label"))
            if not label:
                return
            stage = str(payload.get("stage") or "")
            label = stable_status_labels.setdefault(stage, label)
            item = {
                "stage": stage,
                "label": label,
                "detail": payload.get("detail") if isinstance(payload.get("detail"), str) else None,
                "summary": payload.get("summary") if isinstance(payload.get("summary"), str) else None,
                "source": payload.get("source") if isinstance(payload.get("source"), str) else None,
                "status": "active",
            }
            if item["summary"]:
                status_summary = str(item["summary"])
            next_item = {key: value for key, value in item.items() if value not in (None, "")}
            existing_index = next(
                (index for index, existing in enumerate(status_items) if existing.get("stage") == item["stage"]),
                -1,
            )
            # A stage id represents one stable UI step. Model-generated
            # details may arrive later, but must not rename that step between
            # its active and completed states.
            if existing_index >= 0:
                stable_label = str(status_items[existing_index].get("label") or label)
                item["label"] = stable_label
                next_item["label"] = stable_label
                label = stable_label
            duplicate_index = next(
                (index for index, existing in enumerate(status_items) if existing.get("label") == item["label"]),
                -1,
            )
            if duplicate_index >= 0 and duplicate_index != existing_index:
                return
            if existing_index >= 0:
                for index, existing in enumerate(status_items):
                    if index < existing_index and existing.get("status") == "active":
                        existing["status"] = "completed"
                status_items[existing_index] = {
                    **status_items[existing_index],
                    **next_item,
                    "status": "active",
                }
            else:
                for existing in status_items:
                    if existing.get("status") == "active":
                        existing["status"] = "completed"
                status_items.append(next_item)
            # Forward actual ms-agent statuses immediately so the browser sees
            # the same steps that are later persisted in the final timeline.
            push("skill_status", {**payload, "label": label})

        def emit_simulated_progress() -> None:
            if not progress_simulation_enabled:
                return
            random_source = random.SystemRandom()
            simulated_steps = (
                ("progress_facts", "正在核对关键信息"),
                ("progress_materials", "正在整理相关资料"),
            )
            for stage, label in simulated_steps:
                lower_bound = max(0.1, progress_simulation_interval_s - progress_simulation_jitter_s)
                upper_bound = max(lower_bound, progress_simulation_interval_s + progress_simulation_jitter_s)
                delay_s = random_source.uniform(lower_bound, upper_bound)
                if progress_stop.wait(delay_s):
                    return
                if not is_current_stream():
                    return
                push_status(
                    {
                        "stage": stage,
                        "label": label,
                        "detail": "服务端模拟推理进度",
                        "source": "server_progress",
                    }
                )

        def push_skill_intro(payload: dict[str, Any]) -> None:
            push("skill_intro", payload)
            context.session_meta["skill_intro_emitted"] = True

        def clear_runtime_callbacks() -> None:
            if not is_current_stream():
                return
            context.session_meta.pop("stream_final_reply", None)
            context.session_meta.pop("status_callback", None)
            context.session_meta.pop("lifecycle_callback", None)
            context.session_meta.pop("skill_intro_callback", None)
            context.session_meta.pop("reply_delta_callback", None)
            context.session_meta.pop("reasoning_delta_callback", None)
            context.session_meta.pop("security_callback", None)
            context.session_meta.pop("model_error_callback", None)
            context.session_meta.pop("team_handoff_callback", None)
            context.session_meta.pop("stream_cancel_check", None)
            context.session_meta.pop("streamed_reply_parts", None)
            context.session_meta.pop("streamed_reasoning_parts", None)
            # These are per-transition runtime hints. They must never become
            # durable conversation state or affect the next user turn.
            context.session_meta.pop("include_internal_transition_turn", None)
            context.session_meta.pop("transition_context_message_ids", None)
            context.session_meta.pop("transition_context_mode", None)

        def run() -> None:
            telemetry_token = None
            if parent_telemetry:
                _, telemetry_token = bind_telemetry(
                    request_id=parent_telemetry.request_id,
                    trace_id=parent_telemetry.trace_id,
                    span_id=parent_telemetry.span_id,
                    route=parent_telemetry.route,
                    session_id=session_id,
                    run_id=stream_generation,
                    user_id=user_id,
                    profile_id=str(context.profile_id or ""),
                )
            main_content_sent = False
            thread_generation_key = str(threading.get_ident())
            generation_by_thread[thread_generation_key] = stream_generation
            try:
                context.session_meta["stream_final_reply"] = True
                context.session_meta["enable_thinking"] = enable_thinking
                context.session_meta["return_reasoning"] = return_reasoning
                if requested_target_skill_id:
                    context.session_meta["requested_target_skill_id"] = requested_target_skill_id
                    context.session_meta["handoff_context"] = handoff_context or {}
                    if isinstance(handoff_context, dict):
                        context.session_meta["transition_context_mode"] = str(
                            handoff_context.get("context_mode") or "message_context"
                        )
                        context.session_meta["include_internal_transition_turn"] = True
                        context.session_meta["transition_context_message_ids"] = list(
                            handoff_context.get("context_message_ids") or []
                        )
                context.session_meta["status_callback"] = push_status
                context.session_meta["lifecycle_callback"] = lambda payload: push(
                    "skill_lifecycle", payload
                )
                context.session_meta["skill_intro_callback"] = push_skill_intro
                context.session_meta["reply_delta_callback"] = push_reply_delta
                context.session_meta["reasoning_delta_callback"] = push_reasoning_delta
                context.session_meta["security_callback"] = push_security
                context.session_meta["model_error_callback"] = push_model_error
                context.session_meta["team_handoff_callback"] = lambda payload: push(
                    "team_handoff", payload
                )
                context.session_meta["streamed_reply_parts"] = []
                context.session_meta["streamed_reasoning_parts"] = []
                context.session_meta["skill_intro_emitted"] = False
                with span("orchestrator.handle_message", node="orchestrator", skill_id=str(requested_target_skill_id or "")):
                    result = self.orchestrator.handle_message(content, context)
                invitation = build_skill_invitation(
                    context,
                    assistant_message=result.assistant_message,
                    runtime_registry=getattr(self.orchestrator, "runtime_registry", None),
                    # Route thresholds are owned by config/intent_router.yml.
                    # The legacy LLM setting remains only as a compatibility
                    # fallback for callers that invoke lifecycle helpers directly.
                )
                if invitation:
                    result.assistant_message += invitation
                    push_reply_delta(invitation)
                    for message in reversed(context.messages):
                        if message.get("role") == "assistant":
                            message["content"] = result.assistant_message
                            break
                turn_by_generation = context.session_meta.get("turn_id_by_stream_generation")
                active_turn_id = (
                    str(turn_by_generation.get(stream_generation) or "")
                    if isinstance(turn_by_generation, dict)
                    else ""
                )
                if not is_current_stream():
                    prune_stale_assistant_turn(active_turn_id)
                    if hasattr(self.orchestrator, "_record_events"):
                        self.orchestrator._record_events(
                            context,
                            [
                                make_event(
                                    "stream_interrupted",
                                    {
                                        "reason": "superseded_by_new_user_message",
                                        "stream_generation": stream_generation,
                                        "active_stream_generation": context.session_meta.get(
                                            "active_stream_generation"
                                        ),
                                        "turn_id": active_turn_id,
                                    },
                                )
                            ],
                        )
                    return
                if self.turn_coordinator.is_cancelled(lease):
                    finalize_cancelled_response()
                    return
                moderation_service = getattr(self.orchestrator, "moderation_service", None)
                if moderation_service is not None:
                    try:
                        push_security({"status": "checking", "stage": "output"})
                        moderation_result = moderation_service.check(
                            result.assistant_message,
                            stage="output",
                            trace_id=str(context.session_meta.get("trace_id") or ""),
                            session_id=str(context.session_id),
                            turn_id=active_turn_id,
                        )
                        context.session_meta["moderation_mode"] = moderation_result.mode
                        context.session_meta["semantic_moderation_unavailable"] = moderation_result.mode == "local_fallback"
                        record_security_result(context, stage="output", result=moderation_result)
                        if moderation_result.mode == "local_fallback" and hasattr(self.orchestrator, "_record_events"):
                            self.orchestrator._record_events(
                                context,
                                [make_event("security_degraded", {"stage": "output", **moderation_result.to_public_dict()})],
                            )
                    except ModerationBlockedError as exc:
                        context.session_meta["moderation_mode"] = exc.result.mode
                        context.session_meta["semantic_moderation_unavailable"] = exc.result.mode == "local_fallback"
                        record_security_result(context, stage="output", result=exc.result, case_id=exc.case_id)
                        if hasattr(self.orchestrator, "_record_events"):
                            self.orchestrator._record_events(
                                context,
                                [make_event("moderation_blocked", {"stage": "output", "case_id": exc.case_id, **exc.result.to_public_dict()})],
                            )
                        raise
                if context.session_meta.get("skill_intro") and not context.session_meta.get("skill_intro_emitted"):
                    push_skill_intro(dict(context.session_meta["skill_intro"]))
                if protocol == UNIFIED_PROTOCOL:
                    if not emit_summary_before_body():
                        finalize_cancelled_response()
                        return
                    progress_stop.set()
                emit_buffered_output()
                main_content_sent = True
                push(
                    "main_content_end",
                    {
                        "type": "main_content_end",
                        "session_id": context.session_id,
                        "message_id": _latest_assistant_message_id(context),
                        "assistant_message": result.assistant_message,
                    },
                )
                if not is_current_stream():
                    prune_stale_assistant_turn(active_turn_id)
                    return
                with span("stream.post_process", node="stream_post_process"):
                    self.fact_service.persist_context(context)
                try:
                    after_facts = serialize_known_facts(context.known_facts)
                    fact_changes = _diff_facts(before_facts, after_facts)
                    if fact_changes:
                        push("fact_changes", {"changes": fact_changes})
                    skill_display = build_skill_display(
                        context,
                        runtime_registry=getattr(self.orchestrator, "runtime_registry", None),
                    )
                    active_skill = skill_display["skill_id"]
                    finalized_payload, finalized_events = build_finalized_payload(
                        context,
                        assistant_message=result.assistant_message,
                        facts_delta=fact_changes,
                        runtime_registry=getattr(self.orchestrator, "runtime_registry", None),
                        route_suggestion_client=(
                            self.orchestrator.route_suggestion_client_for_context(context)
                            if callable(getattr(self.orchestrator, "route_suggestion_client_for_context", None))
                            else getattr(
                                self.orchestrator,
                                "route_suggestion_client",
                                getattr(self.orchestrator, "runtime_client", None),
                            )
                        ),
                        monitor_route_suggestions_every_turn=bool(
                            getattr(self.orchestrator, "route_suggestion_monitor_every_turn", False)
                        ),
                    )
                    # Finalization may use an auxiliary model and report a
                    # non-terminal error through the callback.  Clear the
                    # transient callbacks before serializing the context.
                    clear_runtime_callbacks()
                    attach_finalization_metadata(context, finalized_payload)
                    exposure_event = build_route_suggestions_exposed_event(
                        context,
                        finalized_payload,
                        finalized_events,
                        run_id=stream_generation,
                    )
                    if exposure_event:
                        finalized_events.append(exposure_event)
                    if finalized_events and hasattr(self.orchestrator, "_record_events"):
                        self.orchestrator._record_events(context, finalized_events)
                    if (
                        finalized_payload.get("is_final_summary") is True
                        or finalized_payload.get("route_suggestions")
                    ):
                        push("skill_lifecycle", finalized_payload)
                    for item in status_items:
                        if item.get("status") == "active":
                            item["status"] = "completed"
                    message_blocks = [
                        block
                        for block in [
                            _status_timeline_block(status_items, status_summary),
                            build_missing_fact_form_block(
                                context.skill_states.get("planner", {}).get("missing_facts", [])
                                or []
                            ),
                            build_path_actions_block(result.suggested_paths or []),
                            build_citations_block(context, active_skill=active_skill),
                        ]
                        if block
                    ]
                    message_blocks = _append_message_blocks_to_latest_assistant(context, message_blocks)
                    team_handoff = {}
                    for message in reversed(context.messages):
                        if message.get("role") != "assistant":
                            continue
                        raw_handoff = message.get("team_handoff")
                        if not isinstance(raw_handoff, dict):
                            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
                            raw_handoff = metadata.get("team_handoff")
                        team_handoff = raw_handoff if isinstance(raw_handoff, dict) else {}
                        break
                    if not is_current_stream():
                        prune_stale_assistant_turn(active_turn_id)
                        return
                    with span("session.snapshot.persist", node="session_persist"):
                        self.repository.save(context)
                    push(
                        "skill_action",
                        {
                            "type": "skill_action",
                            "session_id": context.session_id,
                            "message_blocks": message_blocks,
                            "active_skill": active_skill,
                            "active_skill_label": skill_display["active_skill_label"],
                            "agent_label": skill_display["agent_label"],
                            "skill_brief": skill_display.get("brief", ""),
                            "skill_info": skill_display.get("info", ""),
                            "scene_name": skill_display["scene_name"],
                            "skill_theme": skill_display["skill_theme"],
                            "conclusion_summary": finalized_payload["conclusion_summary"],
                            "context_compression": finalized_payload["context_compression"],
                            "route_suggestions": finalized_payload["route_suggestions"],
                            "is_final_summary": finalized_payload.get("is_final_summary") is True,
                            "team_handoff": team_handoff,
                        },
                    )
                    for message in reversed(context.messages):
                        if message.get("role") == "assistant":
                            message["presentation"] = envelope_builder.presentation()
                            metadata = message.setdefault("metadata", {})
                            if isinstance(metadata, dict):
                                metadata["presentation"] = message["presentation"]
                            break
                    self.repository.save(context)
                    for block in message_blocks:
                        push("message_block", block)
                    last_reasoning = (
                        context.skill_states.get(CAREER_PLAN_SKILL_ID, {}).get("last_reasoning") or ""
                    )
                    push(
                        "final_message",
                        {
                            "assistant_message": result.assistant_message,
                            "skill_intro": context.session_meta.get("skill_intro"),
                            "message_id": _latest_assistant_message_id(context),
                            "reasoning": last_reasoning,
                            "message_blocks": message_blocks,
                            "active_skill": active_skill,
                            "active_skill_label": skill_display["active_skill_label"],
                            "agent_label": skill_display["agent_label"],
                            "skill_brief": skill_display.get("brief", ""),
                            "skill_info": skill_display.get("info", ""),
                            "scene_name": skill_display["scene_name"],
                            "skill_theme": skill_display["skill_theme"],
                            "conclusion_summary": finalized_payload["conclusion_summary"],
                            "context_compression": finalized_payload["context_compression"],
                            "route_suggestions": finalized_payload["route_suggestions"],
                            "team_handoff": team_handoff,
                            "user_facts": serialize_known_facts(context.user_facts),
                            "shared_facts": serialize_known_facts(context.shared_facts),
                            "profile_facts": serialize_known_facts(context.profile_facts),
                            "session_facts": serialize_known_facts(context.session_facts),
                            "effective_facts": serialize_known_facts(context.known_facts),
                            "profile_id": context.profile_id,
                            "profile_name": context.profile_name,
                            "candidate_paths_brief": context.candidate_paths[:5],
                            "suggested_paths": result.suggested_paths,
                            "router_state": context.skill_states.get("router", {}),
                            "facts_extractor_state": context.skill_states.get(
                                "facts_extractor", {}
                            ),
                            "planner_state": context.skill_states.get("planner", {}),
                            "career_plan_state": context.skill_states.get(CAREER_PLAN_SKILL_ID, {}),
                            "main_planner_state": context.skill_states.get(CAREER_PLAN_SKILL_ID, {}),
                            "conversation_state": get_conversation_state(context),
                        },
                    )
                except Exception as exc:  # pragma: no cover - post-processing safety net
                    if hasattr(self.orchestrator, "_record_events"):
                        self.orchestrator._record_events(
                            context,
                            [
                                make_event(
                                    "stream_post_processing_failed",
                                    {"error": str(exc)},
                                )
                            ],
                        )
                push("run_completed", {"session_id": context.session_id, "conversation_state": get_conversation_state(context)})
                self.repository.save(context)
            except ModerationBlockedError as exc:
                prune_stale_assistant_turn(active_turn_id if "active_turn_id" in locals() else "")
                # Persist the latest input/output moderation result even when
                # no assistant message can be finalized for this turn.
                self.repository.save(context)
                push(
                    "moderation_blocked",
                    {
                        "code": "CONTENT_BLOCKED",
                        "message": "该内容检测到非合规内容，当前对话中断，如果需要请重新输入",
                        "stage": exc.stage,
                        "case_id": exc.case_id,
                        **exc.result.to_public_dict(),
                        "security": get_conversation_state(context)["security"],
                    },
                )
                push(
                    "run_completed",
                    {
                        "session_id": context.session_id,
                        "status": "blocked",
                        "finish_reason": "blocked",
                        "security_blocked": True,
                        "conversation_state": get_conversation_state(context),
                    },
                )
            except Exception as exc:  # pragma: no cover - streamed error path
                # Keep an already-completed input moderation result available
                # to history recovery even if a later runtime/model step fails.
                self.repository.save(context)
                error = public_model_error(exc, terminal=True)
                push("run_failed", {"message": error["message"], "error": error})
                if protocol == UNIFIED_PROTOCOL:
                    push(
                        "run_completed",
                        {
                            "session_id": context.session_id,
                            "status": "failed",
                            "finish_reason": "error",
                            "error": error,
                            "conversation_state": get_conversation_state(context),
                        },
                    )
            finally:
                progress_stop.set()
                clear_runtime_callbacks()
                generation_by_thread.pop(thread_generation_key, None)
                turn_by_generation = context.session_meta.get("turn_id_by_stream_generation")
                if isinstance(turn_by_generation, dict):
                    turn_by_generation.pop(stream_generation, None)
                self.turn_coordinator.release(lease)
                if SSE_ACTIVE:
                    SSE_ACTIVE.dec()
                if telemetry_token is not None:
                    reset_telemetry(telemetry_token)
                try:
                    queue.put_nowait(None)
                except Exception:
                    pass

        initial_skill_display = build_skill_display(
            context,
            active_skill=requested_target_skill_id,
            runtime_registry=getattr(self.orchestrator, "runtime_registry", None),
        )
        if SSE_ACTIVE:
            SSE_ACTIVE.inc()
        first_event_started = __import__("time").perf_counter()
        push("run_started", {"session_id": session_id, "run_id": stream_generation, "risk_stage": "input"})
        push("expert_context", _expert_state_payload(context, self.orchestrator, team_member_switch))
        for event, data in initial_events or []:
            push(event, data)
        if protocol == UNIFIED_PROTOCOL:
            # Always provide one immediate, safe placeholder. Its normalized
            # label is also the stable name for this stage in both the live
            # SSE state and the persisted timeline.
            stable_status_labels["intent"] = normalize_status_label("意图判断")
            push(
                "synthetic_progress",
                {
                    "stage": "intent",
                    "status": "active",
                    "label": "意图判断",
                    "detail": "等待 ms-agent 执行步骤",
                },
            )
        if SSE_TTFT:
            SSE_TTFT.labels(skill_id=str(requested_target_skill_id or "general_chat")).observe(__import__("time").perf_counter() - first_event_started)
        push(
            "skill_context",
            {
                "session_id": session_id,
                "active_skill": initial_skill_display["skill_id"],
                "active_skill_label": initial_skill_display["active_skill_label"],
                "agent_label": initial_skill_display["agent_label"],
                "skill_brief": initial_skill_display.get("brief", ""),
                "skill_info": initial_skill_display.get("info", ""),
                "brief": initial_skill_display.get("brief", ""),
                "info": initial_skill_display.get("info", ""),
                "description": initial_skill_display.get("description", ""),
                "scene_name": initial_skill_display["scene_name"],
                "skill_theme": initial_skill_display["skill_theme"],
            },
        )
        # copy_context carries request/trace context into a bounded worker.
        worker_context = copy_context()
        if progress_simulation_enabled:
            threading.Thread(
                target=emit_simulated_progress,
                name="hailiang-sse-progress",
                daemon=True,
            ).start()
        self._executor.submit(worker_context.run, run)
        try:
            while True:
                try:
                    item = queue.get(timeout=0.25)
                except Empty:
                    if self.turn_coordinator.is_cancelled(lease):
                        finalize_cancelled_response()
                    # Heartbeats keep reverse proxies from timing out idle
                    # streams, without delaying cancellation acknowledgement.
                    yield "event: ping\ndata: {}\n\n"
                    continue
                if item is None:
                    break
                if item is _CANCEL_WAKEUP:
                    # Finalize before closing the original response, then
                    # forward the terminal events already queued by the
                    # cancellation finalizer. The worker remains guarded by
                    # TurnCoordinator and cannot publish further content.
                    finalize_cancelled_response()
                    while True:
                        try:
                            trailing = queue.get_nowait()
                        except Empty:
                            break
                        if trailing is None or trailing is _CANCEL_WAKEUP:
                            continue
                        yield trailing
                    break
                yield item
        finally:
            with self._active_stream_queues_lock:
                if self._active_stream_queues.get(stream_queue_key) is queue:
                    self._active_stream_queues.pop(stream_queue_key, None)
            # Starlette closes the iterator when the browser disconnects.  The
            # worker sees the generation mismatch before each output/persist
            # boundary and therefore cannot overwrite a newer request.
            if context.session_meta.get("active_stream_generation") == stream_generation:
                context.session_meta["active_stream_generation"] = "client_cancelled"

    def prepare_skill_transition(
        self,
        session_id: str,
        user_id: str,
        *,
        action: str,
        target_skill_id: str | None,
        source: str,
        source_message_id: str | None = None,
        source_interaction_id: str | None = None,
        run_id: str = "",
    ) -> dict[str, Any]:
        context = self.repository.get(session_id)
        context.user_id = user_id
        self.fact_service.hydrate_context(context)
        normalized_action = str(action or "").strip()
        if normalized_action not in {"enter", "exit"}:
            raise ValueError("unsupported skill transition action")
        target = GENERAL_CHAT_SKILL_ID if normalized_action == "exit" else canonical_skill_id(target_skill_id)
        runtime_registry = getattr(self.orchestrator, "runtime_registry", None)
        if not target or runtime_registry is None:
            raise ValueError("target skill not found")
        resolve_skill_id = getattr(runtime_registry, "resolve_skill_id", None)
        if normalized_action == "enter" and callable(resolve_skill_id):
            target = resolve_skill_id(target)
        if hasattr(runtime_registry, "is_enabled") and not runtime_registry.is_enabled(target):
            raise ValueError("SKILL_DISABLED")
        if not runtime_registry.get(target):
            raise ValueError("target skill not found")
        if normalized_action == "enter" and target == GENERAL_CHAT_SKILL_ID:
            raise ValueError("target skill cannot be entered directly")
        from_skill = str(
            context.interaction_state.get("active_skill")
            or context.skill_states.get("skill_runtime", {}).get("active_skill_id")
            or CAREER_PLAN_SKILL_ID
        )
        from_skill = canonical_skill_id(from_skill)
        if normalized_action == "enter" and target == from_skill:
            raise RuntimeError("TARGET_SKILL_ALREADY_ACTIVE")
        source_message: dict[str, Any] | None = None
        transition_context: dict[str, Any] = {
            "context_mode": "facts_only" if source in {"toolbar", "exit_button"} else "message_context",
            "facts_snapshot": _transition_facts_snapshot(context),
        }
        if source == "route_suggestion":
            source_message = next(
                (
                    item
                    for item in context.messages
                    if item.get("role") == "assistant" and item.get("message_id") == source_message_id
                ),
                None,
            )
            latest_assistant = next(
                (item for item in reversed(context.messages) if item.get("role") == "assistant"),
                None,
            )
            if source_message is None or latest_assistant is not source_message:
                raise RuntimeError("route suggestion is no longer current")
            interaction_id = str(source_interaction_id or "route_suggestions")
            states = ensure_message_interactions(source_message)
            state = states.get(interaction_id)
            suggestions = source_message.get("route_suggestions") or source_message.get("metadata", {}).get("route_suggestions") or []
            valid_target = any(
                isinstance(item, dict)
                and canonical_skill_id(item.get("target_skill_id")) == target
                for item in suggestions
            )
            if not state or state.get("status") != ACTIVE or not valid_target:
                raise RuntimeError("route suggestion is no longer active")
            update_interaction(source_message, interaction_id, status=SELECTED, selected_target_skill_id=target)
            source_message["selected_route_suggestion"] = target
            metadata = source_message.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["selected_route_suggestion"] = target
            message_context, context_message_ids = _message_context_for_transition(
                context,
                source_message=source_message,
                target_skill_id=target,
            )
            transition_context.update(
                {
                    "context_source_message_id": source_message_id,
                    "context_message_ids": context_message_ids,
                    "handoff_context": message_context,
                }
            )
        elif source not in {"toolbar", "exit_button"}:
            raise ValueError("unsupported skill transition source")

        expired = expire_active_interactions(context.messages)
        transition = {
            "action": normalized_action,
            "from_skill_id": from_skill,
            "to_skill_id": target,
            "source": source,
            "created_at": utc_now_iso(),
            **transition_context,
        }
        target_display = build_skill_display(
            context,
            active_skill=target,
            runtime_registry=runtime_registry,
        )
        from_display = build_skill_display(
            context,
            active_skill=from_skill,
            runtime_registry=runtime_registry,
        )
        # This belongs to the durable transition event, not just the runtime
        # state, so a BFF/front end can render an instant Skill card without a
        # second catalog request.
        transition["skill"] = _transition_skill_payload(target_display)
        transition["from_skill"] = _transition_skill_payload(from_display)
        if normalized_action == "exit":
            # Visible history record; the reset marker below excludes it from
            # subsequent model prompts.
            previous_title = context.title
            context.add_message(
                "user",
                "退出AI咨询室",
                metadata={"message_type": "skill_exit_command", "synthetic": True},
            )
            if previous_title is None:
                context.title = None
            exit_message = context.messages[-1]
            transition["synthetic_user_message"] = {
                "message_id": str(exit_message.get("message_id") or ""),
                "content": str(exit_message.get("content") or ""),
                "created_at": str(exit_message.get("created_at") or ""),
            }
        public_transition = _public_transition(transition)
        context.add_message(
            "assistant",
            "",
            metadata={"message_type": "skill_transition", "skill_transition": public_transition},
        )
        transition_message_id = _latest_assistant_message_id(context) or ""
        if normalized_action == "exit":
            transition["context_reset"] = True
            public_transition = _public_transition(transition)
            context.messages[-1]["metadata"]["skill_transition"] = public_transition
            memory_store = getattr(self.orchestrator, "memory_store", None)
            finalize_memory = getattr(memory_store, "finalize_for_skill_exit", None)
            if callable(finalize_memory):
                try:
                    active_bundle = runtime_registry.get(from_skill)
                    runtime_client = None
                    get_runtime_client = getattr(self.orchestrator, "_runtime_client_for_context", None)
                    if callable(get_runtime_client):
                        runtime_client = get_runtime_client(context)
                    memory_result = finalize_memory(
                        user_id=str(context.user_id or "anonymous"),
                        session_id=str(context.session_id),
                        active_skill_id=from_skill,
                        skill_dir=getattr(active_bundle, "root_dir", None),
                        llm_client=runtime_client,
                    )
                    promoted_fact_keys = _promote_exit_memory_global_facts(
                        context,
                        memory_result.context,
                        source_skill=from_skill,
                    )
                    transition["memory_exit_finalize"] = {
                        "status": memory_result.step.status,
                        "promoted_fact_keys": promoted_fact_keys,
                    }
                    if promoted_fact_keys:
                        self.fact_service.persist_context(context)
                except Exception as exc:  # noqa: BLE001
                    # An observability/continuity failure must never prevent a
                    # user from leaving a Skill.
                    transition["memory_exit_finalize"] = {
                        "status": "warning",
                        "promoted_fact_keys": [],
                        "error_type": type(exc).__name__,
                    }
            _reset_runtime_context_for_general_chat(
                context,
                transition_message_id=transition_message_id,
            )
        else:
            if transition.get("context_mode") == "facts_only":
                _reset_runtime_context_for_skill(
                    context,
                    target_skill_id=target,
                    transition_message_id=transition_message_id,
                )
            context.session_meta["hide_next_user_message"] = True
        event = make_event(
            "skill_transition_requested",
            {**public_transition, "expired_interactions": expired},
        )
        transition["transition_event_id"] = str(event.get("event_id") or "")
        analytics_events = [event]
        if normalized_action == "enter" and source == "toolbar":
            analytics_events.append(
                make_event(
                    "skill_toolbar_clicked",
                    {
                        "session_id": str(context.session_id),
                        "skill_id": target,
                        "source": "toolbar",
                        "run_id": run_id,
                        "message_id": transition_message_id,
                        "dedupe_key": f"toolbar_click:{run_id}:{target}",
                    },
                )
            )
        if hasattr(self.orchestrator, "_record_events"):
            self.orchestrator._record_events(context, analytics_events)
        else:
            context.event_trace.extend(analytics_events)
        self.repository.save(context)
        return {**transition, "message_id": transition_message_id}

    def stream_skill_transition(
        self,
        session_id: str,
        user_id: str,
        *,
        action: str,
        target_skill_id: str | None,
        source: str,
        source_message_id: str | None = None,
        source_interaction_id: str | None = None,
        enable_thinking: bool = False,
        return_reasoning: bool = False,
        prepared_transition: dict[str, Any] | None = None,
        lease: TurnLease | None = None,
        protocol: str = "legacy",
        source_endpoint: str = "sessions/chat/stream",
    ) -> Iterator[str]:
        transition = prepared_transition or self.prepare_skill_transition(
            session_id,
            user_id,
            action=action,
            target_skill_id=target_skill_id,
            source=source,
            source_message_id=source_message_id,
            source_interaction_id=source_interaction_id,
            run_id="",
        )
        transition_protocol = normalize_protocol(protocol)
        lease = lease or self.reserve_turn(session_id, user_id)
        if transition["action"] == "exit":
            transition_builder = SseEnvelopeBuilder(
                run_id=lease.generation,
                session_id=session_id,
                protocol=transition_protocol,
            )
            try:
                started = self._encode_and_record_sse(
                    builder=transition_builder,
                    session_id=session_id,
                    run_id=lease.generation,
                    user_id=user_id,
                    source_endpoint=source_endpoint,
                    internal_event="run_started",
                    data={"session_id": session_id, "run_id": lease.generation, "risk_stage": "input"},
                )
                if started:
                    yield started
                transitioned = self._encode_and_record_sse(
                    builder=transition_builder,
                    session_id=session_id,
                    run_id=lease.generation,
                    user_id=user_id,
                    source_endpoint=source_endpoint,
                    internal_event="skill_transition",
                    data=_public_transition(transition),
                )
                if transitioned:
                    yield transitioned
                completed = self._encode_and_record_sse(
                    builder=transition_builder,
                    session_id=session_id,
                    run_id=lease.generation,
                    user_id=user_id,
                    source_endpoint=source_endpoint,
                    internal_event="run_completed",
                    data={"session_id": session_id, "status": "completed", "finish_reason": "stop"},
                )
                if completed:
                    yield completed
                context = self.repository.get(session_id)
                presentation = transition_builder.presentation()
                for message in reversed(context.messages):
                    if message.get("role") != "assistant":
                        continue
                    metadata = message.setdefault("metadata", {})
                    if not isinstance(metadata, dict) or metadata.get("message_type") != "skill_transition":
                        continue
                    message["content"] = str(presentation["assistant"].get("content") or "")
                    message["presentation"] = presentation
                    break
                snapshots = context.session_meta.setdefault("sse_v2_runs", {})
                if isinstance(snapshots, dict):
                    snapshots[lease.generation] = transition_builder.snapshot()
                ledger = context.session_meta.setdefault("run_ledger", {})
                if isinstance(ledger, dict):
                    item = ledger.setdefault(lease.generation, {})
                    if isinstance(item, dict):
                        item["status"] = "completed"
                self.repository.save(context)
            finally:
                self.turn_coordinator.release(lease)
            return
        prompt = f"进入{transition['to_skill_id']}" if transition["action"] == "enter" else "退出当前顾问"
        handoff_context = {
            "skill_transition": transition,
            "context_mode": transition.get("context_mode") or "message_context",
            "facts_snapshot": transition.get("facts_snapshot") or {},
            "context_message_ids": transition.get("context_message_ids") or [],
            "transition_instruction": prompt,
        }
        if transition.get("handoff_context"):
            handoff_context["handoff_context"] = transition["handoff_context"]
        yield from self.stream_message(
            session_id,
            user_id,
            prompt,
            enable_thinking=enable_thinking,
            return_reasoning=return_reasoning,
            requested_target_skill_id=str(transition["to_skill_id"]),
            handoff_context=handoff_context,
            lease=lease,
            protocol=transition_protocol,
            source_endpoint=source_endpoint,
            initial_events=[("skill_transition", _public_transition(transition))],
        )
        context = self.repository.get(session_id)
        ledger = context.session_meta.get("run_ledger") if isinstance(context.session_meta, dict) else {}
        run_status = ledger.get(lease.generation, {}).get("status") if isinstance(ledger, dict) else ""
        if run_status == "completed":
            event = make_event(
                "skill_activation_succeeded",
                {
                    "session_id": session_id,
                    "skill_id": str(transition["to_skill_id"]),
                    "source": source,
                    "run_id": lease.generation,
                    "message_id": str(transition.get("message_id") or ""),
                    "transition_event_id": str(transition.get("transition_event_id") or ""),
                    "dedupe_key": f"skill_activation:{lease.generation}:{transition['to_skill_id']}",
                },
            )
            if hasattr(self.orchestrator, "_record_events"):
                self.orchestrator._record_events(context, [event])
            else:
                context.event_trace.append(event)
            self.repository.save(context)
