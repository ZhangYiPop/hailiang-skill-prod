from __future__ import annotations

from copy import deepcopy
from pathlib import Path
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
            yield builder.encode("run_started", {"risk_stage": "input"})
            yield builder.encode("run_completed", {"message_id": "msg_fake", "status": "completed"})
        finally:
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
            yield builder.encode("run_started", {"risk_stage": "input"})
            yield builder.encode("skill_transition", {"action": action, "to_skill_id": to_skill, "source": kwargs["source"]})
            yield builder.encode("run_completed", {"message_id": "msg_transition", "status": "completed"})
        finally:
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
        input_payload = {"action": "chat", "content": "你好", "source": "chat"}
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
    app.include_router(build_chat_stream_router(repository, _FactService(), object()), prefix="/api/v1")
    return TestClient(app), repository


def test_open_or_resume_seeds_only_new_session(tmp_path: Path, monkeypatch) -> None:
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
    assert facts.get_profile_facts("u1", "p1").get_value("grade") == "高二"


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
            data=_context_data().model_copy(update={"profile_id": "p2"}),
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
    parsed = _parse_input('{"action":"chat","content":"你好","source":"chat"}')
    assert isinstance(parsed, ChatInput)
    parsed = _parse_input('{"action":"enter_skill","target_skill_id":"interest_explore","source":"toolbar"}')
    assert isinstance(parsed, EnterSkillInput)
    parsed = _parse_input('{"action":"stop","source":"composer"}')
    assert isinstance(parsed, StopInput)
    parsed = _parse_input('{"action":"switch_team_member","source":"toolbar","target_expert_id":"family_education_expert","content":"孩子沉迷手机怎么办"}')
    assert isinstance(parsed, SwitchTeamMemberInput)
    with pytest.raises(HTTPException, match="INVALID_INPUT_JSON"):
        _parse_input("not-json")
    with pytest.raises(HTTPException, match="requires source_message_id"):
        _parse_input('{"action":"enter_skill","target_skill_id":"interest_explore","source":"route_suggestion"}')

    coordinator = TurnCoordinator()
    lease = coordinator.acquire("sess_1", "u1", run_id="bff_run_1")
    assert lease.generation == "bff_run_1"
    coordinator.release(lease)


def test_chat_stream_api_creates_session_and_uses_external_run_id(api_client) -> None:
    client, repository = api_client

    response = client.post("/api/v1/sessions/chat/stream", json=_api_payload(run_id="bff_run_1"))

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

    first = client.post("/api/v1/sessions/chat/stream", json=_api_payload(run_id="same_run"))
    second = client.post("/api/v1/sessions/chat/stream", json=_api_payload(run_id="same_run"))

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"] == "RUN_ID_CONFLICT"
    assert repository.get("sess_1").session_meta["external_run_ids"] == ["same_run"]


def test_chat_stream_api_validates_input_contract(api_client) -> None:
    client, _ = api_client

    invalid_json = client.post("/api/v1/sessions/chat/stream", json=_api_payload(input_payload="not-json"))
    missing_route_fields = client.post(
        "/api/v1/sessions/chat/stream",
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
        "/api/v1/sessions/chat/stream",
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
    initial = client.post("/api/v1/sessions/chat/stream", json=_api_payload(run_id="initial_run"))
    assert initial.status_code == 200

    runner = _FakeRunner.instances[-1]
    runner.reserve_turn("sess_1", "u1", run_id="stop_run")
    response = client.post(
        "/api/v1/sessions/chat/stream",
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
        "/api/v1/sessions/chat/stream",
        json={
            "session_id": "sess_1",
            "run_id": "missing_context",
            "input": '{"action":"chat","content":"你好","source":"chat"}',
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "context_data is required for non-stop actions"


def test_skill_transition_api_uses_single_endpoint_and_external_run_id(api_client) -> None:
    client, repository = api_client

    enter = client.post(
        "/api/v1/sessions/chat/stream",
        json=_api_payload(
            run_id="run_enter",
            input_payload={"action": "enter_skill", "target_skill_id": "interest_explore", "source": "toolbar"},
        ),
    )
    quit_skill = client.post(
        "/api/v1/sessions/chat/stream",
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
    assert [frame["status"] for frame in quit_frames] == ["streaming", "streaming", "completed"]
    assert quit_frames[-1]["run_id"] == "run_quit"
    assert quit_frames[-1]["skill_transition"]["to_skill_id"] == "general_chat"
    assert repository.get("sess_1").interaction_state["active_skill"] == "general_chat"


def test_quit_skill_target_mismatch_does_not_claim_run_id(api_client) -> None:
    client, repository = api_client
    client.post(
        "/api/v1/sessions/chat/stream",
        json=_api_payload(
            run_id="run_enter",
            input_payload={"action": "enter_skill", "target_skill_id": "interest_explore", "source": "toolbar"},
        ),
    )

    response = client.post(
        "/api/v1/sessions/chat/stream",
        json=_api_payload(
            run_id="bad_quit",
            input_payload={"action": "quit_skill", "target_skill_id": "score_improve", "source": "exit_button"},
        ),
    )

    assert response.status_code == 409
    assert repository.get("sess_1").session_meta["external_run_ids"] == ["run_enter"]
