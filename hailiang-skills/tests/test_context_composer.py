from __future__ import annotations

from hailiang_skills.core.context_composer import ContextComposer
from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.profile_candidate_archive import archive_candidate, candidate_archive
from hailiang_skills.storage.repositories.profile_memory_repo import InMemoryProfileMemoryRepository
from hailiang_skills.storage.repositories.profile_memory_repo import PostgresConversationMemoryRepository, PostgresProfileMemoryRepository
from hailiang_skills.storage.database import Base, ProfileRow, build_session_factory
from sqlalchemy import create_engine


def test_composer_preserves_current_input_and_bounds_lower_priority_sections() -> None:
    composer = ContextComposer(8_000)
    memory, status = composer.compose_memory(
        {
            "summary": "旧摘要" * 5_000,
            "facts": {"a": "事实" * 3_000},
            "recent_messages": [{"role": "user", "content": "旧消息" * 10_000}],
            "reference_messages": [{"role": "assistant", "content": "跨技能" * 5_000}],
        },
        confirmed_facts={"grade": "高一"},
        archive=[{"value": "偏好" * 5_000}],
        current_message="当前问题",
        activity_state={"form": "当前表单"},
    )
    assert status["current_message_rejected"] is False
    assert status["estimated_prompt_tokens"] <= 8_000
    assert status["trimmed_sections"]
    assert memory["recent_messages"]


def test_profile_memory_retrieval_is_profile_scoped_and_replaces_candidate() -> None:
    repository = InMemoryProfileMemoryRepository()
    a = SessionContext(session_id="s", user_id="u", profile_id="a")
    b = SessionContext(session_id="s", user_id="u", profile_id="b")
    assert archive_candidate(a, key="interest", value="爱运动", source_skill="talent", source_turn_id="t1", evidence_summary="喜欢篮球", confidence=0.8, repository=repository)
    assert archive_candidate(a, key="interest", value="喜欢绘画", source_skill="talent", source_turn_id="t2", evidence_summary="最近开始画画", confidence=0.9, repository=repository)
    assert candidate_archive(b, repository=repository, query_text="兴趣") == []
    records = candidate_archive(a, repository=repository, query_text="绘画")
    assert len(records) == 1
    assert records[0]["value"] == "喜欢绘画"
    assert records[0]["history"]["recent"][0]["value"] == "爱运动"


def test_postgres_archive_and_checkpoint_persist_by_profile_branch() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory.begin() as db:
        db.add(ProfileRow(profile_id="a", user_id="u", payload={}, facts={}))
    archives = PostgresProfileMemoryRepository(factory)
    archives.record_candidate(
        profile_id="a", fact_key="interest", value="机器人", skill_id="talent", domain="talent",
        source_session_id="s", source_turn_id="t", evidence_summary="喜欢搭建机器人", confidence=0.8,
    )
    assert archives.retrieve("a", "机器人")[0]["profile_id"] == "a"
    checkpoints = PostgresConversationMemoryRepository(factory)
    checkpoints.save("s", "a", {"summary": "已完成基础了解"})
    assert checkpoints.load("s", "a") == {"summary": "已完成基础了解"}
