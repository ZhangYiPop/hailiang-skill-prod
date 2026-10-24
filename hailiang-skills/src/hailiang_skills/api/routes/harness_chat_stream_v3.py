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
from hailiang_skills.harness.contracts import InternalAnswerSubmission, HarnessError, TERMINAL, identifier
from hailiang_skills.harness.formal_v3 import FormalV3TurnExecutor
from hailiang_skills.harness.policy import capture_policy, configure_candidate_compatible_strategy


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
    action: Literal["chat"]
    content: str = Field(min_length=1, max_length=120000)


class AnswerInput(TeamBoundInput):
    action: Literal["answer_input_request"]
    request_id: str = Field(min_length=1, max_length=80)
    # Stable task/bubble ID. A new request run_id is valid for every answer.
    assistant_message_id: str = Field(min_length=1, max_length=80)
    submission_id: str = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=1)
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


def _team_for_context(context, orchestrator, team_id: str):
    """Resolve a requested team against the session's frozen deployment."""
    snapshot = context.session_meta.get("configuration_snapshot")
    entries = snapshot.get("entries") if isinstance(snapshot, dict) else None
    if isinstance(entries, list) and entries:
        from hailiang_skills.workbench.catalog import build_runtime_registries
        _skills, experts, teams = build_runtime_registries(entries)
    else:
        experts = getattr(orchestrator, "expert_registry", None)
        teams = getattr(orchestrator, "expert_team_registry", None)
    team = teams.get(team_id) if teams is not None else None
    if team is None:
        raise HTTPException(422, "EXPERT_TEAM_NOT_FOUND")
    coordinator_id = str(getattr(team, "coordinator_expert_id", "") or "")
    members = tuple(getattr(team, "member_expert_ids", ()) or ())
    if not coordinator_id or coordinator_id not in members or experts is None or experts.get(coordinator_id) is None:
        raise HTTPException(422, "EXPERT_TEAM_COORDINATOR_INVALID")
    return team


def _bind_input_team(context, orchestrator, team_id: str):
    """Persist an input-selected team without narrowing policy to one expert."""
    team = _team_for_context(context, orchestrator, team_id)
    coordinator_id = team.coordinator_expert_id
    context.session_meta["expert_team_id"] = team.team_id
    # Legacy components still require an active coordinator marker.  The
    # policy recognises input_team as team selection, not a direct-expert run.
    context.session_meta["expert_id"] = coordinator_id
    context.session_meta["active_expert_id"] = coordinator_id
    context.session_meta["expert_selection_source"] = "input_team"
    context.session_meta.pop("branch_expert_override", None)
    context.set_session_agent_selection(
        expert_team_id=team.team_id,
        expert_id=coordinator_id,
        selection_source="input_team",
    )
    context.apply_session_agent_selection()
    return team


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
        inputs.append({key: deepcopy(item.get(key)) for key in (
            "id", "title", "fields", "status", "round", "answer", "clarification_reason", "actor",
            "group_id", "group_position", "request_version"
        ) if key in item} | {"owner_node_id": id_map.get(item.get("owner_task_id"))})
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
    state.update({"dispatch": dispatch, "nodes": [projected_node(item) for item in source_nodes],
                  "inputs": inputs, "input_groups": groups})
    return state


