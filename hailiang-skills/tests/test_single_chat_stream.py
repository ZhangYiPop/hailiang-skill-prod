from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import hailiang_skills.api.routes.chat_stream as chat_stream
from hailiang_skills.api.routes.chat_stream import build_chat_stream_router
from hailiang_skills.api.routes.chat_stream import ChatInput, EnterSkillInput, StopInput, SwitchTeamMemberInput, _parse_input
from hailiang_skills.api.session_lifecycle import ContextData, open_or_resume_session
from hailiang_skills.core import session_logging
from hailiang_skills.core.concurrency import TurnCoordinator
from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.fact_service import FactService as RuntimeFactService
from hailiang_skills.core.sse_protocol import SseEnvelopeBuilder
from hailiang_skills.schemas.facts import KnownFacts
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository


class _ProfileRepo:
    def __init__(self) -> None:
        self.profiles: dict[tuple[str, str], dict] = {}
        self.facts: dict[tuple[str, str], KnownFacts] = {}
        self.raise_integrity_once = False

    def get_profile(self, user_id: str, profile_id: str) -> dict:
        value = self.profiles.get((user_id, profile_id))
        if value is None:
            raise KeyError(profile_id)
        return dict(value)

    def get_profile_by_id(self, profile_id: str) -> dict:
        for (owner_id, candidate_id), value in self.profiles.items():
            if candidate_id == profile_id:
                return dict(value)
        raise KeyError(profile_id)

    def create_profile(self, user_id: str, *, profile_id: str, name: str, **_: object) -> dict:
        if self.raise_integrity_once:
            self.raise_integrity_once = False
            self.profiles[(user_id, profile_id)] = {"profile_id": profile_id, "name": "旧名"}
            raise IntegrityError("insert profile", {}, Exception("duplicate profile"))
        value = {"profile_id": profile_id, "name": name}
        self.profiles[(user_id, profile_id)] = value
        return dict(value)

    def update_profile(self, user_id: str, profile_id: str, *, name: str, **_: object) -> dict:
        value = self.get_profile(user_id, profile_id)
        value["name"] = name
        self.profiles[(user_id, profile_id)] = value
        return dict(value)

    def get_profile_facts(self, user_id: str, profile_id: str) -> KnownFacts:
        key = next((key for key in self.facts if key[1] == profile_id), (user_id, profile_id))
        return self.facts.setdefault(key, KnownFacts())

    def save_profile_facts(self, user_id: str, profile_id: str, facts: KnownFacts) -> KnownFacts:
        key = next((key for key in self.facts if key[1] == profile_id), (user_id, profile_id))
        self.facts[key] = facts
        return facts


class _FactService:
    def __init__(self) -> None:
        self.profile_repo = _ProfileRepo()

    def get_profile_facts(self, user_id: str, profile_id: str) -> KnownFacts:
        return self.profile_repo.get_profile_facts(user_id, profile_id)

    def hydrate_context(self, context: SessionContext) -> SessionContext:
        context.load_effective_facts(
            shared_facts=KnownFacts(),
            profile_facts=deepcopy(self.profile_repo.get_profile_facts(context.user_id, context.profile_id)),
            session_facts=context.session_facts,
        )
        return context

    validate_configured_update = staticmethod(RuntimeFactService.validate_configured_update)

    def persist_context(self, context: SessionContext) -> None:
        self.profile_repo.save_profile_facts(context.user_id, context.profile_id, context.profile_facts)


