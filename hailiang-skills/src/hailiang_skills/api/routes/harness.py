from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from hailiang_skills.api.session_lifecycle import ContextData, open_or_resume_session
from hailiang_skills.harness.contracts import AnswerSubmission, HarnessError
from hailiang_skills.harness.policy import capture_policy


class StartRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
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


def build_harness_router(repository, fact_service, orchestrator, runtime, snapshot_resolver=None, debug_snapshot_resolver=None):
    router = APIRouter()

    def access(session_id, user_id, profile_id=None):
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

    @router.get("/sessions/{session_id}/harness")
    def state(session_id: str, user_id: str, profile_id: str | None = None):
        context = access(session_id, user_id, profile_id)
        mode = context.session_meta.get("runtime_mode", "legacy") if context else ("harness_v1" if harness_enabled() else "legacy")
        # Reading cursor first ensures replay can only duplicate, never lose an update.
        cursor = runtime.store.cursor(session_id)
        runs = [runtime.public(run) for run in runtime.store.runs(session_id) if run["profile_id"] == profile_id and run["user_id"] == user_id]
        return {"runtime_mode": mode, "cursor": cursor, "runs": runs}

    @router.post("/sessions/{session_id}/harness/runs", status_code=202)
    def start(session_id: str, request: StartRun):
        data = request.context_data
        context = access(session_id, data.user_id, data.profile_id)
        if runtime.store.active(session_id):
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
            runtime.moderation.check(request.goal, stage="input", session_id=session_id)
        context, created = open_or_resume_session(repository, fact_service, session_id=session_id, data=data,
                                                  context_scope="profile" if data.profile_id else "unbound", allow_context_seed=context is None)
        if created:
            context.session_meta["runtime_mode"] = "harness_v1"
            repository.save(context)
            snapshot = debug_snapshot_resolver(request.debug_session_id) if request.debug_session_id and debug_snapshot_resolver else (snapshot_resolver() if snapshot_resolver else None)
            if snapshot:
                context.session_meta["configuration_snapshot"] = snapshot
                root = snapshot.get("root", {})
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
        previous = runtime.store.runs(session_id)
        history = []
        for run in sorted(previous, key=lambda r: r["created_at"])[-10:]:
            if run["profile_id"] == data.profile_id:
                history.append({"role": "user", "content": run["goal"]})
                if run.get("result"):
                    history.append({"role": "assistant", "content": run["result"]})
        run_id = operation(lambda: runtime.create(session_id, data.user_id, data.profile_id, request.goal, policy, seed, history))
        return {"run_id": run_id, "runtime_mode": "harness_v1", "status": "running"}

    @router.get("/sessions/{session_id}/harness/runs/{run_id}/trace")
    def trace(session_id: str, run_id: str, user_id: str, profile_id: str | None = None):
        from hailiang_skills.core.deployment import deployment_environment
        context = access(session_id, user_id, profile_id)
        if deployment_environment().startswith("prod") or context is None or not context.session_meta.get("workbench_debug_session_id"):
            raise HTTPException(403, "HARNESS_TRACE_REQUIRES_DEBUG_SESSION")
        run = operation(lambda: runtime.store.get(run_id))
        if run["session_id"] != session_id or run["user_id"] != user_id or run["profile_id"] != profile_id:
            raise HTTPException(403, "HARNESS_ACCESS_DENIED")
        return {"run_id": run_id, "tasks": [{"task_id": t["id"], "kind": t["kind"], "trace": t["trace"]} for t in run["tasks"].values()]}

    @router.get("/sessions/{session_id}/harness/runs/{run_id}")
    def get_run(session_id: str, run_id: str, user_id: str, profile_id: str | None = None):
        access(session_id, user_id, profile_id)
        run = operation(lambda: runtime.store.get(run_id))
        if run["session_id"] != session_id or run["user_id"] != user_id or run["profile_id"] != profile_id:
            raise HTTPException(403, "HARNESS_ACCESS_DENIED")
        return runtime.public(run)

    @router.post("/sessions/{session_id}/input-requests/{request_id}/answers", status_code=202)
    def answer(session_id: str, request_id: str, submission: AnswerSubmission):
        access(session_id, submission.user_id, submission.profile_id)
        run = operation(lambda: runtime.store.get(runtime.store.request_run(request_id)))
        if run["session_id"] != session_id:
            raise HTTPException(403, "HARNESS_ACCESS_DENIED")
        if runtime.moderation and not submission.skipped:
            runtime.moderation.check(json.dumps(submission.answers, ensure_ascii=False) + (submission.text or ""), stage="input", session_id=session_id)
        return operation(lambda: runtime.answer(request_id, submission))

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