def _payload(runtime, run, *, request_run_id: str, event: str, reason: str, cursor: int = 0):
    """Canonical V3 product frame with a request ID and stable bubble ID."""
    snapshot = runtime.store.public_snapshot(run["session_id"], run["user_id"], run["profile_id"])
    state = _project_product_state(snapshot, run)
    requested_team_ids = deepcopy(run.get("requested_expert_team_ids") or [run.get("policy", {}).get("team_id")])
    team_orchestration_version = str(run.get("team_orchestration_version") or "single_team_v1")
    state["expert_team_id"] = run.get("policy", {}).get("team_id")
    state["expert_team_ids"] = requested_team_ids
    state["team_orchestration_version"] = team_orchestration_version
    context = deepcopy(run.get("public_context") or {})
    payload = {
        "protocol": SSE_V3_PROTOCOL,
        "session_id": run["session_id"],
        # This echoes the BFF's current HTTP request id. It is intentionally
        # distinct from the persistent assistant_message_id below.
        "run_id": request_run_id,
        "assistant_message_id": run.get("assistant_message_id"),
        "expert_team_id": run.get("policy", {}).get("team_id"),
        "expert_team_ids": requested_team_ids,
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


def formal_v3_enabled() -> bool:
    """Keep public V3 on the candidate-test Harness execution runtime.

    Workbench's candidate-revision test panel creates V3 Harness runs from an
    immutable candidate snapshot. The conversation test console must use the
    same planner, expert-team delegation and Skill execution semantics; the
    formal adapter has different user-confirmed handoff behaviour and must
    not be selected here.
    """
    return False


def build_harness_chat_stream_v3_router(repository, fact_service, orchestrator, runtime, snapshot_resolver=None):
    router = APIRouter()
    formal_executor = FormalV3TurnExecutor(
        repository=repository, orchestrator=orchestrator, harness_runtime=runtime,
    )

    def operation(call):
        try:
            return call()
        except HarnessError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.code}) from exc

    async def stream(http_request: Request, harness_run_id: str, request_run_id: str):
        # V3 remains finite for a BFF, but background execution never depends
        # on this connection remaining open.
        timeout_seconds = max(1, int(os.getenv("HAILIANG_HARNESS_SSE_WAIT_SECONDS", "600")))
        started = time.monotonic()
        run = await asyncio.to_thread(runtime.store.get, harness_run_id)
        initial = await asyncio.to_thread(runtime.store.public_snapshot, run["session_id"], run["user_id"], run["profile_id"])
        cursor = initial["cursor"]
        # A terminal resume must replay only its durable final frame. It may
        # never make the BFF briefly render an active task again.
        if run["status"] in TERMINAL:
            yield format_sse_event("done", _payload(runtime, run, request_run_id=request_run_id, event="done", reason="terminal", cursor=cursor))
            return
        yield format_sse_event("state", _payload(runtime, run, request_run_id=request_run_id, event="state", reason="stream_started", cursor=cursor))
        while not await http_request.is_disconnected():
            run = await asyncio.to_thread(runtime.store.get, harness_run_id)
            if run["status"] in TERMINAL:
                yield format_sse_event("done", _payload(runtime, run, request_run_id=request_run_id, event="done", reason="terminal", cursor=cursor))
                return
            if _suspended(run):
                yield format_sse_event("done_suspended", _payload(runtime, run, request_run_id=request_run_id, event="done_suspended", reason="waiting_for_user", cursor=cursor))
                return
            events = await asyncio.to_thread(runtime.store.public_events, run["session_id"], run["user_id"], run["profile_id"], cursor)
            relevant = [event for event in events if event.get("run_id") == harness_run_id and event.get("type") in {"state", "done"}]
            if relevant:
                cursor = max(event["seq"] for event in events)
                latest = relevant[-1]
                run = await asyncio.to_thread(runtime.store.get, harness_run_id)
                event_name = "done" if latest.get("type") == "done" else "state"
                yield format_sse_event(event_name, _payload(runtime, run, request_run_id=request_run_id, event=event_name, reason=latest.get("reason", "updated"), cursor=cursor))
                if event_name == "done":
                    return
                continue
            if time.monotonic() - started >= timeout_seconds:
                yield format_sse_event("done_timeout", _payload(runtime, run, request_run_id=request_run_id, event="done_timeout", reason="stream_timeout", cursor=cursor))
                return
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
        profile_data = data.profile_data
        if profile_data is not None and not isinstance(profile_data, dict):
            raise HTTPException(422, "INVALID_PROFILE_DATA")

        if isinstance(input_data, ChatInput):
            team_id, team_ids = _single_supported_team_id(input_data.expert_team_id)
            fingerprint = _request_hash(input_data, profile_data, team_ids)
            bound = operation(lambda: runtime.store.client_run(request.session_id, data.user_id, data.profile_id, request.run_id))
            if bound:
                if bound["request_hash"] != fingerprint:
                    raise HTTPException(409, "CLIENT_RUN_ID_CONFLICT")
                harness_run_id = bound["harness_run_id"]
                run = operation(lambda: runtime.store.get(harness_run_id))
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
                if created and snapshot_resolver:
                    snapshot = snapshot_resolver()
                    if snapshot:
                        context.session_meta["configuration_snapshot"] = snapshot
                _bind_input_team(context, orchestrator, team_id)
                configure_candidate_compatible_strategy(context, {"object_type": "expert_team", "object_key": team_id})
                context.session_meta["context_contract_version"] = 3
                repository.save(context)
                if profile_data and data.profile_id:
                    aliases = {"grade": "grade", "province": "student_province", "recent_exam.total_score": "exam_score"}
                    operation(lambda: runtime.store.upsert_portrait(data.user_id, data.profile_id, profile_data, aliases=aliases))
                if runtime.moderation:
                    runtime.moderation.check(input_data.content, stage="input", session_id=request.session_id)
                policy = operation(lambda: capture_policy(orchestrator, context))
                history = []
                for old in sorted(runtime.store.runs(request.session_id), key=lambda item: item["created_at"])[-10:]:
                    if old["profile_id"] == data.profile_id and old["status"] == "completed":
                        history.extend([{"role": "user", "content": old["goal"]}, {"role": "assistant", "content": old.get("result", "")}])
                # 注入上一轮 Run 的结构化上下文
                if history:
                    last = max(runtime.store.runs(request.session_id), key=lambda r: r["created_at"])
                    if last.get("status") == "completed" and last.get("profile_id") == data.profile_id:
                        expert_summaries = []
                        for task in last.get("tasks", {}).values():
                            if task.get("kind") == "expert":
                                sr = task.get("structured_result") or {}
                                expert_summaries.append({
                                    "expert_id": task.get("expert_id"),
                                    "expert_name": (last.get("policy", {}).get("experts", {}).get(task.get("expert_id"), {}).get("name") or task.get("expert_id")),
                                    "status": task.get("status"),
                                    "findings": (sr.get("findings") or [])[:5],
                                    "limitations": (sr.get("limitations") or [])[:3],
                                })
                        history.append({
                            "role": "system",
                            "content": "上一轮执行上下文：" + json.dumps({
                                "last_round_goal": last.get("goal", ""),
                                "last_round_result_summary": (last.get("result") or "")[:300],
                                "orchestration": last.get("orchestration") or {},
                                "expert_summaries": expert_summaries,
                            }, ensure_ascii=False),
                        })
                harness_run_id = operation(lambda: runtime.create(
                    request.session_id, data.user_id, data.profile_id, input_data.content, policy,
                    _seed_facts(context, runtime, request.session_id, data.user_id, data.profile_id), history,
                    wire_protocol=SSE_V3_PROTOCOL,
                    execution_runtime="formal" if formal_v3_enabled() else None,
                ))
                bind_public_ids(harness_run_id, team_ids=team_ids, context=context, profile_data=profile_data, superseded=superseded)
                bind_request(request, input_data, harness_run_id, fingerprint)
                if formal_v3_enabled():
                    formal_executor.start(harness_run_id, message=input_data.content)
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
                    if run.get("execution_runtime") == "formal":
                        if not formal_executor.answer(
                            harness_run_id, request_id=input_data.request_id, answers=input_data.answers,
                            text=input_data.text, skipped=input_data.skipped,
                        ):
                            raise HTTPException(409, "INPUT_REQUEST_NOT_ACTIVE")
                    else:
                        submission = InternalAnswerSubmission(
                            user_id=data.user_id, profile_id=data.profile_id,
                            submission_id=input_data.submission_id, expected_version=input_data.expected_version,
                            answers=input_data.answers, text=input_data.text, skipped=input_data.skipped,
                        )
                        operation(lambda: runtime.answer(input_data.request_id, submission))
                elif run.get("execution_runtime") == "formal" and run.get("status") not in TERMINAL:
                    formal_executor.resume(harness_run_id, message="请继续完成当前专家团分析。")
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