class _FakeRunner:
    instances: list["_FakeRunner"] = []

    def __init__(self, repository, fact_service, orchestrator, turn_coordinator=None) -> None:
        self.repository = repository
        self.fact_service = fact_service
        self.turn_coordinator = turn_coordinator or TurnCoordinator()
        self.instances.append(self)

    def reserve_turn(self, session_id: str, user_id: str, *, run_id: str | None = None):
        return self.turn_coordinator.acquire(session_id, user_id, run_id=run_id)

    def cancel_run(self, session_id: str, user_id: str, run_id: str) -> bool:
        context = self.repository.get(session_id)
        return context.user_id == user_id and self.turn_coordinator.cancel(session_id, run_id)

    def stream_stop(self, session_id: str, user_id: str, *, run_id: str, **_: object):
        builder = SseEnvelopeBuilder(run_id=run_id, session_id=session_id)
        yield builder.encode("run_cancelled", {"session_id": session_id, "run_id": run_id})

    def stream_message(self, session_id: str, user_id: str, content: str, **kwargs: object):
        lease = kwargs["lease"]
        builder = SseEnvelopeBuilder(run_id=lease.generation, session_id=session_id)
        try:
            for event, data in kwargs.get("initial_events", []):
                yield builder.encode(event, data)
            yield builder.encode("run_started", {"risk_stage": "input"})
            yield builder.encode("run_completed", {"message_id": "msg_fake", "status": "completed"})
        finally:
            context = self.repository.get(session_id)
            context.session_meta.setdefault("run_ledger", {}).setdefault(lease.generation, {})["status"] = "completed"
            self.repository.save(context)
            self.turn_coordinator.release(lease)

    def prepare_skill_transition(self, session_id: str, user_id: str, **kwargs: object):
        target_skill_id = str(kwargs.get("target_skill_id") or "")
        if target_skill_id == "unknown_skill":
            raise ValueError("unknown skill")
        context = self.repository.get(session_id)
        context.interaction_state["active_skill"] = target_skill_id if kwargs.get("action") == "enter" else "general_chat"
        self.repository.save(context)
        return {"transition": {"to_skill_id": target_skill_id}}

    def stream_skill_transition(self, session_id: str, user_id: str, **kwargs: object):
        lease = kwargs["lease"]
        action = kwargs["action"]
        target = kwargs["target_skill_id"]
        to_skill = target if action == "enter" else "general_chat"
        builder = SseEnvelopeBuilder(run_id=lease.generation, session_id=session_id)
        try:
            for event, data in kwargs.get("initial_events", []):
                yield builder.encode(event, data)
            yield builder.encode("run_started", {"risk_stage": "input"})
            yield builder.encode("skill_transition", {"action": action, "to_skill_id": to_skill, "source": kwargs["source"]})
            yield builder.encode("run_completed", {"message_id": "msg_transition", "status": "completed"})
        finally:
            context = self.repository.get(session_id)
            context.session_meta.setdefault("run_ledger", {}).setdefault(lease.generation, {})["status"] = "completed"
            self.repository.save(context)
            self.turn_coordinator.release(lease)


def _state_frames(body: str) -> list[dict]:
    frames: list[dict] = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.splitlines()
        if lines[0] == "event: done":
            continue
        assert lines[0] == "event: state"
        frames.append(__import__("json").loads(lines[1][6:]))
    return frames


def _done_frames(body: str) -> list[dict]:
    frames: list[dict] = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.splitlines()
        if lines[0] == "event: done":
            frames.append(__import__("json").loads(lines[1][6:]))
    return frames


def _context_data() -> ContextData:
    return ContextData(
        student_name="小海",
        school_year="2026-2027",
        grade="高一",
        user_id="u1",
        profile_id="p1",
    )


def test_context_data_requires_only_identity_and_allows_extensions() -> None:
    data = ContextData.model_validate(
        {
            "student_name": "小海",
            "user_id": "u1",
            "profile_id": "p1",
            "guardian_phone": "masked-value",
        }
    )

    assert data.school_year is None
    assert data.grade is None
    assert data.model_extra == {"guardian_phone": "masked-value"}


def test_context_data_allows_unbound_identity_with_only_user_id() -> None:
    data = ContextData.model_validate({"user_id": "u1"})
    assert data.profile_id is None
    assert data.student_name is None


def test_new_session_without_optional_school_context_does_not_seed_school_facts() -> None:
    repository = InMemorySessionRepository()
    facts = _FactService()
    data = ContextData(student_name="小海", user_id="u1", profile_id="p1")

    open_or_resume_session(repository, facts, session_id="sess_1", data=data)

    profile_facts = facts.get_profile_facts("u1", "p1")
    assert profile_facts.get_value("grade") is None
    assert profile_facts.get_value("profile_school_facts") is None


