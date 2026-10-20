from __future__ import annotations

import asyncio
import json
import os
import hashlib
from time import monotonic
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from hailiang_skills.api.session_lifecycle import ContextData, open_or_resume_session
from hailiang_skills.harness.contracts import AnswerSubmission, HarnessError
from hailiang_skills.harness.policy import capture_policy, snapshot_root
from hailiang_skills.security.models import ModerationBlockedError
from hailiang_skills.skills.common import extract_grade


class StartRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str | None = Field(default=None, min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=120000)
    context_data: ContextData
    debug_session_id: str | None = None
    expert_team_id: str | None = None
    expert_id: str | None = None
    clear_experts: bool = False


class Identity(BaseModel):
    user_id: str = Field(min_length=1)
    profile_id: str | None = None


def harness_enabled():
    return os.getenv("HAILIANG_HARNESS_NEW_SESSIONS", "true").lower() not in {"0", "false", "off"}


def build_harness_router(repository, fact_service, orchestrator, runtime, snapshot_resolver=None, debug_snapshot_resolver=None, protocol_v3=False):
    router = APIRouter()

    def v3_ready():
        if protocol_v3 and os.getenv('HAILIANG_SSE_V3', 'false').lower() not in {'true', '1', 'on'}:
            raise HTTPException(404, 'SSE_V3_DISABLED')


    def access(session_id, user_id, profile_id=None):
        v3_ready()
        try:
            context = repository.get(session_id)
        except KeyError:
            retained = runtime.store.runs(session_id)
            if any(run["user_id"] != user_id for run in retained):
                raise HTTPException(403, "HARNESS_ACCESS_DENIED")
            return None
        if context.user_id != user_id:
            raise HTTPException(403, "HARNESS_ACCESS_DENIED")
        if runtime.store.active(session_id) and context.profile_id != profile_id:
            raise HTTPException(409, "ACTIVE_RUN_PROFILE_LOCKED")
        return context

    def operation(call):
        try:
            return call()
        except HarnessError as exc:
            raise HTTPException(exc.status, {"code": exc.code, "message": exc.code}) from exc

    @router.get('/sessions/{session_id}/harness/capabilities')
    def capabilities(session_id: str):
        enabled = protocol_v3 and os.getenv('HAILIANG_SSE_V3', 'false').lower() in {'true', '1', 'on'}
        return {'protocols': ['hailiang.sse.v3'] if enabled else [], 'streaming_text': enabled,
                'replay': enabled, 'partial_answers': True, 'finite_run_stream': enabled,
                'finite_run_stream_default_seconds': 290 if enabled else None}

    @router.get("/sessions/{session_id}/harness")
    def state(session_id: str, user_id: str, profile_id: str | None = None):
        context = access(session_id, user_id, profile_id)
        mode = context.session_meta.get("runtime_mode", "legacy") if context else ("harness_v1" if harness_enabled() else "legacy")
        if protocol_v3:
            snapshot = runtime.store.public_snapshot(session_id, user_id, profile_id)
            snapshot['runtime_mode'] = mode
            return snapshot
        # Reading cursor first ensures replay can only duplicate, never lose an update.
        cursor = runtime.store.cursor(session_id)
        runs = [runtime.public(run) for run in runtime.store.runs(session_id) if run["profile_id"] == profile_id and run["user_id"] == user_id]
        return {"runtime_mode": mode, "cursor": cursor, "runs": runs}

    @router.post("/sessions/{session_id}/harness/runs", status_code=202)
    def start(session_id: str, request: StartRun):
        data = request.context_data
        context = access(session_id, data.user_id, data.profile_id)
        submission = None
        if protocol_v3:
            if not request.submission_id:
                raise HTTPException(422, 'SUBMISSION_ID_REQUIRED')
            from hailiang_skills.harness.protocol_v3 import scope_key
            # Submission identity includes session+user; profile is in fingerprint.
            key = scope_key(session_id, data.user_id, request.submission_id)
            fingerprint = hashlib.sha256(json.dumps(request.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            prior = operation(lambda: runtime.store.submission(key, fingerprint))
            if prior:
                saved = runtime.store.get(prior)
                return {'run_id': prior, 'runtime_mode': 'harness_v1', 'status': saved['status'],
                        'superseded_run_id': saved.get('supersedes_run_id')}
            submission = {'key': key, 'fingerprint': fingerprint}
        if runtime.store.active(session_id) and not protocol_v3:
            raise HTTPException(409, "ACTIVE_HARNESS_RUN_EXISTS")
        if context and context.session_meta.get("runtime_mode", "legacy") != "harness_v1":
            raise HTTPException(409, "LEGACY_SESSION_REQUIRES_LEGACY_API")
        if context is None and not harness_enabled():
            raise HTTPException(409, "HARNESS_NEW_SESSIONS_DISABLED")
        if context and context.session_meta.get("workbench_debug_session_id") != request.debug_session_id:
            raise HTTPException(409, "HARNESS_DEBUG_SNAPSHOT_MISMATCH")
        if data.profile_id:
            lookup = getattr(fact_service.profile_repo, "get_profile_by_id", None)
            if lookup:
                try:
                    profile = lookup(data.profile_id)
                    if profile.get("user_id") and profile["user_id"] != data.user_id:
                        raise HTTPException(403, "PROFILE_ACCESS_DENIED")
                    listing = getattr(fact_service.profile_repo, "list_profiles", None)
                    if listing and data.profile_id not in {p["profile_id"] for p in listing(data.user_id)}:
                        raise HTTPException(403, "PROFILE_ACCESS_DENIED")
                except KeyError:
                    pass
        if runtime.moderation:
            try:
                runtime.moderation.check(request.goal, stage="input", session_id=session_id)
            except ModerationBlockedError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "CONTENT_BLOCKED",
                        "message": "该内容检测到非合规内容，请重新输入",
                        "stage": exc.stage,
                        "case_id": exc.case_id,
                        **exc.result.to_public_dict(),
                    },
                ) from exc
        context, created = open_or_resume_session(repository, fact_service, session_id=session_id, data=data,
                                                  context_scope="profile" if data.profile_id else "unbound", allow_context_seed=context is None)
        if created:
            context.session_meta["runtime_mode"] = "harness_v1"
            repository.save(context)
            snapshot = debug_snapshot_resolver(request.debug_session_id) if request.debug_session_id and debug_snapshot_resolver else (snapshot_resolver() if snapshot_resolver else None)
            if snapshot:
                context.session_meta["configuration_snapshot"] = snapshot
                root = snapshot_root(snapshot)
                if root.get("object_type") == "expert_team":
                    context.session_meta["expert_team_id"] = root["object_key"]
            if request.debug_session_id:
                context.session_meta["workbench_debug_session_id"] = request.debug_session_id
        if request.clear_experts:
            context.session_meta.update(harness_experts_disabled=True, expert_team_id=None, expert_id=None)
        elif request.expert_team_id or request.expert_id:
            context.session_meta["harness_experts_disabled"] = False
            if request.expert_team_id:
                context.session_meta["expert_team_id"] = request.expert_team_id
                context.session_meta["expert_id"] = None
            if request.expert_id:
                context.session_meta["expert_id"] = request.expert_id
        policy = operation(lambda: capture_policy(orchestrator, context))
        context.session_meta["context_contract_version"] = 3
        if not context.title:
            context.title = request.goal[:40]
        repository.save(context)
        seed = {}
        from hailiang_skills.harness.store import HarnessStore
        dummy = {"session_id": session_id, "user_id": data.user_id, "profile_id": data.profile_id}
        for scope, facts in [("user", context.shared_facts), ("profile", context.profile_facts), ("session", context.session_facts)]:
            for key, record in facts.facts.items():
                identity = HarnessStore.fact_identity(dummy, {"id": "seed"}, scope, key)
                seed[identity] = {"key": key, "value": record.value, "scope": scope, "status": record.status, "version": 1, "source": record.model_dump()}
        explicit_grade = extract_grade(request.goal)
        if explicit_grade:
            identity = HarnessStore.fact_identity(dummy, {"id": "seed"}, "session", "grade")
            seed[identity] = {
                "key": "grade",
                "value": explicit_grade,
                "scope": "session",
                "status": "confirmed",
                "version": 1,
                "source": {
                    "source_type": "user_input",
                    "source_id": f"{session_id}:goal",
                    "evidence": explicit_grade,
                },
            }
        previous = runtime.store.runs(session_id)
        history = []
        for run in sorted(previous, key=lambda r: r["created_at"])[-10:]:
            # A superseded/failed/in-flight run is retained for audit and its
            # final record, but its drafts and intermediate conclusions must not
            # become conversational history for the replacement run.
            eligible = run["status"] == "completed" if protocol_v3 else bool(run.get("result"))
            if run["profile_id"] == data.profile_id and eligible:
                history.append({"role": "user", "content": run["goal"]})
                if run.get("result"):
                    history.append({"role": "assistant", "content": run["result"]})
        run_id = operation(lambda: runtime.create(session_id, data.user_id, data.profile_id, request.goal, policy, seed, history,
            wire_protocol="hailiang.sse.v3" if protocol_v3 else None, submission=submission,
            supersede_active=protocol_v3))
        created_run = runtime.store.get(run_id)
        return {"run_id": run_id, "runtime_mode": "harness_v1", "status": "running",
                "superseded_run_id": created_run.get("supersedes_run_id")}

    @router.get("/sessions/{session_id}/harness/runs/{run_id}/trace")
    def trace(session_id: str, run_id: str, user_id: str, profile_id: str | None = None):
        from hailiang_skills.core.deployment import deployment_environment
        context = access(session_id, user_id, profile_id)
        if deployment_environment().startswith("prod") or context is None or not context.session_meta.get("workbench_debug_session_id"):
            raise HTTPException(403, "HARNESS_TRACE_REQUIRES_DEBUG_SESSION")
        run = operation(lambda: runtime.store.get(run_id))
        if run["session_id"] != session_id or run["user_id"] != user_id or run["profile_id"] != profile_id:
            raise HTTPException(403, "HARNESS_ACCESS_DENIED")
        return {"run_id": run_id, "context_policy_version": run.get("context_policy_version", "v1"),
                "input_tokens_estimated": run.get("input_tokens_estimated", 0),
                "input_tokens_actual": run.get("input_tokens_actual", 0),
                "input_tokens_charged": run.get("input_tokens_charged", run.get("input_tokens_estimated", 0)),
                "tasks": [{"task_id": t["id"], "kind": t["kind"], "trace": t["trace"]} for t in run["tasks"].values()]}

    @router.get("/sessions/{session_id}/harness/runs/{run_id}")
    def get_run(session_id: str, run_id: str, user_id: str, profile_id: str | None = None):
        access(session_id, user_id, profile_id)
        run = operation(lambda: runtime.store.get(run_id))
        if run["session_id"] != session_id or run["user_id"] != user_id or run["profile_id"] != profile_id:
            raise HTTPException(403, "HARNESS_ACCESS_DENIED")
        if protocol_v3:
            return runtime.store.public_snapshot(session_id, user_id, profile_id)
        return runtime.public(run)

    @router.get("/sessions/{session_id}/harness/runs/{run_id}/record")
    def get_record(session_id: str, run_id: str, user_id: str, profile_id: str | None = None):
        access(session_id, user_id, profile_id)
        if not protocol_v3:
            raise HTTPException(404, "SSE_V3_RECORD_NOT_AVAILABLE")
        return operation(lambda: runtime.store.public_record(session_id, user_id, profile_id, run_id))

    @router.get("/sessions/{session_id}/harness/runs/{run_id}/events")
    async def run_events(session_id: str, run_id: str, request: Request, user_id: str,
                         profile_id: str | None = None, after: int = 0):
        """Finite per-run SSE for forwarders with bounded request lifetimes."""
        access(session_id, user_id, profile_id)
        if not protocol_v3:
            raise HTTPException(404, "SSE_V3_RUN_STREAM_NOT_AVAILABLE")
        run = operation(lambda: runtime.store.get(run_id))
        if run["session_id"] != session_id or run["user_id"] != user_id or run["profile_id"] != profile_id:
            raise HTTPException(403, "HARNESS_ACCESS_DENIED")
        try:
            cursor = max(after, int(request.headers.get("Last-Event-ID", "0")))
        except ValueError:
            raise HTTPException(400, "INVALID_EVENT_CURSOR")
        try:
            wait_seconds = float(os.getenv("HAILIANG_SSE_V3_RUN_STREAM_SECONDS", "600"))
        except ValueError:
            wait_seconds = 600.0
        wait_seconds = min(600.0, max(0.1, wait_seconds))
        initial_snapshot = "after" not in request.query_params and not request.headers.get("Last-Event-ID")

        def filtered_snapshot(snapshot):
            return {
                **snapshot,
                "runs": [item for item in snapshot["runs"] if item["id"] == run_id],
                "nodes": [item for item in snapshot["nodes"] if item.get("run_id") == run_id],
                "input_requests": [item for item in snapshot["input_requests"] if item.get("run_id") == run_id],
                "messages": [item for item in snapshot["messages"] if item.get("run_id") == run_id],
            }

        def persistence(kind, public_run):
            if kind == 'state':
                return {'should_store_snapshot': True, 'should_save_history': False, 'replace_staged': False,
                        'should_discard_staged': False, 'terminal': False, 'record_kind': 'progress'}
            if kind == 'done_suspended':
                return {'should_store_snapshot': True, 'should_save_history': True, 'replace_staged': True,
                        'should_discard_staged': False, 'terminal': False, 'record_kind': 'waiting_for_user'}
            if kind == 'done_timeout':
                return {'should_store_snapshot': True, 'should_save_history': True, 'replace_staged': True,
                        'should_discard_staged': False, 'terminal': False, 'record_kind': 'running'}
            if public_run.get('status') == 'cancelled':
                return {'should_store_snapshot': False, 'should_save_history': False, 'replace_staged': False,
                        'should_discard_staged': True, 'terminal': True, 'record_kind': 'cancelled'}
            return {'should_store_snapshot': True, 'should_save_history': True, 'replace_staged': True,
                    'should_discard_staged': False, 'terminal': True, 'record_kind': 'final'}

        def wire_state(snapshot, kind='state', reason='updated', extra=None):
            """V2-shaped full snapshot plus explicit BFF persistence instruction."""
            filtered = filtered_snapshot(snapshot)
            public_run = next((item for item in filtered['runs'] if item['id'] == run_id), {})
            payload = {
                'protocol': 'hailiang.sse.v3', 'run_id': run_id, 'harness_run_id': run_id,
                'client_run_id': public_run.get('client_run_id'),
                'assistant_message_id': public_run.get('assistant_message_id') or f'msg_harness_{run_id}',
                'record_version': public_run.get('record_version', public_run.get('version', 0)),
                'seq': filtered['cursor'], 'session_id': session_id, 'profile_id': profile_id,
                'status': public_run.get('status', 'running'), 'outcome': public_run.get('outcome'),
                'stream_status': 'waiting_for_user' if kind == 'done_suspended' else ('running' if kind == 'done_timeout' else public_run.get('status', 'running')),
                'reason': reason, 'timestamp': datetime.now(timezone.utc).isoformat(),
                'persistence': persistence(kind, public_run), 'snapshot': filtered,
            }
            if extra:
                payload.update(extra)
            return payload

        def filtered_wire_event(item):
            # State/done snapshots are persisted with all entities so a replay is
            # immutable. A run stream exposes only its own public subtree.
            payload = dict(item)
            if 'snapshot' in payload:
                payload['snapshot'] = filtered_snapshot(payload['snapshot'])
            public_run = next((row for row in payload.get('snapshot', {}).get('runs', []) if row['id'] == run_id), {})
            kind = 'done' if payload.get('type') == 'done' else 'state'
            payload.update({
                'harness_run_id': run_id, 'client_run_id': public_run.get('client_run_id'),
                'assistant_message_id': public_run.get('assistant_message_id') or f'msg_harness_{run_id}',
                'record_version': public_run.get('record_version', public_run.get('version', 0)),
                'stream_status': public_run.get('status', 'running'),
                'persistence': persistence(kind, public_run),
            })
            return payload

        async def finite_stream():
            nonlocal cursor
            started = monotonic()
            public_run = None
            if initial_snapshot:
                snapshot = filtered_snapshot(await asyncio.to_thread(
                    runtime.store.public_snapshot, session_id, user_id, profile_id))
                cursor = snapshot["cursor"]
                public_run = next((item for item in snapshot["runs"] if item["id"] == run_id), None)
                envelope = wire_state(snapshot)
                yield f"id: {cursor}\nevent: state\ndata: {json.dumps(envelope, ensure_ascii=False)}\n\n"
                if public_run and public_run["status"] in {"completed", "failed", "cancelled"}:
                    record = await asyncio.to_thread(runtime.store.public_record, session_id, user_id, profile_id, run_id)
                    event = wire_state(snapshot, 'done', 'completed', {
                        'record_version': record['record_version'],
                        'record_url': f'/api/v3/sessions/{session_id}/harness/runs/{run_id}/record',
                        'final_message_id': record['assistant_message']['id'],
                        'final_message_version': record['assistant_message'].get('version', 0),
                    })
                    yield f"id: {cursor}\nevent: done\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    return
                if public_run and public_run["status"] == "waiting_for_user":
                    pending = [item["id"] for item in snapshot["input_requests"] if item["status"] == "pending"]
                    event = wire_state(snapshot, 'done_suspended', 'waiting_for_user', {
                        'resume_after': cursor, 'pending_request_ids': pending,
                    })
                    yield f"id: {cursor}\nevent: done_suspended\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    return
            last_heartbeat = monotonic()
            while not await request.is_disconnected():
                try:
                    batch = await asyncio.to_thread(
                        runtime.store.public_events, session_id, user_id, profile_id, cursor)
                except HarnessError:
                    snapshot = filtered_snapshot(await asyncio.to_thread(runtime.store.public_snapshot, session_id, user_id, profile_id))
                    event = wire_state(snapshot, 'state', 'resync_required')
                    yield f"id: {event['seq']}\nevent: state\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    return
                for item in batch:
                    cursor = item["seq"]
                    if item.get("run_id") != run_id:
                        continue
                    if item['type'] not in {'state', 'done'}:
                        continue
                    event = filtered_wire_event(item)
                    public_run = next((row for row in event['snapshot']['runs'] if row['id'] == run_id), public_run)
                    yield f"id: {cursor}\nevent: {item['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if item['type'] == 'done':
                        return
                if monotonic() - started >= wait_seconds:
                    snapshot = filtered_snapshot(await asyncio.to_thread(runtime.store.public_snapshot, session_id, user_id, profile_id))
                    event = wire_state(snapshot, 'done_timeout', 'forwarder_wait_limit', {'resume_after': cursor})
                    yield f"id: {cursor}\nevent: done_timeout\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    return
                if not batch:
                    snapshot = filtered_snapshot(await asyncio.to_thread(
                        runtime.store.public_snapshot, session_id, user_id, profile_id))
                    public_run = next((item for item in snapshot["runs"] if item["id"] == run_id), public_run)
                    if public_run and public_run["status"] == "waiting_for_user":
                        pending = [item["id"] for item in snapshot["input_requests"] if item["status"] == "pending"]
                        event = wire_state(snapshot, 'done_suspended', 'waiting_for_user', {
                            'resume_after': cursor, 'pending_request_ids': pending,
                        })
                        yield f"id: {cursor}\nevent: done_suspended\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                        return
                    if monotonic() - last_heartbeat >= 15:
                        yield ": heartbeat\n\n"
                        last_heartbeat = monotonic()
                    await asyncio.sleep(.1)

        return StreamingResponse(finite_stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
            "X-SSE-Protocol": "hailiang.sse.v3", "X-SSE-Mode": "run",
        })

    @router.post("/sessions/{session_id}/input-requests/{request_id}/answers", status_code=202)
    async def answer(session_id: str, request_id: str, submission: AnswerSubmission, request: Request):
        """Answer a card and, when requested, continue the same turn as SSE."""
        access(session_id, submission.user_id, submission.profile_id)
        run = operation(lambda: runtime.store.get(runtime.store.request_run(request_id)))
        if run["session_id"] != session_id:
            raise HTTPException(403, "HARNESS_ACCESS_DENIED")
        if runtime.moderation and not submission.skipped:
            runtime.moderation.check(json.dumps(submission.answers, ensure_ascii=False) + (submission.text or ""), stage="input", session_id=session_id)
        result = operation(lambda: runtime.answer(request_id, submission))
        # The card submit itself becomes the continuation stream. This keeps
        # resume cursor handling inside the BFF instead of exposing it to UI.
        if protocol_v3 and 'text/event-stream' in request.headers.get('accept', ''):
            resumed = runtime.store.get(run['run_id'])
            return await run_events(session_id, resumed['run_id'], request, submission.user_id, submission.profile_id, 0)
        return result

    @router.post("/sessions/{session_id}/harness/runs/{run_id}/cancel")
    def cancel(session_id: str, run_id: str, identity: Identity):
        access(session_id, identity.user_id, identity.profile_id)
        run = operation(lambda: runtime.store.get(run_id))
        if run["session_id"] != session_id or run["user_id"] != identity.user_id or run["profile_id"] != identity.profile_id:
            raise HTTPException(403, "HARNESS_ACCESS_DENIED")
        runtime.cancel(run_id)
        return runtime.public(runtime.store.get(run_id))

    @router.get("/sessions/{session_id}/harness/events")
    async def events(session_id: str, request: Request, user_id: str, profile_id: str | None = None, after: int = 0):
        access(session_id, user_id, profile_id)
        if protocol_v3:
            from hailiang_skills.harness.protocol_v3 import PROTOCOL
            try:
                cursor = max(after, int(request.headers.get('Last-Event-ID', '0')))
            except ValueError:
                raise HTTPException(400, 'INVALID_EVENT_CURSOR')
            async def v3_stream():
                nonlocal cursor
                last_heartbeat = 0.0
                if 'after' not in request.query_params and not request.headers.get('Last-Event-ID'):
                    snap = await asyncio.to_thread(runtime.store.public_snapshot, session_id, user_id, profile_id)
                    cursor = snap['cursor']
                    event = {'protocol': PROTOCOL, 'seq': cursor, 'session_id': session_id,
                             'profile_id': profile_id, 'run_id': None, 'status': 'running', 'outcome': None,
                             'reason': 'updated', 'timestamp': datetime.now(timezone.utc).isoformat(), 'snapshot': snap}
                    yield f"id: {cursor}\nevent: state\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                while not await request.is_disconnected():
                    try:
                        batch = await asyncio.to_thread(runtime.store.public_events, session_id, user_id, profile_id, cursor)
                    except HarnessError:
                        yield 'event: resync_required\ndata: ' + json.dumps({'protocol': PROTOCOL, 'type': 'resync_required', 'seq': max(0, cursor), 'event_id': f'resync:{cursor}', 'run_id': None, 'entity_id': None, 'entity_version': 0, 'timestamp': datetime.now(timezone.utc).isoformat(), 'session_id': session_id, 'profile_id': profile_id, 'data': {'reason': 'INVALID_EVENT_CURSOR'}}) + '\n\n'
                        return
                    for item in batch:
                        cursor = item['seq']
                        # Internal entity events are not part of the V3 public
                        # compatibility contract. Every exposed frame is a full state.
                        if item['type'] not in {'state', 'done'}:
                            continue
                        yield f"id: {cursor}\nevent: {item['type']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                    if not batch:
                        if monotonic() - last_heartbeat >= 15:
                            yield ': heartbeat\n\n'
                            last_heartbeat = monotonic()
                        await asyncio.sleep(.1)
            return StreamingResponse(v3_stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'X-SSE-Protocol': PROTOCOL})
        try:
            after = max(0, after, int(request.headers.get("Last-Event-ID", "0")))
        except ValueError:
            raise HTTPException(400, "INVALID_EVENT_CURSOR")
        async def stream():
            cursor = after
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(runtime.store.events, session_id, cursor)
                for item in batch:
                    cursor = item["seq"]
                    run = await asyncio.to_thread(runtime.store.get, item["run_id"])
                    if run["user_id"] != user_id or run["profile_id"] != profile_id:
                        continue
                    yield f"id: {cursor}\nevent: harness\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                if not batch:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
