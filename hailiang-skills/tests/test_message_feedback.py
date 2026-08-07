from __future__ import annotations

from pathlib import Path

from hailiang_skills.api.routes.chat import MessageFeedbackRequest, build_chat_router
from hailiang_skills.core.context import SessionContext
from hailiang_skills.core import session_logging
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository


class _FactService:
    profile_repo = None


class _Orchestrator:
    runtime_registry = None


def _feedback_endpoint(router):
    return next(
        route.endpoint
        for route in router.routes
        if getattr(route, "path", "") == "/sessions/{session_id}/messages/{message_id}/feedback"
    )


def test_assistant_message_feedback_is_persisted_and_can_be_cleared(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_logging, "SESSION_LOG_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session_logging, "USER_LOG_ROOT", tmp_path / "users")
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="sess_feedback", user_id="user_feedback")
    context.add_message("assistant", "先了解一下孩子的情况")
    repository.create(context)
    message_id = context.messages[0]["message_id"]
    endpoint = _feedback_endpoint(build_chat_router(repository, _Orchestrator(), _FactService()))

    result = endpoint(
        context.session_id,
        message_id,
        MessageFeedbackRequest(feedback="like"),
    )

    assert result["message_id"] == message_id
    assert context.messages[0]["feedback"] == "like"
    assert context.messages[0]["metadata"]["feedback"] == "like"
    assert context.event_trace[-1]["event_type"] == "assistant_feedback_updated"

    endpoint(
        context.session_id,
        message_id,
        MessageFeedbackRequest(feedback=None),
    )
    restored = repository.load_from_snapshot(context.session_id)
    assert restored.messages[0]["feedback"] is None
    assert restored.messages[0]["feedback_updated_at"]


def test_old_messages_receive_stable_ids() -> None:
    context = SessionContext(
        session_id="sess_legacy",
        messages=[{"role": "assistant", "content": "旧消息"}],
    )

    message = context.messages[0]
    assert message["message_id"].startswith("msg_")
    assert message["metadata"]["message_id"] == message["message_id"]
