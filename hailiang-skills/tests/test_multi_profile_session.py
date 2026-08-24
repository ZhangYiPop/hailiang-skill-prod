from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine

from hailiang_skills.api.profile_targeting import ProfileTargetResolver
from hailiang_skills.api.session_lifecycle import ContextData, open_or_resume_session
from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.fact_service import FactService as RuntimeFactService
from hailiang_skills.runtime_bridge.default_expert_team import (
    initialize_default_expert_team,
    require_default_expert_team,
)
from hailiang_skills.runtime_bridge.conversation_memory import ConversationMemoryStore
from hailiang_skills.schemas.facts import KnownFacts
from hailiang_skills.storage.database import Base, build_session_factory
from hailiang_skills.storage.repositories.postgres_repo import PostgresSessionRepository
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository


class _Profiles:
    def __init__(self) -> None:
        self.profiles: dict[str, dict] = {}
        self.facts: dict[str, KnownFacts] = {}

    def get_profile_by_id(self, profile_id: str) -> dict:
        if profile_id not in self.profiles:
            raise KeyError(profile_id)
        return dict(self.profiles[profile_id])

    def create_profile(self, user_id: str, *, profile_id: str, name: str, **_: object) -> dict:
        value = {"user_id": user_id, "profile_id": profile_id, "name": name}
        self.profiles[profile_id] = value
        return dict(value)

    def update_profile(self, user_id: str, profile_id: str, *, name: str, **_: object) -> dict:
        value = {**self.profiles[profile_id], "user_id": user_id, "name": name}
        self.profiles[profile_id] = value
        return dict(value)

    def get_profile_facts(self, _user_id: str, profile_id: str) -> KnownFacts:
        return self.facts.setdefault(profile_id, KnownFacts())

    def save_profile_facts(self, _user_id: str, profile_id: str, facts: KnownFacts) -> KnownFacts:
        self.facts[profile_id] = facts
        return facts


class _Facts:
    def __init__(self) -> None:
        self.profile_repo = _Profiles()

    validate_configured_update = staticmethod(RuntimeFactService.validate_configured_update)

    def get_profile_facts(self, user_id: str, profile_id: str) -> KnownFacts:
        return self.profile_repo.get_profile_facts(user_id, profile_id)

    def hydrate_context(self, context: SessionContext) -> SessionContext:
        context.load_effective_facts(
            shared_facts=KnownFacts(),
            profile_facts=deepcopy(self.profile_repo.get_profile_facts(context.user_id, str(context.profile_id))),
            session_facts=context.session_facts,
        )
        return context

    def persist_context(self, context: SessionContext) -> None:
        self.profile_repo.save_profile_facts(context.user_id, str(context.profile_id), context.profile_facts)


def _context(profile_id: str, name: str, *, grade: str | None = None) -> ContextData:
    return ContextData(
        user_id="user_1",
        profile_id=profile_id,
        student_name=name,
        grade=grade,
    )


def test_resolver_supports_input_context_and_strict_match_without_contract_change() -> None:
    forwarded = _context("profile_a", "小A")
    input_resolution = ProfileTargetResolver("input").resolve(
        input_profile_id="profile_b",
        context_data=forwarded,
    )
    assert input_resolution.target_profile_id == "profile_b"
    assert input_resolution.status == "mismatched"
    assert input_resolution.allow_context_seed is False

    context_resolution = ProfileTargetResolver("context_data").resolve(
        input_profile_id="profile_b",
        context_data=forwarded,
    )
    assert context_resolution.target_profile_id == "profile_a"
    assert context_resolution.allow_context_seed is True

    with pytest.raises(HTTPException, match="PROFILE_CONTEXT_MISMATCH"):
        ProfileTargetResolver("strict_match").resolve(
            input_profile_id="profile_b",
            context_data=forwarded,
        )


def test_profile_branches_isolate_messages_skill_expert_and_resume_state() -> None:
    repository = InMemorySessionRepository()
    facts = _Facts()
    session_id = "sess_multi_" + __import__("uuid").uuid4().hex
    session, created = open_or_resume_session(
        repository,
        facts,
        session_id=session_id,
        data=_context("profile_a", "小A"),
    )
    assert created is True
    session.add_message("user", "A 的问题")
    session.skill_states["skill_runtime"]["active_skill_id"] = "skill_a"
    session.session_meta["active_expert_id"] = "expert_a"
    repository.save(session)

    branch_b, created = open_or_resume_session(
        repository,
        facts,
        session_id=session_id,
        data=_context("profile_b", "小B"),
        target_profile_id="profile_b",
    )
    assert created is False
    assert branch_b.messages == []
    assert branch_b.skill_states["skill_runtime"]["active_skill_id"] == "general_chat"
    assert "active_expert_id" not in branch_b.session_meta
    branch_b.add_message("user", "B 的问题")
    branch_b.session_meta["active_expert_id"] = "expert_b"
    repository.save(branch_b)

    resumed_a, _ = open_or_resume_session(
        repository,
        facts,
        session_id=session_id,
        data=_context("profile_a", "小A"),
        target_profile_id="profile_a",
    )
    assert [item["content"] for item in resumed_a.messages] == ["A 的问题"]
    assert resumed_a.skill_states["skill_runtime"]["active_skill_id"] == "skill_a"
    assert resumed_a.session_meta["active_expert_id"] == "expert_a"
    assert resumed_a.session_meta["resume_recap_pending"] is True
    assert [item["item_type"] for item in resumed_a.timeline_items].count("profile_switch") == 2
    assert {
        item["profile_id"]
        for item in resumed_a.timeline_items
        if item["item_type"] == "message"
    } == {"profile_a", "profile_b"}


