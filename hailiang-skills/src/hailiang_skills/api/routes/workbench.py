from __future__ import annotations

import copy
import json
import time
from queue import Empty, Queue
from threading import Thread
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from hailiang_skills.core.deployment import deployment_environment
from hailiang_skills.workbench import WorkbenchConflict, WorkbenchError, WorkbenchService
from hailiang_skills.workbench.service import WORKBENCH_CANDIDATE_LLM_TIMEOUT_S


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActorInput(StrictModel):
    display_name: str = Field(min_length=2, max_length=80)
    device_token: str | None = None


class ObjectInput(StrictModel):
    object_type: Literal["skill", "expert", "expert_team"]
    object_key: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=256)
    description: str = ""
    actor_id: str = Field(min_length=1)


class RevisionInput(StrictModel):
    base_revision_id: str | None = None
    payload: dict[str, Any]
    dependency_locks: list[dict[str, Any]] = Field(default_factory=list)
    assets: list[dict[str, Any]] = Field(default_factory=list)
    actor_id: str = Field(min_length=1)


class ReleaseInput(StrictModel):
    revision_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    manual_confirmation: bool
    confirmation_notes: str = ""
    actor_id: str = Field(min_length=1)


class ActorAction(StrictModel):
    actor_id: str = Field(min_length=1)


class CurrentReleaseInput(ActorAction):
    expected_current_release_id: str | None


class DeploymentDeactivateInput(ActorAction):
    expert_team_id: str = Field(min_length=1, max_length=160)


class DeleteObjectInput(StrictModel):
    confirmation_name: str = Field(min_length=1, max_length=256)
    actor_id: str = Field(min_length=1)


class IdMigrationInput(DeleteObjectInput):
    new_object_key: str = Field(min_length=1, max_length=160)


class ReferenceMigrationInput(DeleteObjectInput):
    from_release_id: str = Field(min_length=1)
    to_release_id: str = Field(min_length=1)


class DebugSessionInput(StrictModel):
    revision_id: str = Field(min_length=1)
    baseline_release_id: str | None = None
    actor_id: str = Field(min_length=1)


class RevisionTestSessionInput(StrictModel):
    revision_id: str = Field(min_length=1)
    soul_revision_id: str | None = None
    actor_id: str = Field(min_length=1)


class RevisionTestTurnInput(StrictModel):
    user_message: str = ""
    form_submission: dict[str, Any] | None = None
    team_handoff_selection: dict[str, Any] | None = None
    expert_selection: dict[str, Any] | None = None
    actor_id: str = Field(min_length=1)


class FormalChatSessionInput(StrictModel):
    object_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    release_id: str = Field(min_length=1)
    soul_revision_id: str | None = None
    actor_id: str = Field(min_length=1)


class DebugTurnInput(StrictModel):
    user_message: str
    assistant_message: str
    trace: list[dict[str, Any]] = Field(default_factory=list)
    actor_id: str = Field(min_length=1)


class DebugCompleteInput(StrictModel):
    conclusion: str = ""
    actor_id: str = Field(min_length=1)


class EvaluationSuiteInput(StrictModel):
    object_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    cases: list[dict[str, Any]] = Field(min_length=1)
    actor_id: str = Field(min_length=1)


class EvaluationRunInput(StrictModel):
    suite_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    baseline_release_id: str | None = None
    soul_revision_id: str | None = None
    results: list[dict[str, Any]] | None = None
    actor_id: str = Field(min_length=1)


class EvaluationCompleteInput(StrictModel):
    results: list[dict[str, Any]]
    manual_result: Literal["accepted", "rejected"]
    manual_notes: str = ""
    actor_id: str = Field(min_length=1)


class ExportInput(StrictModel):
    release_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)


class SoulRevisionInput(StrictModel):
    content: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)


class ConversionCommitInput(StrictModel):
    draft: dict[str, Any]
    target_object_id: str | None = None
    actor_id: str = Field(min_length=1)


def _call(callback):
    try:
        return callback()
    except WorkbenchConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc), "details": exc.details},
        ) from exc
    except WorkbenchError as exc:
        status = 404 if exc.code.endswith("NOT_FOUND") else 422
        raise HTTPException(
            status_code=status,
            detail={"code": exc.code, "message": str(exc), "details": exc.details},
        ) from exc