def test_new_session_seeds_grade_when_forwarder_omits_school_year() -> None:
    repository = InMemorySessionRepository()
    facts = _FactService()
    data = ContextData(student_name="小海", user_id="u1", profile_id="p1", grade="高一")

    context, created = open_or_resume_session(
        repository,
        facts,
        session_id=f"sess_grade_only_{uuid4().hex}",
        data=data,
    )

    assert created is True
    assert facts.get_profile_facts("u1", "p1").get_value("grade") == "高一"
    assert context.known_facts.get_value("grade") == "高一"
    assert facts.get_profile_facts("u1", "p1").get_value("profile_school_facts") is None


def test_new_session_seeds_registered_forwarded_facts_from_envelope_and_extra_fields() -> None:
    repository = InMemorySessionRepository()
    facts = _FactService()
    data = ContextData.model_validate(
        {
            "student_name": "小海",
            "user_id": "u1",
            "profile_id": "p1",
            "facts": {"student_province": "浙江", "score_total": 580},
            "subject_group": "物理",
            "unregistered_forwarding_metadata": "must not reach facts",
        }
    )

    context, _ = open_or_resume_session(
        repository,
        facts,
        session_id=f"sess_forwarded_facts_{uuid4().hex}",
        data=data,
    )

    assert context.known_facts.get_value("student_province") == "浙江"
    assert context.known_facts.get_value("score_total") == 580
    assert context.known_facts.get_value("subject_group") == "物理"
    assert context.known_facts.get_value("unregistered_forwarding_metadata") is None


def _api_payload(*, session_id: str = "sess_1", run_id: str = "run_1", input_payload: dict | str | None = None) -> dict:
    if input_payload is None:
        input_payload = {"action": "chat", "profile_id": "p1", "content": "你好", "source": "chat"}
    elif isinstance(input_payload, dict) and input_payload.get("action") != "stop":
        input_payload = {"profile_id": "p1", **input_payload}
    raw_input = input_payload if isinstance(input_payload, str) else __import__("json").dumps(input_payload)
    return {
        "session_id": session_id,
        "run_id": run_id,
        "input": raw_input,
        "context_data": _context_data().model_dump(),
    }


