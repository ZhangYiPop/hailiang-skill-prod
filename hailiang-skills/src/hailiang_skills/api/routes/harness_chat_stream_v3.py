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
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hailiang_skills.api.session_lifecycle import ContextData, open_or_resume_session
from hailiang_skills.harness.contracts import InternalAnswerSubmission, HarnessError, TERMINAL
from hailiang_skills.harness.policy import capture_policy


SSE_V3_PROTOCOL = "hailiang.sse.v3"


def format_sse_event(event: str, data: dict) -> str:
    """Keep the public adapter independent from the legacy streaming runtime."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TeamBoundInput(StrictInput):
    # V3 always makes the business entry team explicit.  The server owns all
    # member/Skill routing below it; callers never select an expert or Skill.
    expert_team_id: str = Field(min_length=1, max_length=100)


class ChatInput(TeamBoundInput):
    action: Literal["chat"]
    content: str = Field(min_length=1, max_length=120000)


class AnswerInput(TeamBoundInput):
    action: Literal["answer_input_request"]
    request_id: str = Field(min_length=1, max_length=80)
    answers: dict[str, Any] = Field(default_factory=dict)
    text: str | None = Field(default=None, max_length=120000)
    skipped: bool = False


class StopInput(TeamBoundInput):
    action: Literal["stop"]


class V3ChatStreamRequest(StrictInput):
    session_id: str = Field(min_length=1, max_length=80)
    run_id: str = Field(min_length=1, max_length=100)
    input: str = Field(min_length=1)
    context_data: ContextData


def _parse_input(raw: str) -> ChatInput | AnswerInput | StopInput:
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
        if action == "stop":
            return StopInput.model_validate(value)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors()) from exc
    raise HTTPException(422, "HARNESS_V3_ACTION_UNSUPPORTED")


def _request_hash(input_data, profile_data):
    encoded = json.dumps({"input": input_data.model_dump(), "profile_data": profile_data}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _profile_name(profile_data: dict | None, profile_id: str) -> str:
    value = (profile_data or {}).get("name")
    return value.strip() if isinstance(value, str) and value.strip() else f"未命名孩子-{profile_id}"


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


def _payload(runtime, run, *, event: str, reason: str, cursor: int = 0):
    """Return the canonical public V3 full frame for the minimal BFF entry."""
    snapshot = runtime.store.public_snapshot(run["session_id"], run["user_id"], run["profile_id"])
    run_id = run["run_id"]
    # A BFF stream is scoped to one run. Never carry another run's queued form
    # groups into its bubble merely because they share the session/profile scope.
    snapshot = {
        **snapshot,
        "runs": [item for item in snapshot["runs"] if item["id"] == run_id],
        "nodes": [item for item in snapshot["nodes"] if item.get("run_id") == run_id],
        "input_requests": [item for item in snapshot["input_requests"] if item.get("run_id") == run_id],
        "input_groups": [item for item in snapshot.get("input_groups", []) if item.get("run_id") == run_id],
        "messages": [item for item in snapshot["messages"] if item.get("run_id") == run_id],
    }
    public_run = next((item for item in snapshot["runs"] if item["id"] == run_id), {})
    return {
        "protocol": SSE_V3_PROTOCOL,
        "session_id": run["session_id"],
        # run_id remains for older V2-shaped BFF code. The two explicit IDs
        # remove ambiguity between its idempotency key and the Harness ID.
        "run_id": run_id,
        "harness_run_id": run_id,
        "client_run_id": public_run.get("client_run_id", run.get("client_run_id")),
        "assistant_message_id": public_run.get("assistant_message_id") or run.get("assistant_message_id") or f"msg_harness_{run_id}",
        "record_version": public_run.get("record_version", run["version"]),
        "seq": cursor,
        "cursor": cursor,
        "reason": reason,
        "stream_status": "waiting_for_user" if event == "done_suspended" else ("running" if event == "done_timeout" else run["status"]),
        "outcome": _outcome(run),
        "persistence": _persistence(event=event, run=run),
        "snapshot": snapshot,
    }


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

    async def stream(http_request: Request, harness_run_id: str):
        # V3 remains a finite BFF request, but ten minutes gives multi-agent
        # work a practical default before emitting a resumable checkpoint.
        timeout_seconds = max(1, int(os.getenv("HAILIANG_HARNESS_SSE_WAIT_SECONDS", "600")))
        started = time.monotonic()
        run = await asyncio.to_thread(runtime.store.get, harness_run_id)
        initial = await asyncio.to_thread(runtime.store.public_snapshot, run["session_id"], run["user_id"], run["profile_id"])
        cursor = initial["cursor"]
        yield format_sse_event("state", _payload(runtime, run, event="state", reason="stream_started", cursor=cursor))
        while not await http_request.is_disconnected():
            run = await asyncio.to_thread(runtime.store.get, harness_run_id)
            if run["status"] in TERMINAL:
                yield format_sse_event("done", _payload(runtime, run, event="done", reason="terminal", cursor=cursor))
                return
            if _suspended(run):
                # This is a durable checkpoint: BFF stores it as a pending
                # assistant record, then resumes with the same client_run_id.
                yield format_sse_event("done_suspended", _payload(runtime, run, event="done_suspended", reason="waiting_for_user", cursor=cursor))
                return
            events = await asyncio.to_thread(runtime.store.public_events, run["session_id"], run["user_id"], run["profile_id"], cursor)
            relevant = [event for event in events if event.get("run_id") == harness_run_id and event.get("type") in {"state", "done"}]
            if relevant:
                cursor = max(event["seq"] for event in events)
                latest = relevant[-1]
                run = await asyncio.to_thread(runtime.store.get, harness_run_id)
                event_name = "done" if latest.get("type") == "done" else "state"
                yield format_sse_event(event_name, _payload(runtime, run, event=event_name, reason=latest.get("reason", "updated"), cursor=cursor))
                if event_name == "done":
                    return
                continue
            if time.monotonic() - started >= timeout_seconds:
                # A finite BFF connection timing out is also a persistence
                # checkpoint, never a claim that the background task failed.
                yield format_sse_event("done_timeout", _payload(runtime, run, event="done_timeout", reason="stream_timeout", cursor=cursor))
                return
            await asyncio.sleep(0.2)

    @router.post("/sessions/chat/stream")
    async def post_chat_stream(request: V3ChatStreamRequest, http_request: Request):
        input_data = _parse_input(request.input)
        data = request.context_data
        profile_data = data.profile_data
        if profile_data is not None and not isinstance(profile_data, dict):
            raise HTTPException(422, "INVALID_PROFILE_DATA")

        if isinstance(input_data, ChatInput):
            fingerprint = _request_hash(input_data, profile_data)
            bound = operation(lambda: runtime.store.client_run(request.session_id, data.user_id, data.profile_id, request.run_id))
            if bound:
                if bound["request_hash"] != fingerprint:
                    raise HTTPException(409, "CLIENT_RUN_ID_CONFLICT")
                harness_run_id = bound["harness_run_id"]
            else:
                active = runtime.store.active(request.session_id)
                if active:
                    if active["user_id"] != data.user_id:
                        raise HTTPException(409, "ACTIVE_HARNESS_RUN_OWNED_BY_OTHER")
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
                _bind_input_team(context, orchestrator, input_data.expert_team_id)
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
                harness_run_id = operation(lambda: runtime.create(request.session_id, data.user_id, data.profile_id, input_data.content, policy, _seed_facts(context, runtime, request.session_id, data.user_id, data.profile_id), history, wire_protocol=SSE_V3_PROTOCOL))
                harness_run_id = operation(lambda: runtime.store.bind_client_run(request.session_id, data.user_id, data.profile_id, request.run_id, harness_run_id, fingerprint))
            # Stored on the run so a retry, form answer and final reply all
            # address one stable assistant bubble.
            def bind_public_ids(run, changes, _db):
                changed = False
                if run.get("client_run_id") != request.run_id:
                    run["client_run_id"] = request.run_id
                    changed = True
                if not run.get("assistant_message_id"):
                    run["assistant_message_id"] = f"msg_harness_{run['run_id']}"
                    changed = True
                if changed:
                    changes.append({"event": "assistant_message_bound", "node_id": run["root_task_id"]})
            operation(lambda: runtime.store.mutate(harness_run_id, bind_public_ids))
        elif isinstance(input_data, AnswerInput):
            harness_run_id = operation(lambda: runtime.store.request_run(input_data.request_id))
            run = operation(lambda: runtime.store.get(harness_run_id))
            if run["session_id"] != request.session_id or run["user_id"] != data.user_id or run["profile_id"] != data.profile_id:
                raise HTTPException(403, "HARNESS_ACCESS_DENIED")
            _assert_run_team(run, input_data.expert_team_id)
            if profile_data and data.profile_id:
                aliases = {"grade": "grade", "province": "student_province", "recent_exam.total_score": "exam_score"}
                operation(lambda: runtime.store.upsert_portrait(data.user_id, data.profile_id, profile_data, aliases=aliases))
            submission = InternalAnswerSubmission(user_id=data.user_id, profile_id=data.profile_id, answers=input_data.answers,
                                                   text=input_data.text, skipped=input_data.skipped)
            operation(lambda: runtime.answer(input_data.request_id, submission, allow_partial=True, allow_correction=True))
        else:
            bound = operation(lambda: runtime.store.client_run(request.session_id, data.user_id, data.profile_id, request.run_id))
            if not bound:
                raise HTTPException(404, "HARNESS_CLIENT_RUN_NOT_FOUND")
            harness_run_id = bound["harness_run_id"]
            run = operation(lambda: runtime.store.get(harness_run_id))
            if run["session_id"] != request.session_id or run["user_id"] != data.user_id or run["profile_id"] != data.profile_id:
                raise HTTPException(403, "HARNESS_ACCESS_DENIED")
            _assert_run_team(run, input_data.expert_team_id)
            runtime.cancel(harness_run_id)
        return StreamingResponse(stream(http_request, harness_run_id), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no", "X-SSE-Protocol": SSE_V3_PROTOCOL,
        })

    return router