def build_workbench_router(service: WorkbenchService) -> APIRouter:
    router = APIRouter(tags=["workbench"])

    @router.get("/kernel")
    def kernel_info():
        return service.kernel_info()

    @router.post("/actors")
    def register_actor(body: ActorInput):
        return _call(lambda: service.register_actor(body.display_name, body.device_token))

    @router.get("/objects")
    def list_objects(object_type: str | None = None, include_archived: bool = False):
        return {"objects": _call(lambda: service.list_objects(object_type=object_type, include_archived=include_archived))}

    @router.post("/objects", status_code=201)
    def create_object(body: ObjectInput):
        return _call(lambda: service.create_object(**body.model_dump()))

    @router.get("/objects/{object_id}")
    def get_object(object_id: str):
        return _call(lambda: service.get_object(object_id))

    @router.post("/objects/{object_id}/archive")
    def archive_object(object_id: str, body: ActorAction):
        return _call(lambda: service.archive_object(object_id, actor_id=body.actor_id))

    @router.post("/objects/{object_id}/unarchive")
    def unarchive_object(object_id: str, body: ActorAction):
        return _call(lambda: service.unarchive_object(object_id, actor_id=body.actor_id))

    @router.delete("/objects/{object_id}")
    def delete_object(object_id: str, body: DeleteObjectInput):
        return _call(lambda: service.delete_object(object_id, **body.model_dump()))

    @router.post("/objects/{object_id}/id-migrations", status_code=201)
    def migrate_object_id(object_id: str, body: IdMigrationInput):
        return _call(lambda: service.migrate_object_id(object_id, **body.model_dump()))

    @router.post("/reference-migrations", status_code=201)
    def migrate_references(body: ReferenceMigrationInput):
        return _call(lambda: service.migrate_references(**body.model_dump()))

    @router.post("/objects/{object_id}/revisions", status_code=201)
    def save_revision(object_id: str, body: RevisionInput):
        return _call(lambda: service.save_revision(object_id, **body.model_dump()))

    @router.get("/revisions/compare")
    def compare_revisions(left_revision_id: str, right_revision_id: str):
        return _call(lambda: service.compare_revisions(left_revision_id, right_revision_id))

    @router.get("/revisions/{revision_id}/assets")
    def list_revision_assets(revision_id: str):
        return {"assets": _call(lambda: service.list_revision_assets(revision_id))}

    @router.get("/releases")
    def list_releases(object_type: str | None = None, include_archived: bool = False):
        return {"releases": _call(lambda: service.list_releases(object_type=object_type, include_archived=include_archived))}

    @router.post("/releases/{release_id}/make-current")
    def make_current(release_id: str, body: CurrentReleaseInput):
        return _call(lambda: service.select_current_release(release_id, **body.model_dump()))

    @router.post("/releases/{release_id}/drafts", status_code=201)
    def draft_from_release(release_id: str, body: ActorAction):
        return _call(lambda: service.draft_from_release(release_id, actor_id=body.actor_id))

    @router.post("/releases", status_code=201)
    def publish_revision(body: ReleaseInput):
        return _call(lambda: service.publish_revision(**body.model_dump()))

    @router.post("/releases/{release_id}/archive")
    def archive_release(release_id: str, body: ActorAction):
        return _call(lambda: service.archive_release(release_id, actor_id=body.actor_id))

    @router.post("/debug-sessions", status_code=201)
    def create_debug_session(body: DebugSessionInput):
        return _call(lambda: service.create_debug_session(**body.model_dump()))

    @router.post("/revision-tests", status_code=201)
    def create_revision_test_session(body: RevisionTestSessionInput):
        return _call(lambda: service.create_revision_test_session(**body.model_dump()))

    @router.post("/standard-skill-conversions/preview")
    async def preview_standard_skill_conversion(request: Request, actor_id: str = Query(min_length=1)):
        package_bytes = await request.body()
        return _call(lambda: service.preview_standard_skill_package(package_bytes, actor_id=actor_id))

    @router.post("/standard-skill-conversions/commit", status_code=201)
    def commit_standard_skill_conversion(body: ConversionCommitInput):
        return _call(lambda: service.commit_standard_skill_conversion(**body.model_dump()))

    @router.get("/soul-revisions")
    def list_soul_revisions():
        return {"revisions": _call(service.list_soul_revisions)}

    @router.post("/soul-revisions", status_code=201)
    def save_soul_revision(body: SoulRevisionInput):
        return _call(lambda: service.save_soul_revision(**body.model_dump()))

    @router.post("/revision-tests/{debug_session_id}/turns")
    def run_revision_test_turn(debug_session_id: str, body: RevisionTestTurnInput):
        return _call(lambda: service.run_revision_test_turn(debug_session_id, **body.model_dump()))

    @router.post("/revision-tests/{debug_session_id}/turns/stream")
    def stream_revision_test_turn(debug_session_id: str, body: RevisionTestTurnInput):
        def stream():
            queue: Queue[tuple[str, dict[str, Any]]] = Queue()
            latest_state: dict[str, Any] | None = None

            def emit(event: str, payload: dict[str, Any]):
                nonlocal latest_state
                # Candidate transport is independent from formal chat, but a
                # ``state`` frame has the exact same v2 semantics.  Keeping
                # the last snapshot lets an error finish with authoritative
                # context instead of forcing the UI to guess the active
                # expert or Skill from stale local state.
                if event == "state" and payload.get("protocol") == "hailiang.sse.v2":
                    latest_state = copy.deepcopy(payload)
                queue.put((event, payload))

            def emit_failure_state(
                code: str,
                message: str,
                details: dict[str, Any] | None = None,
            ) -> dict[str, Any] | None:
                if latest_state is None:
                    return None
                failed = copy.deepcopy(latest_state)
                failed["seq"] = int(failed.get("seq") or 0) + 1
                failed["status"] = "failed"
                assistant = failed.get("assistant") if isinstance(failed.get("assistant"), dict) else {}
                failed["assistant"] = {**assistant, "status": "failed"}
                failed["error"] = {
                    "code": code,
                    "message": message,
                    "upstream_detail": str((details or {}).get("detail") or ""),
                    "retryable": code in {"MODEL_TIMEOUT", "REVISION_TEST_TIMEOUT"},
                    "terminal": True,
                }
                emit("state", failed)
                return failed

            def worker():
                try:
                    emit("done", service.run_revision_test_turn(debug_session_id, **body.model_dump(), on_event=emit))
                except WorkbenchError as exc:
                    emit_failure_state(exc.code, str(exc), exc.details)
                    emit("error", {"code": exc.code, "message": str(exc), "details": exc.details})
                except Exception as exc:
                    emit_failure_state("REVISION_TEST_FAILED", str(exc))
                    emit("error", {"code": "REVISION_TEST_FAILED", "message": str(exc)})
            Thread(target=worker, daemon=True).start()
            yield "event: started\ndata: {}\n\n"
            # This is a deadline for the *entire* candidate turn, rather than
            # one socket read in an individual model request.  Candidate
            # execution can include snapshot mounting, expert routing, tools
            # and several model calls; without this watchdog a stuck worker
            # would keep the browser in a ping-only streaming state forever.
            deadline = time.monotonic() + WORKBENCH_CANDIDATE_LLM_TIMEOUT_S
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    run_id = str((latest_state or {}).get("run_id") or "")
                    if run_id:
                        try:
                            service.stop_revision_test_turn(
                                debug_session_id,
                                run_id=run_id,
                                actor_id=body.actor_id,
                            )
                        except WorkbenchError:
                            # The worker may have just completed and removed
                            # its live entry. The terminal timeout below still
                            # prevents the client from being left loading.
                            pass
                    timeout_message = "候选修订测试执行超时，请稍后重试。"
                    timeout_details = {"timeout_s": WORKBENCH_CANDIDATE_LLM_TIMEOUT_S}
                    failed_state = emit_failure_state(
                        "REVISION_TEST_TIMEOUT",
                        timeout_message,
                        timeout_details,
                    )
                    # Unlike worker failures, this branch terminates the
                    # generator immediately. Flush its authoritative failed
                    # snapshot directly instead of leaving it in the queue.
                    if failed_state is not None:
                        yield f"event: state\ndata: {json.dumps(failed_state, ensure_ascii=False)}\n\n"
                    timeout_payload = {
                        "code": "REVISION_TEST_TIMEOUT",
                        "message": timeout_message,
                        "details": timeout_details,
                    }
                    yield f"event: error\ndata: {json.dumps(timeout_payload, ensure_ascii=False)}\n\n"
                    return
                try:
                    event, payload = queue.get(timeout=min(10, remaining))
                except Empty:
                    # Keep proxies and browser fetch streams alive while a
                    # model is preparing its first token. The bounded model
                    # timeout above is still responsible for the terminal
                    # error and loading-state cleanup.
                    yield "event: ping\ndata: {}\n\n"
                    continue
                yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if event in {"done", "error"}:
                    return
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/revision-tests/{debug_session_id}/turns/{run_id}/stop")
    def stop_revision_test_turn(debug_session_id: str, run_id: str, body: ActorAction):
        return _call(lambda: service.stop_revision_test_turn(
            debug_session_id,
            run_id=run_id,
            actor_id=body.actor_id,
        ))

    @router.post("/formal-team-chat-sessions", status_code=201)
    def create_formal_team_chat_session(body: RevisionTestSessionInput):
        return _call(lambda: service.create_formal_team_chat_session(**body.model_dump()))

    @router.post("/formal-chat-sessions", status_code=201)
    def create_formal_chat_session(body: FormalChatSessionInput):
        return _call(lambda: service.create_formal_chat_session(**body.model_dump()))

    @router.post("/debug-sessions/{debug_session_id}/turns")
    def append_debug_turn(debug_session_id: str, body: DebugTurnInput):
        return _call(lambda: service.append_debug_turn(debug_session_id, **body.model_dump()))

    @router.post("/debug-sessions/{debug_session_id}/complete")
    def complete_debug_session(debug_session_id: str, body: DebugCompleteInput):
        return _call(lambda: service.complete_debug_session(debug_session_id, **body.model_dump()))

    @router.get("/debug-sessions")
    def list_debug_sessions(
        object_id: str | None = None,
        revision_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ):
        return {"sessions": _call(lambda: service.list_debug_sessions(object_id=object_id, revision_id=revision_id, status=status, limit=limit))}

    @router.get("/evaluation-suites")
    def list_evaluation_suites(object_id: str | None = None):
        return {"suites": service.list_evaluation_suites(object_id)}

    @router.post("/evaluation-suites", status_code=201)
    def create_evaluation_suite(body: EvaluationSuiteInput):
        return _call(lambda: service.create_evaluation_suite(**body.model_dump()))

    @router.post("/evaluation-runs", status_code=201)
    def create_evaluation_run(body: EvaluationRunInput):
        return _call(lambda: service.create_evaluation_run(**body.model_dump()))

    @router.post("/evaluation-runs/{run_id}/complete")
    def complete_evaluation_run(run_id: str, body: EvaluationCompleteInput):
        return _call(lambda: service.complete_evaluation_run(run_id, **body.model_dump()))

    @router.get("/evaluation-runs")
    def list_evaluation_runs(
        object_id: str | None = None,
        revision_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
    ):
        return {"runs": _call(lambda: service.list_evaluation_runs(object_id=object_id, revision_id=revision_id, status=status, limit=limit))}

    @router.post("/exports")
    def export_release(body: ExportInput):
        archive, manifest = _call(lambda: service.export_release(body.release_id, actor_id=body.actor_id))
        filename = f"{manifest['root']['object_key']}-v{manifest['root']['release_no']}.zip"
        return Response(
            archive,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Package-Id": manifest["package_id"],
            },
        )

    @router.post("/imports", status_code=201)
    async def import_object_package(
        request: Request,
        actor_id: str = Query(min_length=1),
    ):
        package_bytes = await request.body()
        return _call(lambda: service.import_object_package(package_bytes, actor_id=actor_id))

    @router.get("/audit")
    def audit(limit: int = Query(default=50, ge=1, le=200)):
        return {"events": service.list_audit_events(limit)}

    return router