@pytest.fixture()
def api_client(monkeypatch, tmp_path: Path) -> tuple[TestClient, InMemorySessionRepository]:
    monkeypatch.setattr(session_logging, "SESSION_LOG_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session_logging, "USER_LOG_ROOT", tmp_path / "users")
    _FakeRunner.instances.clear()
    monkeypatch.setattr(chat_stream, "StreamingRunner", _FakeRunner)
    repository = InMemorySessionRepository()
    app = FastAPI()
    router = build_chat_stream_router(repository, _FactService(), object())
    app.include_router(router, prefix="/api/v2")
    return TestClient(app), repository


def test_open_or_resume_refreshes_matched_application_projection(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_logging, "SESSION_LOG_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session_logging, "USER_LOG_ROOT", tmp_path / "users")
    repository = InMemorySessionRepository()
    facts = _FactService()

    context, created = open_or_resume_session(repository, facts, session_id="sess_1", data=_context_data())
    assert created is True
    assert context.profile_name == "小海"
    assert facts.get_profile_facts("u1", "p1").get_value("grade") == "高一"

    facts.get_profile_facts("u1", "p1").set_fact("grade", "高二", source_skill="chat", scope="profile")
    resumed, created = open_or_resume_session(repository, facts, session_id="sess_1", data=_context_data())
    assert created is False
    assert resumed.session_id == "sess_1"
    assert facts.get_profile_facts("u1", "p1").get_value("grade") == "高一"


def test_new_session_allows_another_user_for_shared_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_logging, "SESSION_LOG_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session_logging, "USER_LOG_ROOT", tmp_path / "users")
    repository = InMemorySessionRepository()
    facts = _FactService()

    first, created = open_or_resume_session(repository, facts, session_id="sess_parent_a", data=_context_data())
    second, second_created = open_or_resume_session(
        repository,
        facts,
        session_id="sess_parent_b",
        data=_context_data().model_copy(update={"user_id": "u2"}),
    )

    assert created is True
    assert second_created is True
    assert first.profile_id == second.profile_id == "p1"
    assert first.user_id == "u1"
    assert second.user_id == "u2"


def test_open_or_resume_rejects_session_owner_conflict(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_logging, "SESSION_LOG_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session_logging, "USER_LOG_ROOT", tmp_path / "users")
    repository = InMemorySessionRepository()
    facts = _FactService()
    open_or_resume_session(repository, facts, session_id="sess_1", data=_context_data())
    with pytest.raises(HTTPException, match="SESSION_ID_CONFLICT"):
        open_or_resume_session(
            repository,
            facts,
            session_id="sess_1",
            data=_context_data().model_copy(update={"user_id": "u2"}),
        )


def test_open_or_resume_tolerates_concurrent_profile_insert(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(session_logging, "SESSION_LOG_ROOT", tmp_path / "sessions")
    monkeypatch.setattr(session_logging, "USER_LOG_ROOT", tmp_path / "users")
    repository = InMemorySessionRepository()
    facts = _FactService()
    facts.profile_repo.raise_integrity_once = True

    context, created = open_or_resume_session(repository, facts, session_id="sess_1", data=_context_data())

    assert created is True
    assert context.profile_name == "小海"
    assert facts.profile_repo.get_profile("u1", "p1")["name"] == "小海"


def test_input_contract_and_external_run_id() -> None:
    parsed = _parse_input('{"action":"chat","profile_id":"p1","content":"你好","source":"chat"}')
    assert isinstance(parsed, ChatInput)
    parsed = _parse_input('{"action":"enter_skill","profile_id":"p1","target_skill_id":"interest_explore","source":"toolbar"}')
    assert isinstance(parsed, EnterSkillInput)
    parsed = _parse_input('{"action":"stop","source":"composer"}')
    assert isinstance(parsed, StopInput)
    reserved = _parse_input('{"action":"open_session","profile_id":"p1","opening_mode":"model"}')
    assert reserved.action == "open_session"
    parsed = _parse_input('{"action":"switch_team_member","profile_id":"p1","source":"toolbar","target_expert_id":"family_education_expert","content":"孩子沉迷手机怎么办"}')
    assert isinstance(parsed, SwitchTeamMemberInput)
    with pytest.raises(HTTPException, match="INVALID_INPUT_JSON"):
        _parse_input("not-json")
    with pytest.raises(HTTPException, match="requires source_message_id"):
        _parse_input('{"action":"enter_skill","profile_id":"p1","target_skill_id":"interest_explore","source":"route_suggestion"}')

    coordinator = TurnCoordinator()
    lease = coordinator.acquire("sess_1", "u1", run_id="bff_run_1")
    assert lease.generation == "bff_run_1"
    coordinator.release(lease)


def test_chat_stream_api_accepts_unbound_context_and_rejects_profile_data(api_client) -> None:
    client, repository = api_client
    response = client.post("/api/v2/sessions/chat/stream", json={
        "session_id": "sess_unbound_api",
        "run_id": "run_unbound_api",
        "input": '{"action":"chat","context_scope":"unbound","content":"你好","source":"chat"}',
        "context_data": {"user_id": "u1"},
    })
    assert response.status_code == 200
    frames = _state_frames(response.text)
    assert frames[-1]["context_scope"] == "unbound"
    assert frames[-1]["profile_id"] is None
    assert repository.get("sess_unbound_api").profile_id is None

    invalid = client.post("/api/v2/sessions/chat/stream", json={
        "session_id": "sess_invalid_unbound",
        "run_id": "run_invalid_unbound",
        "input": '{"action":"chat","context_scope":"unbound","profile_id":"p1","content":"你好","source":"chat"}',
        "context_data": {"user_id": "u1"},
    })
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "UNBOUND_CONTEXT_MUST_NOT_INCLUDE_PROFILE_ID"


def test_chat_stream_api_creates_session_and_uses_external_run_id(api_client) -> None:
    client, repository = api_client

    response = client.post("/api/v2/sessions/chat/stream", json=_api_payload(run_id="bff_run_1"))

    assert response.status_code == 200
    frames = _state_frames(response.text)
    assert all(frame["protocol"] == "hailiang.sse.v2" for frame in frames)
    assert all(frame["run_id"] == "bff_run_1" for frame in frames)
    assert frames[-1]["status"] == "completed"
    assert _done_frames(response.text) == [frames[-1]]
    context = repository.get("sess_1")
    assert context.user_id == "u1"
    assert context.profile_id == "p1"
    assert context.session_meta["external_run_ids"] == ["bff_run_1"]


def test_chat_stream_api_rejects_duplicate_external_run_id(api_client) -> None:
    client, repository = api_client

    first = client.post("/api/v2/sessions/chat/stream", json=_api_payload(run_id="same_run"))
    second = client.post("/api/v2/sessions/chat/stream", json=_api_payload(run_id="same_run"))

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "RUN_ID_CONFLICT"
    assert repository.get("sess_1").session_meta["external_run_ids"] == ["same_run"]


def test_team_interaction_commit_retries_from_a_fresh_session_snapshot() -> None:
    """A card confirmation must not expose an internal optimistic-lock retry."""

    class ConflictOnceRepository:
        def __init__(self, context: SessionContext) -> None:
            self._stored = deepcopy(context)
            self.save_attempts = 0
            self.recorded_runs: list[tuple[str, str]] = []

        def get(self, session_id: str) -> SessionContext:
            assert session_id == self._stored.session_id
            return deepcopy(self._stored)

        def save(self, context: SessionContext) -> SessionContext:
            self.save_attempts += 1
            if self.save_attempts == 1:
                # Model an unrelated committed update between read and save.
                self._stored.session_meta["competing_update"] = True
                raise chat_stream.SessionVersionConflict("session changed")
            self._stored = deepcopy(context)
            return context

        def record_run(self, context: SessionContext, run_id: str, action: str) -> None:
            self.recorded_runs.append((run_id, action))

    initial = SessionContext(session_id="sess_handoff_retry", user_id="u1", profile_id="p1")
    repository = ConflictOnceRepository(initial)
    applied_to: list[SessionContext] = []

    def apply(context: SessionContext) -> dict:
        applied_to.append(context)
        context.session_meta["active_expert_id"] = "family_education_expert"
        context.session_meta["handoff_card_status"] = "selected"
        return {"target_expert_id": "family_education_expert"}

    context, result = chat_stream._commit_run_action(
        repository,
        repository.get("sess_handoff_retry"),
        "run_handoff_retry",
        action="confirm_team_handoff",
        apply=apply,
    )

    assert result == {"target_expert_id": "family_education_expert"}
    assert len(applied_to) == 2
    assert repository.save_attempts == 2
    assert context.session_meta["competing_update"] is True
    assert context.session_meta["active_expert_id"] == "family_education_expert"
    assert context.session_meta["external_run_ids"] == ["run_handoff_retry"]
    assert context.session_meta["run_ledger"]["run_handoff_retry"] == {
        "status": "running",
        "action": "confirm_team_handoff",
    }
    assert repository.recorded_runs == [("run_handoff_retry", "confirm_team_handoff")]


def test_free_form_follow_up_expires_team_handoff_without_forcing_a_click(api_client, monkeypatch) -> None:
    """A coordinator recommendation is optional, not a blocking form."""
    client, repository = api_client
    commit_calls: list[str] = []
    original_commit = chat_stream._commit_run_action

    def tracked_commit(*args, **kwargs):
        commit_calls.append(str(kwargs.get("action") or ""))
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(chat_stream, "_commit_run_action", tracked_commit)
    facts = _FactService()
    context, _ = open_or_resume_session(
        repository,
        facts,
        session_id="sess_optional_handoff",
        data=_context_data(),
    )
    context.session_meta.update({
        "expert_team_id": "student_growth_expert_team",
        "expert_id": "career_plan_expert",
        "active_expert_id": "career_plan_expert",
        "expert_selection_source": "manual_team",
        "pending_team_handoff": {"team_id": "student_growth_expert_team"},
    })
    context.messages.append({
        "message_id": "msg_handoff",
        "role": "assistant",
        "content": "建议由家庭教育专家接管。",
        "team_handoff": {
            "team_id": "student_growth_expert_team",
            "candidates": [{"expert_id": "family_education_expert", "mention_name": "家庭教育专家"}],
        },
    })
    repository.save(context)

    response = client.post(
        "/api/v2/sessions/chat/stream",
        json=_api_payload(
            session_id="sess_optional_handoff",
            run_id="run_follow_up",
            input_payload={"action": "chat", "profile_id": "p1", "content": "我再补充一点情况", "source": "chat"},
        ),
    )

    assert response.status_code == 200
    saved = repository.get("sess_optional_handoff")
    assert saved.session_meta["active_expert_id"] == "career_plan_expert"
    assert "pending_team_handoff" not in saved.session_meta
    assert saved.messages[-1]["interaction_states"]["team_handoff"]["status"] == "expired"
    assert commit_calls == ["chat"]


def test_chat_stream_rejects_mismatched_legacy_input_profile_id(api_client) -> None:
    client, repository = api_client
    payload = _api_payload(
        session_id="sess_mismatch_api",
        run_id="run_mismatch",
        input_payload={"action": "chat", "profile_id": "p2", "content": "聊 B", "source": "chat"},
    )
    payload["context_data"]["expert_id"] = "must_not_control_routing"

    response = client.post("/api/v2/sessions/chat/stream", json=payload)

    assert response.status_code == 409
    assert response.json()["detail"] == "PROFILE_CONTEXT_MISMATCH"
    with pytest.raises(KeyError):
        repository.get("sess_mismatch_api")


def test_chat_stream_uses_context_data_profile_without_legacy_input_profile_id(api_client) -> None:
    client, repository = api_client
    payload = {
        "session_id": "sess_context_selected_profile",
        "run_id": "run_context_selected_profile",
        "input": '{"action":"chat","content":"聊孩子","source":"chat"}',
        "context_data": _context_data().model_dump(),
    }

    response = client.post("/api/v2/sessions/chat/stream", json=payload)

    assert response.status_code == 200
    frames = _state_frames(response.text)
    assert all(frame["profile_id"] == "p1" for frame in frames)
    assert repository.get("sess_context_selected_profile").profile_id == "p1"


def test_manual_expert_is_bound_to_chat_and_persists_on_profile_branch(monkeypatch) -> None:
    monkeypatch.setattr(chat_stream, "StreamingRunner", _FakeRunner)
    repository = InMemorySessionRepository()
    coordinator = SimpleNamespace(agent_id="career_plan_expert")
    family = SimpleNamespace(agent_id="family_education_expert")
    team = SimpleNamespace(
        team_id="student_growth_expert_team",
        coordinator_expert_id="career_plan_expert",
        member_expert_ids={"career_plan_expert", "family_education_expert"},
    )
    orchestrator = SimpleNamespace(
        expert_registry={
            "career_plan_expert": coordinator,
            "family_education_expert": family,
        },
        expert_team_registry={"student_growth_expert_team": team},
    )
    app = FastAPI()
    app.include_router(build_chat_stream_router(repository, _FactService(), orchestrator), prefix="/api/v2")
    client = TestClient(app)
    session_id = "sess_manual_expert_" + uuid4().hex
    first = _api_payload(
        session_id=session_id,
        run_id="manual_1",
        input_payload={
            "action": "chat",
            "profile_id": "p1",
            "expert_id": "family_education_expert",
            "content": "分析亲子沟通",
            "source": "chat",
        },
    )

    assert client.post("/api/v2/sessions/chat/stream", json=first).status_code == 200
    context = repository.get(session_id)
    assert context.session_meta["active_expert_id"] == "family_education_expert"
    assert context.session_meta["expert_selection_source"] == "manual"

    follow_up = _api_payload(
        session_id=session_id,
        run_id="manual_2",
        input_payload={"action": "chat", "profile_id": "p1", "content": "继续", "source": "chat"},
    )
    assert client.post("/api/v2/sessions/chat/stream", json=follow_up).status_code == 200
    assert repository.get(session_id).session_meta["active_expert_id"] == "family_education_expert"


def test_plain_chat_does_not_implicitly_activate_an_expert_or_team(api_client) -> None:
    client, repository = api_client

    response = client.post(
        "/api/v2/sessions/chat/stream",
        json=_api_payload(session_id="sess_plain_chat", run_id="plain_chat_1"),
    )

    assert response.status_code == 200
    context = repository.get("sess_plain_chat")
    assert "expert_team_id" not in context.session_meta
    assert "expert_id" not in context.session_meta
    assert "active_expert_id" not in context.session_meta
    assert context.interaction_state["active_skill"] == "general_chat"


def test_chat_stream_api_validates_input_contract(api_client) -> None:
    client, _ = api_client

    invalid_json = client.post("/api/v2/sessions/chat/stream", json=_api_payload(input_payload="not-json"))
    missing_route_fields = client.post(
        "/api/v2/sessions/chat/stream",
        json=_api_payload(
            run_id="run_2",
            input_payload={
                "action": "enter_skill",
                "target_skill_id": "interest_explore",
                "source": "route_suggestion",
            },
        ),
    )
    bad_source = client.post(
        "/api/v2/sessions/chat/stream",
        json=_api_payload(
            run_id="run_3",
            input_payload={"action": "chat", "content": "你好", "source": "toolbar"},
        ),
    )

    assert invalid_json.status_code == 422
    assert invalid_json.json()["detail"] == "INVALID_INPUT_JSON"
    assert missing_route_fields.status_code == 422
    assert bad_source.status_code == 422


def test_stop_accepts_session_and_run_id_without_context_data(api_client) -> None:
    client, _repository = api_client
    initial = client.post("/api/v2/sessions/chat/stream", json=_api_payload(run_id="initial_run"))
    assert initial.status_code == 200

    runner = _FakeRunner.instances[-1]
    runner.reserve_turn("sess_1", "u1", run_id="stop_run")
    response = client.post(
        "/api/v2/sessions/chat/stream",
        json={
            "session_id": "sess_1",
            "run_id": "stop_run",
            "input": '{"action":"stop","source":"composer"}',
        },
    )

    assert response.status_code == 200
    frames = _state_frames(response.text)
    assert frames[-1]["run_id"] == "stop_run"
    assert frames[-1]["status"] == "stopped"


def test_non_stop_actions_still_require_session_and_context_data(api_client) -> None:
    client, _repository = api_client

    response = client.post(
        "/api/v2/sessions/chat/stream",
        json={
            "session_id": "sess_1",
            "run_id": "missing_context",
            "input": '{"action":"chat","profile_id":"p1","content":"你好","source":"chat"}',
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "context_data is required for non-stop actions"


def test_skill_transition_api_uses_single_endpoint_and_external_run_id(api_client) -> None:
    client, repository = api_client

    enter = client.post(
        "/api/v2/sessions/chat/stream",
        json=_api_payload(
            run_id="run_enter",
            input_payload={"action": "enter_skill", "target_skill_id": "interest_explore", "source": "toolbar"},
        ),
    )
    quit_skill = client.post(
        "/api/v2/sessions/chat/stream",
        json=_api_payload(
            run_id="run_quit",
            input_payload={"action": "quit_skill", "target_skill_id": "interest_explore", "source": "exit_button"},
        ),
    )

    assert enter.status_code == 200
    enter_frames = _state_frames(enter.text)
    assert all(frame["run_id"] == "run_enter" for frame in enter_frames)
    assert enter_frames[-1]["skill_transition"]["to_skill_id"] == "interest_explore"
    assert quit_skill.status_code == 200
    quit_frames = _state_frames(quit_skill.text)
    assert quit_frames[-1]["status"] == "completed"
    assert all(frame["profile_id"] == "p1" for frame in quit_frames)
    assert quit_frames[-1]["run_id"] == "run_quit"
    assert quit_frames[-1]["skill_transition"]["to_skill_id"] == "general_chat"
    assert repository.get("sess_1").interaction_state["active_skill"] == "general_chat"


def test_quit_skill_target_mismatch_does_not_claim_run_id(api_client) -> None:
    client, repository = api_client
    client.post(
        "/api/v2/sessions/chat/stream",
        json=_api_payload(
            run_id="run_enter",
            input_payload={"action": "enter_skill", "target_skill_id": "interest_explore", "source": "toolbar"},
        ),
    )

    response = client.post(
        "/api/v2/sessions/chat/stream",
        json=_api_payload(
            run_id="bad_quit",
            input_payload={"action": "quit_skill", "target_skill_id": "score_improve", "source": "exit_button"},
        ),
    )

    assert response.status_code == 409
    assert repository.get("sess_1").session_meta["external_run_ids"] == ["run_enter"]
