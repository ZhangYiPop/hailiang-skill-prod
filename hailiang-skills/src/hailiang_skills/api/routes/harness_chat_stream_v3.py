"""Minimal, V2-shaped public SSE entry for the multi-agent Harness.

The detailed Harness endpoints remain useful for the workbench.  This module
is intentionally the only endpoint a product BFF needs for a Harness chat.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from copy import deepcopy
from typing import Any, Literal, TypeAlias

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hailiang_skills.api.session_lifecycle import ContextData, open_or_resume_session
from hailiang_skills.core.deployment import deployment_environment
from hailiang_skills.harness.contracts import InternalAnswerSubmission, HarnessError, TERMINAL, identifier
from hailiang_skills.harness.policy import capture_policy


SSE_V3_PROTOCOL = "hailiang.sse.v3"


def format_sse_event(event: str, data: dict) -> str:
    """Keep the public adapter independent from the legacy streaming runtime."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


ExpertTeamSelector: TypeAlias = str | list[str]


class TeamBoundInput(StrictInput):
    # The canonical wire form is a list so callers can reserve a multi-team
    # request now. A single string remains valid for V2-style compatibility.
    # The current orchestration head deliberately accepts exactly one team.
    expert_team_id: ExpertTeamSelector


class ChatInput(TeamBoundInput):
    # A selector starts or replaces the session's team binding.  When omitted,
    # a bound session continues with its frozen team; ``clear_expert_team`` is
    # the explicit transition back to standalone general_chat.
    expert_team_id: ExpertTeamSelector | None = None
    expert_team_source: Literal["filesystem_fallback"] | None = None
    clear_expert_team: bool = False
    action: Literal["chat"]
    content: str = Field(min_length=1, max_length=120000)


class AnswerInput(TeamBoundInput):
    action: Literal["answer_input_request"]
    request_id: str = Field(min_length=1, max_length=80)
    # Stable task/bubble ID. A new request run_id is valid for every answer.
    assistant_message_id: str = Field(min_length=1, max_length=80)
    answers: dict[str, Any] = Field(default_factory=dict)
    text: str | None = Field(default=None, max_length=120000)
    skipped: bool = False


class ResumeInput(TeamBoundInput):
    action: Literal["resume"]
    assistant_message_id: str = Field(min_length=1, max_length=80)


class StopInput(StrictInput):
    action: Literal["stop"]
    source: Literal["composer"]


class V3ChatStreamRequest(StrictInput):
    session_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=100)
    input: str = Field(min_length=1)
    context_data: ContextData


def _parse_input(raw: str) -> ChatInput | AnswerInput | ResumeInput | StopInput:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, "INVALID_INPUT_JSON") from exc
    if not isinstance(value, dict):
        raise HTTPException(422, "input must be a JSON object")
    try:
        action = value.get("action")
        if action == "chat":
            return ChatInput.model_validate(value)
        if action == "answer_input_request":
            return AnswerInput.model_validate(value)
        if action == "resume":
            return ResumeInput.model_validate(value)
        if action == "stop":
            return StopInput.model_validate(value)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors()) from exc
    raise HTTPException(422, "HARNESS_V3_ACTION_UNSUPPORTED")


def _normalise_expert_team_ids(value: ExpertTeamSelector) -> list[str]:
    """Validate the public selector and canonicalise it to an ordered list."""
    raw_values = [value] if isinstance(value, str) else value
    if not isinstance(raw_values, list) or not raw_values:
        raise HTTPException(422, "EXPERT_TEAM_ID_REQUIRED")
    if len(raw_values) > 8:
        raise HTTPException(422, "TOO_MANY_EXPERT_TEAMS")
    team_ids: list[str] = []
    for item in raw_values:
        if not isinstance(item, str) or not (team_id := item.strip()) or len(team_id) > 100:
            raise HTTPException(422, "INVALID_EXPERT_TEAM_ID")
        team_ids.append(team_id)
    if len(set(team_ids)) != len(team_ids):
        raise HTTPException(422, "DUPLICATE_EXPERT_TEAM_ID")
    return team_ids


def _single_supported_team_id(value: ExpertTeamSelector) -> tuple[str, list[str]]:
    team_ids = _normalise_expert_team_ids(value)
    # This makes the contract forward compatible without pretending current
    # single-head orchestration has already combined several teams.
    if len(team_ids) != 1:
        raise HTTPException(422, "MULTI_EXPERT_TEAMS_NOT_ENABLED")
    return team_ids[0], team_ids