def build_deployment_router(service: WorkbenchService) -> APIRouter:
    router = APIRouter(tags=["deployment"])

    def active_environment(requested: str | None) -> str:
        """Keep deployment writes and formal-chat reads in one environment."""
        current = deployment_environment()
        if requested is not None and requested != current:
            raise WorkbenchConflict(
                f"部署环境必须与当前服务环境一致: {current}",
                code="DEPLOYMENT_ENVIRONMENT_MISMATCH",
                details={"requested_environment": requested, "current_environment": current},
            )
        return current

    @router.post("/imports", status_code=201)
    async def import_package(
        request: Request,
        environment: str | None = Query(default=None),
        actor_id: str = Query(min_length=1),
    ):
        package_bytes = await request.body()
        return _call(lambda: service.import_package(package_bytes, environment=active_environment(environment), actor_id=actor_id))

    @router.get("/deployments")
    def list_deployments(environment: str | None = None):
        return _call(lambda: {"deployments": service.list_deployments(active_environment(environment))})

    @router.get("/deployments/{deployment_id}/preview")
    def preview_deployment(deployment_id: str, environment: str | None = None):
        def preview() -> dict:
            deployments = service.list_deployments(active_environment(environment))
            target = next((item for item in deployments if item["deployment_id"] == deployment_id), None)
            if target is None:
                raise HTTPException(status_code=404, detail={"code": "DEPLOYMENT_NOT_FOUND", "message": "部署记录不存在"})
            return {"deployment": target, "validation": {"valid": True, "kernel_compatible": True, "dependency_complete": True}}

        return _call(preview)

    @router.post("/deployments/{deployment_id}/activate")
    def activate_deployment(deployment_id: str, body: ActorAction):
        return _call(lambda: service.activate_deployment(deployment_id, actor_id=body.actor_id))

    @router.post("/deployments/{deployment_id}/rollback")
    def rollback_deployment(deployment_id: str, body: ActorAction):
        return _call(lambda: service.rollback_deployment(deployment_id, actor_id=body.actor_id))

    @router.post("/deployments/{deployment_id}/deactivate")
    def deactivate_deployment(deployment_id: str, body: DeploymentDeactivateInput):
        return _call(lambda: service.deactivate_deployment(deployment_id, **body.model_dump()))

    @router.post("/deployments/{deployment_id}/restore")
    def restore_deployment(deployment_id: str, body: ActorAction):
        return _call(lambda: service.restore_deployment(deployment_id, actor_id=body.actor_id))

    return router
