from __future__ import annotations

import json
from io import BytesIO
from typing import Any, Literal
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.fact_service import FactService, serialize_known_facts
from hailiang_skills.core.fact_prompt_builder import build_missing_fact_form_block
from hailiang_skills.core.skill_display import build_skill_display
from hailiang_skills.core.skill_lifecycle import (
    attach_finalization_metadata,
    build_skill_invitation,
    build_finalized_payload,
    build_route_suggestions_exposed_event,
)
from hailiang_skills.storage.event_store import read_events
from hailiang_skills.core.path_action_builder import (
    build_citations_block,
    build_path_actions_block,
)
from hailiang_skills.core.session_logging import get_session_log_dir
from hailiang_skills.core.session_logging import append_session_events, delete_session_logs, write_session_snapshot
from hailiang_skills.core.logging import make_event, utc_now_iso
from hailiang_skills.core.skill_display import build_skill_catalog
from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID
from hailiang_skills.core.streaming_runner import _diff_facts
from hailiang_skills.core.streaming_runner import _append_message_blocks_to_latest_assistant
from hailiang_skills.core.conversation_state import get_conversation_state, record_security_result
from hailiang_skills.core.sse_protocol import presentation_from_message
from hailiang_skills.core.message_interactions import ACTIVE, SUBMITTED, ensure_message_interactions, update_interaction
from hailiang_skills.core.telemetry import span
from hailiang_skills.storage.repositories.session_index_repo import (
    FileBackedSessionIndexRepository,
)
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository
from hailiang_skills.security.models import ModerationBlockedError


def build_session_logs_archive(session_id: str) -> bytes:
    log_dir = get_session_log_dir(session_id)
    snapshot_path = log_dir / "snapshot.json"
    events_path = log_dir / "events.jsonl"
    event_source = "none"
    try:
        persisted_events = read_events(session_id)
    except Exception:
        # A transient database outage must not make diagnostic log download
        # unavailable; the local file is the development fallback.
        persisted_events = None
        event_source = "file_fallback"

    archive = BytesIO()
    with ZipFile(archive, mode="w", compression=ZIP_DEFLATED) as zip_file:
        if snapshot_path.is_file():
            zip_file.write(snapshot_path, arcname="snapshot.json")
        else:
            zip_file.writestr("snapshot.json", "{}\n")
        if persisted_events is not None:
            # PostgreSQL is the source of truth when configured.  Keep the
            # archive shape identical to the file fallback so existing tools
            # can consume either backend.
            content = "".join(
                json.dumps(event, ensure_ascii=False) + "\n" for event in persisted_events
            )
            zip_file.writestr("events.jsonl", content)
            zip_file.writestr("event_source.txt", "postgres\n")
        elif events_path.is_file():
            zip_file.write(events_path, arcname="events.jsonl")
            zip_file.writestr("event_source.txt", f"{event_source or 'file'}\n")
        else:
            zip_file.writestr("events.jsonl", "")
            zip_file.writestr("event_source.txt", "none\n")
    archive.seek(0)
    return archive.getvalue()


def _latest_assistant_message_id(context: SessionContext) -> str | None:
    for message in reversed(context.messages):
        if message.get("role") == "assistant":
            return str(message.get("message_id") or "") or None
    return None


def _recent_summary(payloads: list[dict[str, Any]] | None) -> str | None:
    """Return summary metadata only; wording is rendered by the frontend."""
    for payload in payloads or []:
        for message in reversed(payload.get("messages", [])):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            compression = message.get("context_compression")
            if not isinstance(compression, dict):
                compression = metadata.get("context_compression")
            summary = ""
            if isinstance(compression, dict):
                summary = str(compression.get("conversation_summary") or "").strip()
            summary = summary or str(message.get("conclusion_summary") or metadata.get("conclusion_summary") or "").strip()
            if summary:
                return summary[:240]
    return None