def _request_hash(input_data, profile_data, team_ids: list[str]):
    payload = input_data.model_dump()
    # A legacy string and the canonical one-element list mean the same request
    # for idempotency purposes.
    if "expert_team_id" in payload:
        payload["expert_team_id"] = team_ids
    encoded = json.dumps({"input": payload, "profile_data": profile_data}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _profile_name(profile_data: dict | None, profile_id: str) -> str:
    value = (profile_data or {}).get("name")
    return value.strip() if isinstance(value, str) and value.strip() else f"未命名孩子-{profile_id}"


def _normalise_profile_data(data: ContextData) -> dict[str, Any] | None:
    """Use the V3 portrait name, while accepting the V2 BFF name alias.

    ``student_name`` remains an accepted ContextData field for V2-compatible
    forwarders. Without this bridge a V3 request succeeds but silently creates
    an ``未命名孩子-*`` profile, even when the BFF supplied a child name.
    """
    raw = data.profile_data
    if raw is not None and not isinstance(raw, dict):
        raise HTTPException(422, "INVALID_PROFILE_DATA")
    profile_data = deepcopy(raw) if raw else {}
    legacy_name = str(data.student_name or "").strip()
    if not isinstance(profile_data.get("name"), str) or not profile_data["name"].strip():
        if legacy_name:
            profile_data["name"] = legacy_name
    return profile_data or None


def _public_context(context, profile_data: dict | None = None) -> dict[str, Any]:
    """Persist V2-compatible profile context in the V3 run, never in prompts."""
    profile_id = getattr(context, "profile_id", None) if context is not None else None
    scope = "profile" if profile_id else "unbound"
    name = str(getattr(context, "profile_name", "") or "").strip() if context is not None else ""
    if not name and profile_id:
        name = _profile_name(profile_data, str(profile_id))
    return {
        "profile_id": profile_id,
        "profile_name": name or None,
        "context_scope": scope,
        "context_label": name if scope == "profile" else "未绑定孩子",
        "context_notice": (
            {"type": "profile_context_activated", "text": f"本轮回答将结合 **{name}** 的档案数据。"}
            if scope == "profile" and name else {}
        ),
        "profile_context_status": "matched" if scope == "profile" else "unbound",
    }


def _ensure_profile(fact_service, user_id: str, profile_id: str | None, profile_data: dict | None):
    if not profile_id:
        return
    repo = fact_service.profile_repo
    listing = getattr(repo, "list_profiles", None)
    owned = {str(item.get("profile_id")) for item in listing(user_id)} if listing else set()
    if profile_id in owned:
        if isinstance((profile_data or {}).get("name"), str):
            updater = getattr(repo, "update_profile", None)
            if updater:
                updater(user_id, profile_id, name=_profile_name(profile_data, profile_id))
        return
    lookup = getattr(repo, "get_profile_by_id", None)
    if lookup:
        try:
            lookup(profile_id)
        except KeyError:
            pass
        else:
            raise HTTPException(403, "PROFILE_ACCESS_DENIED")
    creator = getattr(repo, "create_profile", None)
    if creator is None:
        raise HTTPException(409, "PROFILE_NOT_READY")
    creator(user_id, profile_id=profile_id, name=_profile_name(profile_data, profile_id), shared_facts_initialized=False)


def _team_for_context(context, orchestrator, team_id: str, *, source: str | None = None):
    """Resolve a selected team from deployment or an explicit local test package."""
    force_filesystem = source == "filesystem_fallback"
    used_filesystem = force_filesystem
    if force_filesystem:
        if deployment_environment().startswith("prod"):
            raise HTTPException(403, "FILESYSTEM_FALLBACK_NOT_AVAILABLE")
        from hailiang_skills.runtime_bridge.expert_bundle import load_local_expert_registry
        from hailiang_skills.runtime_bridge.expert_team_bundle import load_local_expert_team_registry
        from hailiang_skills.runtime_bridge.main_planner import (
            PROJECT_RUNTIME_AGENTS_ROOT,
            PROJECT_RUNTIME_AGENT_TEAMS_ROOT,
        )
        local_skills = orchestrator._load_runtime_registry()
        experts = load_local_expert_registry(PROJECT_RUNTIME_AGENTS_ROOT, local_skills)
        teams = load_local_expert_team_registry(PROJECT_RUNTIME_AGENT_TEAMS_ROOT, experts)
        team = teams.get(team_id)
        if team is not None:
            context.session_meta["filesystem_team_fallback_id"] = team_id
            context.session_meta["expert_team_source"] = "filesystem_fallback"
    else:
        snapshot = context.session_meta.get("configuration_snapshot")
        entries = snapshot.get("entries") if isinstance(snapshot, dict) else None
        if isinstance(entries, list) and entries:
            from hailiang_skills.workbench.catalog import build_runtime_registries
            _skills, experts, teams = build_runtime_registries(entries)
        else:
            experts = getattr(orchestrator, "expert_registry", None)
            teams = getattr(orchestrator, "expert_team_registry", None)
        team = teams.get(team_id) if teams is not None else None
    if team is None and not force_filesystem:
        # A deployment is only authoritative for teams it actually contains.
        # An explicitly selected team without a frozen release must use the
        # checked-in runtime package, never another database team's release.
        from hailiang_skills.runtime_bridge.expert_bundle import load_local_expert_registry
        from hailiang_skills.runtime_bridge.expert_team_bundle import load_local_expert_team_registry
        from hailiang_skills.runtime_bridge.main_planner import (
            PROJECT_RUNTIME_AGENTS_ROOT,
            PROJECT_RUNTIME_AGENT_TEAMS_ROOT,
        )
        local_skills = orchestrator._load_runtime_registry()
        experts = load_local_expert_registry(PROJECT_RUNTIME_AGENTS_ROOT, local_skills)
        teams = load_local_expert_team_registry(PROJECT_RUNTIME_AGENT_TEAMS_ROOT, experts)
        team = teams.get(team_id)
        if team is not None:
            used_filesystem = True
            context.session_meta["filesystem_team_fallback_id"] = team_id
    if team is not None and not used_filesystem:
        context.session_meta.pop("filesystem_team_fallback_id", None)
        context.session_meta.pop("expert_team_source", None)
    if team is None:
        raise HTTPException(422, "EXPERT_TEAM_NOT_FOUND")
    coordinator_id = str(getattr(team, "coordinator_expert_id", "") or "")
    members = tuple(getattr(team, "member_expert_ids", ()) or ())
    if not coordinator_id or coordinator_id not in members or experts is None or experts.get(coordinator_id) is None:
        raise HTTPException(422, "EXPERT_TEAM_COORDINATOR_INVALID")
    return team


def _bind_input_team(context, orchestrator, team_id: str, *, source: str | None = None):
    """Persist an input-selected team without narrowing policy to one expert."""
    team = _team_for_context(context, orchestrator, team_id, source=source)
    coordinator_id = team.coordinator_expert_id
    context.session_meta["expert_team_id"] = team.team_id
    # Legacy components still require an active coordinator marker.  The
    # policy recognises input_team as team selection, not a direct-expert run.
    context.session_meta["expert_id"] = coordinator_id
    context.session_meta["active_expert_id"] = coordinator_id
    context.session_meta["expert_selection_source"] = "input_team"
    if source != "filesystem_fallback":
        context.session_meta.pop("filesystem_team_fallback_id", None)
        context.session_meta.pop("expert_team_source", None)
    context.session_meta.pop("branch_expert_override", None)
    context.set_session_agent_selection(
        expert_team_id=team.team_id,
        expert_id=coordinator_id,
        selection_source="input_team",
    )
    context.apply_session_agent_selection()
    return team


def _activate_general_chat(context):
    """A V3 request without a team is an explicit general-chat turn."""
    from hailiang_skills.api.session_lifecycle import _initialize_general_chat_state
    context.session_meta.pop("expert_team_id", None)
    context.session_meta.pop("expert_id", None)
    context.session_meta.pop("active_expert_id", None)
    context.session_meta.pop("expert_selection_source", None)
    context.session_meta.pop("filesystem_team_fallback_id", None)
    context.session_meta.pop("expert_team_source", None)
    context.session_meta.pop("harness_v3_minimal", None)
    context.set_session_agent_selection(
        expert_team_id=None,
        expert_id=None,
        selection_source="general_chat",
    )
    _initialize_general_chat_state(context)


def _general_payload(*, session_id: str, request_run_id: str, assistant_message_id: str, content: str, event: str, reason: str):
    return {
        "protocol": SSE_V3_PROTOCOL,
        "conversation_mode": "general",
        "session_id": session_id,
        "run_id": request_run_id,
        "assistant_message_id": assistant_message_id,
        "expert_team_id": None,
        "expert_team_ids": [],
        "team_orchestration_version": None,
        "record_version": 1,
        "cursor": 0,
        "reason": reason,
        "stream_status": "completed" if event == "done" else "running",
        "outcome": "completed" if event == "done" else None,
        "persistence": {
            "should_store_snapshot": event != "done",
            "should_save_history": event == "done",
            "replace_staged": event == "done",
            "should_discard_staged": False,
            "terminal": event == "done",
            "record_kind": "general_chat",
        },
        "state": {
            "conversation_mode": "general",
            "result": content,
            "assistant_message": {"id": assistant_message_id, "content": content, "status": "completed", "revision": 1},
        },
    }


def _assert_run_team(run: dict, team_id: str):
    if str((run.get("policy") or {}).get("team_id") or "") != team_id:
        raise HTTPException(409, "EXPERT_TEAM_MISMATCH")


def _seed_facts(context, runtime, session_id, user_id, profile_id):
    from hailiang_skills.harness.store import HarnessStore
    seed = {}
    dummy = {"session_id": session_id, "user_id": user_id, "profile_id": profile_id}
    for scope, facts in [("user", context.shared_facts), ("profile", context.profile_facts), ("session", context.session_facts)]:
        for key, record in facts.facts.items():
            identity = HarnessStore.fact_identity(dummy, {"id": "seed"}, scope, key)
            seed[identity] = {"key": key, "value": record.value, "scope": scope, "status": record.status, "version": 1, "source": record.model_dump()}
    return seed


def _outcome(run: dict) -> str | None:
    """Business outcome is separate from transport completion."""
    if run["status"] == "cancelled":
        return "cancelled"
    if run["status"] == "failed":
        return "failed"
    if run["status"] == "completed":
        return run.get("outcome", "completed")
    return None


def _persistence(*, event: str, run: dict) -> dict[str, Any]:
    """Tell a simple BFF exactly what to persist; it must not infer this."""
    if event == "state":
        return {"should_store_snapshot": True, "should_save_history": False,
                "replace_staged": False, "should_discard_staged": False,
                "terminal": False, "record_kind": "progress"}
    if event == "done_suspended":
        return {"should_store_snapshot": True, "should_save_history": True,
                "replace_staged": True, "should_discard_staged": False,
                "terminal": False, "record_kind": "waiting_for_user"}
    if event == "done_timeout":
        return {"should_store_snapshot": True, "should_save_history": True,
                "replace_staged": True, "should_discard_staged": False,
                "terminal": False, "record_kind": "running"}
    if run["status"] == "cancelled":
        return {"should_store_snapshot": False, "should_save_history": False,
                "replace_staged": False, "should_discard_staged": True,
                "terminal": True, "record_kind": "cancelled"}
    return {"should_store_snapshot": True, "should_save_history": True,
            "replace_staged": True, "should_discard_staged": False,
            "terminal": True, "record_kind": "final"}


def _project_product_state(snapshot: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """Turn the diagnostic/public-store snapshot into the BFF-facing state.

    The store projection intentionally keeps durable internal IDs for tracing.
    This boundary assigns stable per-frame product IDs and removes all fields
    that would let a BFF treat an internal Harness ID as an external contract.
    """
    internal_run_id = run["run_id"]
    source_run = next((item for item in snapshot.get("runs", []) if item.get("id") == internal_run_id), {})
    source_nodes = [item for item in snapshot.get("nodes", []) if item.get("run_id") == internal_run_id]
    id_map = {item["id"]: f"node_{index + 1}" for index, item in enumerate(source_nodes)}

    def projected_node(item: dict[str, Any]) -> dict[str, Any]:
        kind, status = item.get("kind"), item.get("status")
        phase = item.get("phase")
        action = item.get("action")
        if kind == "tool":
            phase = phase or ({"running": "using_tool", "waiting_for_user": "waiting_for_user",
                               "waiting_for_dependency": "waiting_for_dependency", "completed": "completed"}.get(status, status))
            action = action or item.get("title") or "正在执行工具"
        return {
            "id": id_map[item["id"]], "parent_id": id_map.get(item.get("parent_id")),
            "kind": kind, "title": item.get("title"), "actor": item.get("actor"),
            "expert_id": item.get("expert_id"), "brief": deepcopy(item.get("brief")),
            "phase": phase, "action": action, "status": status, "outcome": item.get("outcome"),
            "result": deepcopy(item.get("result")),
            "waiting_on": [id_map.get(value, value) for value in item.get("waiting_on") or []],
            "children_count": item.get("children_count", 0),
            "completed_children_count": item.get("completed_children_count", 0),
            "attempts": item.get("attempts"), "error": deepcopy(item.get("error")),
            "stale": bool(item.get("stale", False)), "started_at": item.get("started_at"), "ended_at": item.get("ended_at"),
        }

    dispatch = deepcopy(source_run.get("dispatch"))
    if isinstance(dispatch, dict):
        for expert in dispatch.get("experts") or []:
            if isinstance(expert, dict):
                expert["node_id"] = id_map.get(expert.get("node_id"))
                expert["depends_on_node_ids"] = [id_map.get(value, value) for value in expert.get("depends_on_node_ids") or []]
    inputs = []
    for item in snapshot.get("input_requests", []):
        if item.get("run_id") != internal_run_id:
            continue
        # This is a product-facing projection, so use the public V3 names
        # consumed by the task-progress component.  In particular, bindings
        # are needed to place an answered request below its owning task; do
        # not leak the durable task IDs while doing that mapping.
        projected_input = {
            key: deepcopy(item.get(key))
            for key in (
                "id", "title", "fields", "status", "round", "answer", "clarification_reason", "actor",
                "group_id", "group_position",
            )
            if key in item
        }
        projected_input["version"] = int(item.get("version") or item.get("request_version") or 0)
        projected_input["owner_task_id"] = id_map.get(item.get("owner_task_id"))
        projected_input["bindings"] = [
            {
                **deepcopy(binding),
                "task_id": id_map.get(binding.get("task_id"), binding.get("task_id")),
            }
            for binding in (item.get("bindings") or [])
            if isinstance(binding, dict)
        ]
        inputs.append(projected_input)
    groups = []
    for item in snapshot.get("input_groups", []):
        if item.get("run_id") == internal_run_id:
            groups.append({key: deepcopy(item.get(key)) for key in (
                "id", "status", "created_at", "request_ids", "active_request_id", "total_count", "completed_count", "actors", "version"
            ) if key in item})
    allowed = (
        "goal", "status", "outcome", "phase", "action", "dispatch", "synthesis_status",
        "recommendations_status", "jump_cards", "result", "error", "input_queue", "waiting_count",
        "completed_count", "task_count", "input_tokens_estimated", "input_tokens_actual", "input_token_limit",
    )
    state = {key: deepcopy(source_run.get(key)) for key in allowed if key in source_run}
    # During an active tail stream ``run.result`` is intentionally not yet
    # committed.  The durable public final-message projection is therefore
    # the reconnect-safe source of the partial user-visible reply.
    message_id = str(run.get("assistant_message_id") or (internal_run_id + ":final"))
    partial_message = next(
        (
            item for item in snapshot.get("messages", [])
            if item.get("id") == message_id and item.get("kind") == "final"
        ),
        None,
    )
    if partial_message and not state.get("result"):
        state["result"] = str(partial_message.get("content") or "")
    state["assistant_message"] = {
        "id": message_id,
        "content": str((partial_message or {}).get("content") or state.get("result") or ""),
        "status": str((partial_message or {}).get("status") or ("completed" if run.get("status") in TERMINAL else "streaming")),
        "revision": int((partial_message or {}).get("revision") or 1),
    }
    state.update({"dispatch": dispatch, "nodes": [projected_node(item) for item in source_nodes],
                  "inputs": inputs, "input_groups": groups})
    return state


def _message_payload(runtime, run: dict[str, Any], event: dict[str, Any], *, request_run_id: str) -> dict[str, Any]:
    """Translate a durable Harness text event into the public V3 envelope."""
    data = deepcopy(event.get("data") or {})
    message_id = str(data.get("id") or run.get("assistant_message_id") or (run["run_id"] + ":final"))
    return {
        "protocol": SSE_V3_PROTOCOL,
        "conversation_mode": "team",
        "session_id": run["session_id"],
        "run_id": request_run_id,
        "assistant_message_id": message_id,
        "cursor": int(event.get("seq") or 0),
        "record_version": int(run.get("version") or 0),
        "expert_team_source": run.get("policy", {}).get("team_source", "deployed"),
        "revision": int(data.get("revision") or 1),
        "offset": int(data.get("offset") or 0),
        "text": str(data.get("text") or ""),
    }


def _payload(runtime, run, *, request_run_id: str, event: str, reason: str, cursor: int = 0):
    """Canonical V3 product frame with a request ID and stable bubble ID."""
    snapshot = runtime.store.public_snapshot(run["session_id"], run["user_id"], run["profile_id"])
    state = _project_product_state(snapshot, run)
    requested_team_ids = deepcopy(run.get("requested_expert_team_ids") or [run.get("policy", {}).get("team_id")])
    team_orchestration_version = str(run.get("team_orchestration_version") or "single_team_v1")
    state["expert_team_id"] = run.get("policy", {}).get("team_id")
    state["expert_team_ids"] = requested_team_ids
    state["expert_team_source"] = run.get("policy", {}).get("team_source", "deployed")
    state["team_orchestration_version"] = team_orchestration_version
    context = deepcopy(run.get("public_context") or {})
    payload = {
        "protocol": SSE_V3_PROTOCOL,
        "conversation_mode": "team",
        "session_id": run["session_id"],
        # This echoes the BFF's current HTTP request id. It is intentionally
        # distinct from the persistent assistant_message_id below.
        "run_id": request_run_id,
        "assistant_message_id": run.get("assistant_message_id"),
        "expert_team_id": run.get("policy", {}).get("team_id"),
        "expert_team_ids": requested_team_ids,
        "expert_team_source": run.get("policy", {}).get("team_source", "deployed"),
        "team_orchestration_version": team_orchestration_version,
        "record_version": run["version"], "cursor": cursor, "reason": reason,
        "stream_status": "waiting_for_user" if event == "done_suspended" else ("running" if event == "done_timeout" else run["status"]),
        "outcome": _outcome(run), "persistence": _persistence(event=event, run=run),
        **context, "state": state,
    }
    if run.get("superseded_assistant_message_id"):
        payload["superseded_assistant_message_id"] = run["superseded_assistant_message_id"]
    return payload

def _suspended(run):
    if run["status"] in TERMINAL or not any(item["status"] == "pending" for item in run["inputs"].values()):
        return False
    live = [task for task in run["tasks"].values() if task["status"] not in TERMINAL]
    return not any(task["status"] in {"queued", "running"} for task in live)


def build_harness_chat_stream_v3_router(repository, fact_service, orchestrator, runtime, snapshot_resolver=None):
    router = APIRouter()

    def operation(call):
        try:
            return call()
        except HarnessError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.code}) from exc

    async def general_stream(context, input_data: ChatInput, request_run_id: str):
        """Use the established general_chat runtime in the public V3 envelope."""
        result = await asyncio.to_thread(orchestrator.handle_message, input_data.content, context)
        repository.save(context)
        assistant_message_id = next(
            (
                str(message.get("message_id") or "")
                for message in reversed(context.messages)
                if message.get("role") == "assistant" and str(message.get("message_id") or "")
            ),
            identifier("msg"),
        )
        content = str(getattr(result, "assistant_message", "") or "")
        yield format_sse_event("state", _general_payload(
            session_id=context.session_id, request_run_id=request_run_id,
            assistant_message_id=assistant_message_id, content="", event="state", reason="general_started",
        ))
        if content:
            yield format_sse_event("message.delta", {
                "protocol": SSE_V3_PROTOCOL,
                "conversation_mode": "general",
                "session_id": context.session_id,
                "run_id": request_run_id,
                "assistant_message_id": assistant_message_id,
                "revision": 1,
                "offset": 0,
                "text": content,
            })
        yield format_sse_event("done", _general_payload(
            session_id=context.session_id, request_run_id=request_run_id,
            assistant_message_id=assistant_message_id, content=content, event="done", reason="general_completed",
        ))

    async def stream(http_request: Request, harness_run_id: str, request_run_id: str):
        # V3 remains finite for a BFF, but background execution never depends
        # on this connection remaining open.
        timeout_seconds = max(1, int(os.getenv("HAILIANG_HARNESS_SSE_WAIT_SECONDS", "600")))
        # Like SSE v2's ``ping`` frame, this is transport-only and therefore
        # neither changes the task snapshot nor asks the BFF/frontend to
        # re-render. It keeps a quiet model/tool call from being mistaken for
        # a dead HTTP connection by a proxy.
        heartbeat_seconds = max(1, int(os.getenv("HAILIANG_HARNESS_SSE_HEARTBEAT_SECONDS", "15")))
        started = time.monotonic()
        last_wire_activity = started
        run = await asyncio.to_thread(runtime.store.get, harness_run_id)
        initial = await asyncio.to_thread(runtime.store.public_snapshot, run["session_id"], run["user_id"], run["profile_id"])
        cursor = initial["cursor"]
        # A terminal resume must replay only its durable final frame. It may
        # never make the BFF briefly render an active task again.
        if run["status"] in TERMINAL:
            yield format_sse_event("done", _payload(runtime, run, request_run_id=request_run_id, event="done", reason="terminal", cursor=cursor))
            return
        yield format_sse_event("state", _payload(runtime, run, request_run_id=request_run_id, event="state", reason="stream_started", cursor=cursor))
        last_wire_activity = time.monotonic()
        while not await http_request.is_disconnected():
            run = await asyncio.to_thread(runtime.store.get, harness_run_id)
            events = await asyncio.to_thread(runtime.store.public_events, run["session_id"], run["user_id"], run["profile_id"], cursor)
            relevant = [event for event in events if event.get("run_id") == harness_run_id and event.get("type") in {"state", "done", "message.delta", "message.reset"}]
            if relevant:
                cursor = max(event["seq"] for event in events)
                run = await asyncio.to_thread(runtime.store.get, harness_run_id)
                for item in relevant:
                    event_type = str(item.get("type") or "state")
                    if event_type in {"message.delta", "message.reset"}:
                        yield format_sse_event(event_type, _message_payload(runtime, run, item, request_run_id=request_run_id))
                        last_wire_activity = time.monotonic()
                        continue
                    event_name = "done" if event_type == "done" else "state"
                    yield format_sse_event(event_name, _payload(runtime, run, request_run_id=request_run_id, event=event_name, reason=item.get("reason", "updated"), cursor=int(item.get("seq") or cursor)))
                    last_wire_activity = time.monotonic()
                    if event_name == "done":
                        return
                continue
            # Always drain persisted deltas before returning a terminal frame.
            # A short final answer can emit reset/deltas/done between two SSE
            # polls; checking terminal status first used to drop those deltas.
            if run["status"] in TERMINAL:
                yield format_sse_event("done", _payload(runtime, run, request_run_id=request_run_id, event="done", reason="terminal", cursor=cursor))
                return
            if _suspended(run):
                yield format_sse_event("done_suspended", _payload(runtime, run, request_run_id=request_run_id, event="done_suspended", reason="waiting_for_user", cursor=cursor))
                return
            if time.monotonic() - started >= timeout_seconds:
                yield format_sse_event("done_timeout", _payload(runtime, run, request_run_id=request_run_id, event="done_timeout", reason="stream_timeout", cursor=cursor))
                return
            if time.monotonic() - last_wire_activity >= heartbeat_seconds:
                yield format_sse_event("ping", {})
                last_wire_activity = time.monotonic()
            await asyncio.sleep(0.2)

    def authorised_message_run(request: V3ChatStreamRequest, assistant_message_id: str) -> str:
        return operation(lambda: runtime.store.message_run(
            request.session_id, request.context_data.user_id, request.context_data.profile_id, assistant_message_id,
        ))

    def bind_request(request: V3ChatStreamRequest, input_data, harness_run_id: str, fingerprint: str) -> str:
        return operation(lambda: runtime.store.bind_client_run(
            request.session_id, request.context_data.user_id, request.context_data.profile_id,
            request.run_id, harness_run_id, fingerprint,
        ))

    def bind_public_ids(harness_run_id: str, *, team_ids: list[str], context, profile_data, superseded: str | None = None):
        def apply(run, changes, _db):
            changed = False
            if not run.get("assistant_message_id"):
                run["assistant_message_id"] = identifier("msg")
                changed = True
            if run.get("requested_expert_team_ids") != list(team_ids):
                run["requested_expert_team_ids"] = list(team_ids)
                changed = True
            if not run.get("team_orchestration_version"):
                run["team_orchestration_version"] = "single_team_v1"
                changed = True
            public_context = _public_context(context, profile_data) if context is not None else deepcopy(run.get("public_context") or {})
            if run.get("public_context") != public_context:
                run["public_context"] = public_context
                changed = True
            if superseded and run.get("superseded_assistant_message_id") != superseded:
                run["superseded_assistant_message_id"] = superseded
                changed = True
            if changed:
                changes.append({"event": "assistant_message_bound", "node_id": run["root_task_id"]})
        operation(lambda: runtime.store.mutate(harness_run_id, apply))

    @router.post("/sessions/chat/stream")
    async def post_chat_stream(request: V3ChatStreamRequest, http_request: Request):
        input_data = _parse_input(request.input)
        data = request.context_data
        profile_data = _normalise_profile_data(data)

        if isinstance(input_data, ChatInput):
            if input_data.clear_expert_team and input_data.expert_team_id is not None:
                raise HTTPException(422, "CLEAR_EXPERT_TEAM_CONFLICT")
            if input_data.expert_team_id is None:
                if input_data.expert_team_source is not None:
                    raise HTTPException(422, "EXPERT_TEAM_SOURCE_REQUIRES_TEAM")
                # Team binding is a conversation property, rather than a
                # one-message UI selection.  Preserve it across refreshes and
                # clients; only an explicit exit returns the session to the
                # general-chat path.
                if not input_data.clear_expert_team:
                    try:
                        bound_context = repository.get(request.session_id)
                    except KeyError:
                        bound_context = None
                    bound_team_id = str((bound_context.session_meta if bound_context else {}).get("expert_team_id") or "").strip()
                    bound_source = (bound_context.session_meta if bound_context else {}).get("expert_team_source")
                    if bound_team_id:
                        input_data = input_data.model_copy(update={
                            "expert_team_id": bound_team_id,
                            "expert_team_source": "filesystem_fallback" if bound_source == "filesystem_fallback" else None,
                        })
                if input_data.expert_team_id is not None:
                    # Continue through the team branch below with the
                    # persisted binding and the same source marker.
                    pass
                else:
                    active = runtime.store.active(request.session_id)
                    if active:
                        raise HTTPException(409, "ACTIVE_HARNESS_RUN_EXISTS")
                    _ensure_profile(fact_service, data.user_id, data.profile_id, profile_data)
                    context, created = open_or_resume_session(
                        repository, fact_service, session_id=request.session_id, data=data,
                        context_scope="profile" if data.profile_id else "unbound", allow_context_seed=False,
                    )
                    if created and snapshot_resolver:
                        snapshot = snapshot_resolver()
                        if snapshot:
                            context.session_meta["configuration_snapshot"] = snapshot
                    _activate_general_chat(context)
                    repository.save(context)
                    return StreamingResponse(
                        general_stream(context, input_data, request.run_id),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no", "X-SSE-Protocol": SSE_V3_PROTOCOL},
                    )
            bound = operation(lambda: runtime.store.client_run(request.session_id, data.user_id, data.profile_id, request.run_id))
            if bound:
                run = operation(lambda: runtime.store.get(bound["harness_run_id"]))
                team_id, team_ids = _single_supported_team_id(input_data.expert_team_id)
                fingerprint = _request_hash(input_data, profile_data, team_ids)
                if bound["request_hash"] != fingerprint:
                    raise HTTPException(409, "CLIENT_RUN_ID_CONFLICT")
                harness_run_id = bound["harness_run_id"]
                _assert_run_team(run, team_id)
                try:
                    context = repository.get(request.session_id)
                except KeyError:
                    context = None
                bind_public_ids(harness_run_id, team_ids=team_ids, context=context, profile_data=profile_data)
            else:
                active = runtime.store.active(request.session_id)
                superseded = None
                if active:
                    if active["user_id"] != data.user_id or active["profile_id"] != data.profile_id:
                        raise HTTPException(409, "ACTIVE_HARNESS_RUN_OWNED_BY_OTHER")
                    superseded = active.get("assistant_message_id")
                    runtime.cancel(active["run_id"])
                _ensure_profile(fact_service, data.user_id, data.profile_id, profile_data)
                context, created = open_or_resume_session(
                    repository, fact_service, session_id=request.session_id, data=data,
                    context_scope="profile" if data.profile_id else "unbound", allow_context_seed=False,
                )
                context.session_meta["runtime_mode"] = "harness_v1"
                context.session_meta["harness_v3_minimal"] = True
                if created and snapshot_resolver:
                    snapshot = snapshot_resolver()
                    if snapshot:
                        context.session_meta["configuration_snapshot"] = snapshot
                team_id, team_ids = _single_supported_team_id(input_data.expert_team_id)
                _team_for_context(context, orchestrator, team_id, source=input_data.expert_team_source)
                fingerprint = _request_hash(input_data, profile_data, team_ids)
                _bind_input_team(context, orchestrator, team_id, source=input_data.expert_team_source)
                repository.save(context)
                if profile_data and data.profile_id:
                    aliases = {"grade": "grade", "province": "student_province", "recent_exam.total_score": "exam_score"}
                    operation(lambda: runtime.store.upsert_portrait(data.user_id, data.profile_id, profile_data, aliases=aliases))
                if runtime.moderation:
                    runtime.moderation.check(input_data.content, stage="input", session_id=request.session_id)
                policy = operation(lambda: capture_policy(orchestrator, context))
                history = []
                for old in sorted(runtime.store.runs(request.session_id), key=lambda item: item["created_at"])[-10:]:
                    old_policy = old.get("policy") or {}
                    if (old["profile_id"] == data.profile_id and old["status"] == "completed"
                            and old_policy.get("team_id") == team_id
                            and old_policy.get("team_source", "deployed") == policy.get("team_source", "deployed")):
                        history.extend([{"role": "user", "content": old["goal"]}, {"role": "assistant", "content": old.get("result", "")}])
                harness_run_id = operation(lambda: runtime.create(
                    request.session_id, data.user_id, data.profile_id, input_data.content, policy,
                    _seed_facts(context, runtime, request.session_id, data.user_id, data.profile_id), history,
                    wire_protocol=SSE_V3_PROTOCOL,
                ))
                bind_public_ids(harness_run_id, team_ids=team_ids, context=context, profile_data=profile_data, superseded=superseded)
                bind_request(request, input_data, harness_run_id, fingerprint)
        elif isinstance(input_data, (AnswerInput, ResumeInput)):
            team_id, team_ids = _single_supported_team_id(input_data.expert_team_id)
            fingerprint = _request_hash(input_data, profile_data, team_ids)
            bound = operation(lambda: runtime.store.client_run(request.session_id, data.user_id, data.profile_id, request.run_id))
            if bound:
                if bound["request_hash"] != fingerprint:
                    raise HTTPException(409, "CLIENT_RUN_ID_CONFLICT")
                harness_run_id = bound["harness_run_id"]
                run = operation(lambda: runtime.store.get(harness_run_id))
                if run.get("assistant_message_id") != input_data.assistant_message_id:
                    raise HTTPException(409, "ASSISTANT_MESSAGE_ID_MISMATCH")
            else:
                harness_run_id = authorised_message_run(request, input_data.assistant_message_id)
                run = operation(lambda: runtime.store.get(harness_run_id))
                _assert_run_team(run, team_id)
                if isinstance(input_data, AnswerInput):
                    if operation(lambda: runtime.store.request_run(input_data.request_id)) != harness_run_id:
                        raise HTTPException(409, "INPUT_REQUEST_MESSAGE_MISMATCH")
                    if profile_data and data.profile_id:
                        aliases = {"grade": "grade", "province": "student_province", "recent_exam.total_score": "exam_score"}
                        operation(lambda: runtime.store.upsert_portrait(data.user_id, data.profile_id, profile_data, aliases=aliases))
                    submission = InternalAnswerSubmission(
                        user_id=data.user_id, profile_id=data.profile_id, answers=input_data.answers,
                        text=input_data.text, skipped=input_data.skipped,
                    )
                    operation(lambda: runtime.answer(input_data.request_id, submission, allow_partial=True, allow_correction=True))
                bind_request(request, input_data, harness_run_id, fingerprint)
            _assert_run_team(run, team_id)
        else:
            # Existing BFF stop routes may forward an old request ID. A fresh
            # stop request instead resolves the authorised active task.
            bound = operation(lambda: runtime.store.client_run(request.session_id, data.user_id, data.profile_id, request.run_id))
            if bound:
                harness_run_id = bound["harness_run_id"]
                run = operation(lambda: runtime.store.get(harness_run_id))
                if run["session_id"] != request.session_id or run["user_id"] != data.user_id or run["profile_id"] != data.profile_id:
                    raise HTTPException(403, "HARNESS_ACCESS_DENIED")
            else:
                active = runtime.store.active(request.session_id)
                if not active:
                    raise HTTPException(404, "ACTIVE_HARNESS_RUN_NOT_FOUND")
                if active["user_id"] != data.user_id or active["profile_id"] != data.profile_id:
                    raise HTTPException(403, "HARNESS_ACCESS_DENIED")
                harness_run_id = active["run_id"]
            runtime.cancel(harness_run_id)
        return StreamingResponse(stream(http_request, harness_run_id, request.run_id), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no", "X-SSE-Protocol": SSE_V3_PROTOCOL,
        })

    return router