def test_mismatched_new_target_gets_no_forwarded_name_or_facts() -> None:
    repository = InMemorySessionRepository()
    facts = _Facts()
    session_id = "sess_mismatch_" + __import__("uuid").uuid4().hex
    session, created = open_or_resume_session(
        repository,
        facts,
        session_id=session_id,
        data=_context("profile_a", "小A", grade="高一"),
        target_profile_id="profile_b",
        allow_context_seed=False,
    )
    assert created is True
    assert session.profile_id == "profile_b"
    assert session.profile_name is None
    assert session.profile_facts.get_value("grade") is None
    assert facts.profile_repo.profiles["profile_b"]["name"] == ""


def test_default_team_uses_configured_coordinator_and_validates_membership() -> None:
    coordinator = SimpleNamespace(agent_id="career_plan_expert")
    team = SimpleNamespace(
        team_id="student_growth_expert_team",
        coordinator_expert_id="career_plan_expert",
        member_expert_ids={"career_plan_expert", "family_education_expert"},
    )
    orchestrator = SimpleNamespace(
        expert_registry={"career_plan_expert": coordinator},
        expert_team_registry={"student_growth_expert_team": team},
    )
    context = SessionContext(profile_id="profile_a", interaction_state={"active_skill": "old"})
    context.skill_states["skill_runtime"] = {"active_skill_id": "old"}

    assert require_default_expert_team(orchestrator) is team
    assert initialize_default_expert_team(context, orchestrator, force=True) is True
    assert context.interaction_state["active_skill"] == "general_chat"
    assert context.session_meta == {
        "expert_team_id": "student_growth_expert_team",
        "active_expert_id": "career_plan_expert",
        "expert_id": "career_plan_expert",
        "expert_selection_source": "default_coordinator",
    }

    invalid = SimpleNamespace(
        expert_registry={"career_plan_expert": coordinator},
        expert_team_registry={
            "student_growth_expert_team": SimpleNamespace(
                team_id="student_growth_expert_team",
                coordinator_expert_id="career_plan_expert",
                member_expert_ids={"family_education_expert"},
            )
        },
    )
    with pytest.raises(RuntimeError, match="coordinator is invalid"):
        require_default_expert_team(invalid)


def test_clean_multi_profile_schema_can_be_created_from_empty_database() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    expected = {
        "chat_sessions",
        "application_profile_projections",
        "chat_session_profiles",
        "chat_session_items",
        "chat_runs",
        "chat_context_checkpoints",
        "chat_context_jobs",
    }
    assert expected.issubset(set(Base.metadata.tables))


def test_postgres_repository_round_trips_isolated_profile_branches() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    repository = PostgresSessionRepository(build_session_factory(engine))
    context = SessionContext(
        session_id="sess_sql",
        user_id="user_1",
        profile_id="profile_a",
        profile_name="小A",
    )
    context.add_message("user", "A 的数据库消息")
    repository.create(context)

    context.activate_profile_branch("profile_b", profile_name="小B")
    context.append_profile_switch(from_profile_id="profile_a", from_profile_name="小A")
    context.add_message("user", "B 的数据库消息")
    repository.save(context)

    restored = repository.get("sess_sql")
    assert restored.profile_id == "profile_b"
    assert [item["content"] for item in restored.messages] == ["B 的数据库消息"]
    restored.activate_profile_branch("profile_a")
    assert [item["content"] for item in restored.messages] == ["A 的数据库消息"]
    assert set(restored.profile_branches) == {"profile_a", "profile_b"}
    assert any(item["item_type"] == "profile_switch" for item in restored.timeline_items)


def test_hard_context_threshold_compresses_synchronously_and_keeps_recent_turns(tmp_path) -> None:
    store = ConversationMemoryStore(
        runtime_dir=tmp_path,
        active_window_messages=16,
        context_window_tokens=4_000,
        async_checkpoint_ratio=0.60,
        sync_compression_ratio=0.80,
    )
    long_text = "孩子本轮的具体情况" * 80
    for index in range(10):
        store.append_turn(
            user_id="user_1",
            session_id="sess__profile__profile_a",
            active_skill_id="general_chat",
            user_message=f"{index}:{long_text}",
            assistant_message=f"{index}:{long_text}",
        )

    result = store.prepare_for_turn(
        user_id="user_1",
        session_id="sess__profile__profile_a",
        active_skill_id="general_chat",
        skill_dir=None,
        llm_client=None,
        defer_update=True,
    )

    assert result.context["status"]["checkpoint_mode"] == "sync_compression"
    assert result.context["status"]["memory_update_status"] == "degraded_success"
    assert len(result.context["recent_messages"]) == 16
    assert result.context["summary"]