class CreateSessionResponse(BaseModel):
    status: Literal["created", "resumed"]
    session_id: str
    user_id: str
    user_display_name: str = ""
    profile_id: str
    title: str | None = None
    # Kept as a nullable compatibility field. Opening copy is now owned by
    # the frontend and is never generated or persisted by this service.
    opening_message: None = None
    recent_session_summary: str | None = None
    message_id: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    profile_school_facts: list[dict[str, str]] = Field(default_factory=list)
    profile_facts: dict[str, Any] = Field(default_factory=dict)
    shared_facts: dict[str, Any] = Field(default_factory=dict)
    session_facts: dict[str, Any] = Field(default_factory=dict)
    effective_facts: dict[str, Any] = Field(default_factory=dict)
    interaction_state: dict[str, Any] = Field(default_factory=dict)
    skill_states: dict[str, Any] = Field(default_factory=dict)
    conversation_state: dict[str, Any] = Field(default_factory=dict)


class ProfileSchoolFact(BaseModel):
    school_year: str = Field(min_length=1)
    grade: str = Field(min_length=1)


class CreateSessionRequest(BaseModel):
    session_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    parent_name: str | None = None  # legacy compatibility; not used for opening copy
    profile_school_facts: list[ProfileSchoolFact] = Field(default_factory=list)


class UpdateSessionRequest(BaseModel):
    title: str = Field(min_length=1)


class MessageFeedbackRequest(BaseModel):
    feedback: Literal["like", "dislike", None]


class MessageInteractionUpdateRequest(BaseModel):
    status: Literal["submitted"]
    submitted_fact_keys: list[str] = Field(default_factory=list)


class MessageRequest(BaseModel):
    content: str
    user_id: str | None = None
    enable_thinking: bool = False
    return_reasoning: bool = False
    requested_target_skill_id: str | None = None
    handoff_context: dict | None = None


def build_chat_router(
    repository: InMemorySessionRepository,
    orchestrator,
    fact_service: FactService,
    user_metadata_repository=None,
) -> APIRouter:
    router = APIRouter()
    session_index_repo = FileBackedSessionIndexRepository()

    def session_context(session_id: str):
        try:
            context = repository.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        return context

    def profile_school_facts(context: SessionContext) -> list[dict[str, str]]:
        value = context.profile_facts.get_value("profile_school_facts", [])
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def public_messages(context: SessionContext) -> list[dict[str, Any]]:
        """Expose one v2 presentation shape for both new and legacy history."""
        latest_assistant_id = next(
            (
                str(message.get("message_id") or "")
                for message in reversed(context.messages)
                if message.get("role") == "assistant"
            ),
            "",
        )
        items: list[dict[str, Any]] = []
        for message in context.messages:
            copy = dict(message)
            if copy.get("role") == "assistant":
                copy["presentation"] = presentation_from_message(
                    copy,
                    latest=bool(latest_assistant_id and copy.get("message_id") == latest_assistant_id),
                )
            items.append(copy)
        return items

    def replace_profile_school_facts(user_id: str, profile_id: str, items: list[ProfileSchoolFact]) -> None:
        school_facts = sorted([item.model_dump() for item in items], key=lambda item: item["school_year"])
        profile_facts = fact_service.get_profile_facts(user_id, profile_id)
        profile_facts.set_fact(
            "profile_school_facts", school_facts, source_skill="project_backend",
            source_type="project_backend", source_label="profile_school_facts", scope="profile",
        )
        if school_facts:
            profile_facts.set_fact(
                "grade", school_facts[-1]["grade"], source_skill="project_backend",
                source_type="project_backend", source_label="profile_school_facts", scope="profile",
            )
        else:
            profile_facts.reset_fact("grade")
        fact_service.profile_repo.save_profile_facts(user_id, profile_id, profile_facts)

    def session_response(
        context: SessionContext,
        *,
        status: Literal["created", "resumed"],
        recent_session_summary: str | None = None,
    ) -> CreateSessionResponse:
        fact_service.hydrate_context(context)
        user_metadata = user_metadata_repository.get(context.user_id) if user_metadata_repository else {}
        return CreateSessionResponse(
            status=status,
            session_id=context.session_id,
            user_id=context.user_id,
            user_display_name=str(user_metadata.get("display_name") or ""),
            profile_id=str(context.profile_id or ""),
            title=context.title,
            recent_session_summary=recent_session_summary,
            message_id=_latest_assistant_message_id(context),
            messages=public_messages(context),
            profile_school_facts=profile_school_facts(context),
            profile_facts=serialize_known_facts(context.profile_facts),
            shared_facts=serialize_known_facts(context.shared_facts),
            session_facts=serialize_known_facts(context.session_facts),
            effective_facts=serialize_known_facts(context.known_facts),
            interaction_state=context.interaction_state,
            skill_states=context.skill_states,
            conversation_state=get_conversation_state(context),
        )

    @router.get("/skills")
    def list_runtime_skills(grade: str = Query(default="")) -> dict:
        return {
            "skills": build_skill_catalog(
                getattr(orchestrator, "runtime_registry", None),
                include_fallback=False,
                grade=grade,
            )
        }

    @router.get("/users/{user_id}/profiles/{profile_id}/sessions")
    def list_profile_sessions(user_id: str, profile_id: str) -> dict:
        try:
            fact_service.profile_repo.get_profile(user_id, profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=403, detail="profile does not belong to current user") from exc
        if hasattr(repository, "list_by_profile"):
            contexts = repository.list_by_profile(user_id, profile_id)
            sessions = [
                {
                    "session_id": context.session_id,
                    "user_id": context.user_id,
                    "profile_id": context.profile_id,
                    "profile_name": context.profile_name,
                    "title": context.title,
                    "message_count": len(context.messages),
                    "created_at": (context.messages[0].get("created_at") if context.messages else None),
                    "updated_at": (context.messages[-1].get("created_at") if context.messages else None),
                    "active_skill": context.interaction_state.get("active_skill"),
                }
                for context in contexts
            ]
            return {"sessions": sessions}
        return {
            "sessions": session_index_repo.list_sessions(user_id=user_id, profile_id=profile_id)
        }

    @router.post("/sessions/{session_id}/messages")
    def post_message(session_id: str, request: MessageRequest) -> dict:
        try:
            context = repository.get(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc

        if request.user_id and request.user_id != context.user_id:
            raise HTTPException(status_code=409, detail="request user_id does not match session")
        if context.profile_id:
            try:
                profile = fact_service.profile_repo.get_profile(context.user_id, context.profile_id)
                context.profile_name = str(profile.get("name") or context.profile_name or "")
            except KeyError:
                pass
        fact_service.hydrate_context(context)
        before_facts = serialize_known_facts(context.known_facts)
        context.session_meta["enable_thinking"] = request.enable_thinking
        context.session_meta["return_reasoning"] = request.return_reasoning
        if request.requested_target_skill_id:
            context.session_meta["requested_target_skill_id"] = request.requested_target_skill_id
            context.session_meta["handoff_context"] = request.handoff_context or {}
        try:
            result = orchestrator.handle_message(request.content, context)
        except ModerationBlockedError as exc:
            security = record_security_result(context, stage=exc.stage, result=exc.result, case_id=exc.case_id)
            repository.save(context)
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "CONTENT_BLOCKED",
                    "message": "该内容检测到非合规内容，请重新输入",
                    "stage": exc.stage,
                    "case_id": exc.case_id,
                    **exc.result.to_public_dict(),
                    "security": security,
                },
                ) from exc
        invitation = build_skill_invitation(
            context,
            assistant_message=result.assistant_message,
            runtime_registry=getattr(orchestrator, "runtime_registry", None),
        )
        if invitation:
            result.assistant_message += invitation
            for message in reversed(context.messages):
                if message.get("role") == "assistant":
                    message["content"] = result.assistant_message
                    break
        moderation_service = getattr(orchestrator, "moderation_service", None)
        if moderation_service is not None:
            try:
                moderation_result = moderation_service.check(
                    result.assistant_message,
                    stage="output",
                    trace_id=str(context.session_meta.get("trace_id") or ""),
                    session_id=str(context.session_id),
                    turn_id=str(context.session_meta.get("active_turn_id") or ""),
                )
                record_security_result(context, stage="output", result=moderation_result)
            except ModerationBlockedError as exc:
                context.messages = [
                    message
                    for message in context.messages
                    if not (
                        message.get("role") == "assistant"
                        and message.get("metadata", {}).get("turn_id") == context.session_meta.get("active_turn_id")
                    )
                ]
                security = record_security_result(context, stage=exc.stage, result=exc.result, case_id=exc.case_id)
                repository.save(context)
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "CONTENT_BLOCKED",
                        "message": "该内容检测到非合规内容，当前对话中断，如果需要请重新输入",
                        "stage": exc.stage,
                        "case_id": exc.case_id,
                        **exc.result.to_public_dict(),
                        "security": security,
                    },
                ) from exc
        fact_service.persist_context(context)
        skill_display = build_skill_display(
            context,
            runtime_registry=getattr(orchestrator, "runtime_registry", None),
        )
        active_skill = skill_display["skill_id"]
        after_facts = serialize_known_facts(context.known_facts)
        fact_changes = _diff_facts(before_facts, after_facts)
        finalized_payload, finalized_events = build_finalized_payload(
            context,
            assistant_message=result.assistant_message,
            facts_delta=fact_changes,
            runtime_registry=getattr(orchestrator, "runtime_registry", None),
            route_suggestion_client=getattr(
                orchestrator,
                "route_suggestion_client",
                getattr(orchestrator, "runtime_client", None),
            ),
            monitor_route_suggestions_every_turn=bool(
                getattr(orchestrator, "route_suggestion_monitor_every_turn", False)
            ),
        )
        attach_finalization_metadata(context, finalized_payload)
        exposure_event = build_route_suggestions_exposed_event(
            context,
            finalized_payload,
            finalized_events,
            run_id=str((context.session_meta or {}).get("active_stream_generation") or ""),
        )
        if exposure_event:
            finalized_events.append(exposure_event)
        if finalized_events and hasattr(orchestrator, "_record_events"):
            orchestrator._record_events(context, finalized_events)
        message_blocks = [
            block
            for block in [
                build_missing_fact_form_block(
                    context.skill_states.get("planner", {}).get("missing_facts", []) or []
                ),
                build_path_actions_block(result.suggested_paths or []),
                build_citations_block(context, active_skill=active_skill),
            ]
            if block
        ]
        _append_message_blocks_to_latest_assistant(context, message_blocks)
        repository.save(context)
        return {
            "message_id": _latest_assistant_message_id(context),
            "assistant_message": result.assistant_message,
            "skill_intro": context.session_meta.get("skill_intro"),
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
            "reasoning": context.skill_states.get(CAREER_PLAN_SKILL_ID, {}).get("last_reasoning") or "",
            "asset_support": context.skill_states.get(active_skill or "", {}).get("asset_support", {}),
            "session_log_dir": str(get_session_log_dir(context.session_id)),
            "candidate_paths_brief": context.candidate_paths[:5],
            "suggested_paths": result.suggested_paths,
            "facts_updated": list(result.updated_facts.keys()),
            "risk_alerts": result.risk_alerts,
            "conversation_state": get_conversation_state(context),
            "user_facts": serialize_known_facts(context.user_facts),
            "shared_facts": serialize_known_facts(context.shared_facts),
            "profile_facts": serialize_known_facts(context.profile_facts),
            "session_facts": serialize_known_facts(context.session_facts),
            "effective_facts": serialize_known_facts(context.known_facts),
            "profile_id": context.profile_id,
            "profile_name": context.profile_name,
            "router_state": context.skill_states.get("router", {}),
            "facts_extractor_state": context.skill_states.get("facts_extractor", {}),
            "planner_state": context.skill_states.get("planner", {}),
            "career_plan_state": context.skill_states.get(CAREER_PLAN_SKILL_ID, {}),
            "main_planner_state": context.skill_states.get(CAREER_PLAN_SKILL_ID, {}),
            "admission_state": context.skill_states.get("admission", {}),
            "school_intro_state": context.skill_states.get("school_intro", {}),
            "ranking_snapshot": context.skill_states.get("convergence", {}).get(
                "ranking_snapshot", {}
            ),
        }

    @router.get("/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        context = session_context(session_id)

        fact_service.hydrate_context(context)
        return {
            "session_id": context.session_id,
            "user_id": context.user_id,
            "user_display_name": str((user_metadata_repository.get(context.user_id) if user_metadata_repository else {}).get("display_name") or ""),
            "profile_id": context.profile_id,
            "profile_name": context.profile_name,
            "title": context.title,
            "session_log_dir": str(get_session_log_dir(context.session_id)),
            "facts": {key: record.model_dump() for key, record in context.known_facts.facts.items()},
            "user_facts": serialize_known_facts(context.user_facts),
            "shared_facts": serialize_known_facts(context.shared_facts),
            "profile_facts": serialize_known_facts(context.profile_facts),
            "session_facts": serialize_known_facts(context.session_facts),
            "effective_facts": serialize_known_facts(context.known_facts),
            "candidate_paths": context.candidate_paths,
            "message_count": len(context.messages),
            "conversation_state": get_conversation_state(context),
            "profile_school_facts": profile_school_facts(context),
            "skill_states": context.skill_states,
            "skill_display": build_skill_display(
                context,
                runtime_registry=getattr(orchestrator, "runtime_registry", None),
            ),
        }

    @router.patch("/sessions/{session_id}/messages/{message_id}/feedback")
    def update_message_feedback(
        session_id: str,
        message_id: str,
        request: MessageFeedbackRequest,
    ) -> dict:
        context = session_context(session_id)
        message = next(
            (
                item
                for item in context.messages
                if item.get("message_id") == message_id and item.get("role") == "assistant"
            ),
            None,
        )
        if message is None:
            raise HTTPException(status_code=404, detail="assistant message not found")
        updated_at = utc_now_iso()
        message["feedback"] = request.feedback
        message["feedback_updated_at"] = updated_at
        metadata = message.setdefault("metadata", {})
        metadata["feedback"] = request.feedback
        metadata["feedback_updated_at"] = updated_at
        event = make_event(
            "assistant_feedback_updated",
            {
                "message_id": message_id,
                "feedback": request.feedback,
                "updated_at": updated_at,
            },
        )
        context.event_trace.append(event)
        append_session_events(session_id, [event])
        repository.save(context)
        return {
            "session_id": session_id,
            "message_id": message_id,
            "feedback": request.feedback,
            "feedback_updated_at": updated_at,
        }

    @router.patch("/sessions/{session_id}/messages/{message_id}/interactions/{interaction_id}")
    def update_message_interaction(
        session_id: str,
        message_id: str,
        interaction_id: str,
        request: MessageInteractionUpdateRequest,
    ) -> dict:
        context = session_context(session_id)
        message = next(
            (
                item
                for item in context.messages
                if item.get("message_id") == message_id and item.get("role") == "assistant"
            ),
            None,
        )
        if message is None:
            raise HTTPException(status_code=404, detail="assistant message not found")
        if not interaction_id.startswith("fact_form:"):
            raise HTTPException(status_code=422, detail="only fact_form interactions can be updated")
        states = ensure_message_interactions(message)
        state = states.get(interaction_id)
        if state is None:
            raise HTTPException(status_code=404, detail="message interaction not found")
        if state.get("status") not in {ACTIVE, SUBMITTED}:
            raise HTTPException(status_code=409, detail="message interaction is no longer active")
        form_id = interaction_id.split(":", 1)[1]
        blocks = message.get("blocks") or message.get("metadata", {}).get("blocks") or []
        fields: set[str] = set()
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "fact_form":
                continue
            payload = block.get("payload") if isinstance(block.get("payload"), dict) else {}
            if str(payload.get("form_id") or "") == form_id:
                fields = {
                    str(field.get("fact_key"))
                    for field in payload.get("fields", [])
                    if isinstance(field, dict) and field.get("fact_key")
                }
                break
        submitted_keys = list(dict.fromkeys(str(key) for key in request.submitted_fact_keys if str(key)))
        if not fields or any(key not in fields for key in submitted_keys):
            raise HTTPException(status_code=422, detail="submitted fact keys do not match the form")
        updated_state = update_interaction(
            message,
            interaction_id,
            status=SUBMITTED,
            submitted_fact_keys=submitted_keys,
        )
        event = make_event(
            "assistant_message_interaction_updated",
            {
                "message_id": message_id,
                "interaction_id": interaction_id,
                "status": SUBMITTED,
                "submitted_fact_keys": submitted_keys,
            },
        )
        context.event_trace.append(event)
        append_session_events(session_id, [event])
        repository.save(context)
        return {
            "session_id": session_id,
            "message_id": message_id,
            "interaction_id": interaction_id,
            "state": updated_state,
        }

    @router.get("/sessions/{session_id}/context")
    def get_session_context(session_id: str) -> dict:
        context = session_context(session_id)
        fact_service.hydrate_context(context)
        return {
            "session_id": context.session_id,
            "user_id": context.user_id,
            "user_display_name": str((user_metadata_repository.get(context.user_id) if user_metadata_repository else {}).get("display_name") or ""),
            "profile_id": context.profile_id,
            "profile_name": context.profile_name,
            "title": context.title,
            "messages": public_messages(context),
            "user_facts": serialize_known_facts(context.user_facts),
            "shared_facts": serialize_known_facts(context.shared_facts),
            "profile_facts": serialize_known_facts(context.profile_facts),
            "session_facts": serialize_known_facts(context.session_facts),
            "effective_facts": serialize_known_facts(context.known_facts),
            "skill_states": context.skill_states,
            "interaction_state": context.interaction_state,
            "candidate_paths": context.candidate_paths,
            "conversation_state": get_conversation_state(context),
            "profile_school_facts": profile_school_facts(context),
            "event_count": len(context.event_trace),
        }

    @router.get("/sessions/{session_id}/events")
    def get_events(session_id: str) -> dict:
        context = session_context(session_id)
        return {"events": context.event_trace}

    @router.get("/sessions/{session_id}/logs/download")
    def download_session_logs(session_id: str, request: Request) -> Response:
        context = session_context(session_id)

        client_host = (request.client.host if request.client else "").split("%")[0]
        local_debug_request = client_host in {"127.0.0.1", "::1", "localhost"}
        if hasattr(repository, "list_by_profile") and not local_debug_request:
            # Production raw logs can include regulated conversation content;
            # only a local-debug browser may export its development recording.
            raise HTTPException(status_code=403, detail="production log export is disabled")

        write_session_snapshot(context)
        filename = f"{session_id}-logs.zip"
        return Response(
            content=build_session_logs_archive(session_id),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.patch("/sessions/{session_id}")
    def update_session(session_id: str, request: UpdateSessionRequest) -> dict:
        context = session_context(session_id)
        context.title = request.title.strip()
        repository.save(context)
        return {
            "session_id": context.session_id,
            "title": context.title,
        }

    @router.delete("/sessions/{session_id}")
    def delete_session(session_id: str, user_id: str = Query(min_length=1), profile_id: str = Query(min_length=1)) -> dict:
        try:
            repository.delete(session_id, user_id=user_id, profile_id=profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="session does not belong to current user and profile") from exc
        delete_session_logs(session_id)
        return {"session_id": session_id, "deleted": True}

    return router
