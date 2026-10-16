from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hailiang_skills.skill_runtime.data_loader import load_generated_tool_registry
from hailiang_skills.skill_runtime.models import SessionState, SkillBundle, SkillContract
from hailiang_skills.skill_runtime.tools import (
    DATABASE_QUERY_TOOLS,
    QUERY_SCORE_LINES_TOOL_NAME,
    build_tool_specs,
    default_tool_registry,
    execute_tool_call,
    make_tool_call_request,
    run_database_query,
)

SCORE_VIEW = {
    "v_score_lines": {
        "purpose": "测试用分数线视图",
        "columns": ["province", "category", "batch", "score"],
        "filterable": ["province", "category", "batch"],
        "filters": {"province": "exact", "category": "exact", "batch": "like"},
        "required": ["province"],
    }
}


@pytest.fixture()
def score_db(tmp_path: Path) -> str:
    db_path = tmp_path / "score.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE v_score_lines (province TEXT, category TEXT, batch TEXT, score TEXT)"
    )
    conn.executemany(
        "INSERT INTO v_score_lines VALUES (?, ?, ?, ?)",
        [
            ("浙江", "物理类", "本科线", "492"),
            ("浙江", "历史类", "本科线", "480"),
            ("江苏", "物理类", "特招线", "520"),
        ],
    )
    conn.commit()
    conn.close()
    return "sqlite:///" + db_path.as_posix()


def _bundle(tmp_path: Path, deps: list[str], *, tools: dict | None = None) -> SkillBundle:
    config = {"tools": tools} if tools else None
    return SkillBundle(
        source="test",
        archive_path=tmp_path,
        extracted_dir=tmp_path,
        root_dir=tmp_path,
        root_name="test",
        metadata={"name": "test", "tool_dependency": deps},
        skill_file=tmp_path / "SKILL.md",
        tool_registry=default_tool_registry(config),
        contract=SkillContract(skill_id="test"),
    )


def test_database_tools_registered_and_closed_without_connection():
    registry = default_tool_registry()
    enabled = {item.name for item in registry.capabilities if item.enabled}
    assert set(DATABASE_QUERY_TOOLS).isdisjoint(enabled)
    for tool_name in DATABASE_QUERY_TOOLS:
        config = registry.settings["tools"][tool_name]
        assert config["connection"] == ""
        assert config["connection_env"] == ""
        assert config["allowed_views"] == {}
        assert "postgresql+psycopg://" in config["connection_note"]


def test_generated_registry_keeps_connections_empty():
    registry = default_tool_registry(load_generated_tool_registry())
    for tool_name in DATABASE_QUERY_TOOLS:
        config = registry.settings["tools"][tool_name]
        capability = next(item for item in registry.capabilities if item.name == tool_name)
        assert config["connection"] == ""
        assert config["connection_env"] == ""
        assert config["allowed_views"] == {}
        assert capability.enabled is False


def test_connection_env_outranks_inline_connection(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HAILIANG_TEST_DB_URL", "")
    registry = default_tool_registry(
        {
            "tools": {
                QUERY_SCORE_LINES_TOOL_NAME: {
                    "connection": "postgresql+psycopg://user:secret@db.example/app",
                    "connection_env": "HAILIANG_TEST_DB_URL",
                }
            }
        }
    )
    capability = next(item for item in registry.capabilities if item.name == QUERY_SCORE_LINES_TOOL_NAME)
    assert capability.enabled is False

    monkeypatch.setenv("HAILIANG_TEST_DB_URL", "postgresql+psycopg://user:secret@db.example/app")
    registry = default_tool_registry(
        {
            "tools": {
                QUERY_SCORE_LINES_TOOL_NAME: {
                    "connection": "",
                    "connection_env": "HAILIANG_TEST_DB_URL",
                }
            }
        }
    )
    capability = next(item for item in registry.capabilities if item.name == QUERY_SCORE_LINES_TOOL_NAME)
    assert capability.enabled is True


def test_undeclared_skill_does_not_see_database_tools(tmp_path: Path):
    bundle = _bundle(tmp_path, ["subject_requirements"])
    names = {item.name for item in build_tool_specs(bundle, SessionState(session_id="s"))}
    assert set(DATABASE_QUERY_TOOLS).isdisjoint(names)


def test_declared_tool_stays_disabled_until_views_exist(score_db: str, tmp_path: Path):
    bundle = _bundle(
        tmp_path,
        [QUERY_SCORE_LINES_TOOL_NAME],
        tools={QUERY_SCORE_LINES_TOOL_NAME: {"connection": score_db}},
    )
    spec = next(item for item in build_tool_specs(bundle, SessionState(session_id="s")) if item.name == QUERY_SCORE_LINES_TOOL_NAME)
    assert spec.enabled is False
    result = execute_tool_call(bundle, SessionState(session_id="s"), make_tool_call_request(QUERY_SCORE_LINES_TOOL_NAME, {"province": "浙江"}))
    assert result.ok is False
    assert "未启用" in result.error


def test_score_query_uses_configured_view(score_db: str, tmp_path: Path):
    bundle = _bundle(
        tmp_path,
        [QUERY_SCORE_LINES_TOOL_NAME],
        tools={QUERY_SCORE_LINES_TOOL_NAME: {"connection": score_db, "allowed_views": SCORE_VIEW}},
    )
    spec = next(item for item in build_tool_specs(bundle, SessionState(session_id="s")) if item.name == QUERY_SCORE_LINES_TOOL_NAME)
    assert spec.enabled is True
    assert spec.parameters_schema["required"] == ["province"]
    result = run_database_query(bundle, QUERY_SCORE_LINES_TOOL_NAME, {"province": "浙江", "batch": "本科", "fields": ["score"]})
    assert result["row_count"] == 2
    assert result["rows"] == [{"score": "492"}, {"score": "480"}]


def test_query_rejects_unknown_field_and_sql_fragment(score_db: str, tmp_path: Path):
    bundle = _bundle(
        tmp_path,
        [QUERY_SCORE_LINES_TOOL_NAME],
        tools={QUERY_SCORE_LINES_TOOL_NAME: {"connection": score_db, "allowed_views": SCORE_VIEW}},
    )
    with pytest.raises(ValueError, match="不支持的返回字段"):
        run_database_query(bundle, QUERY_SCORE_LINES_TOOL_NAME, {"province": "浙江", "fields": ["score; drop table v_score_lines"]})
    with pytest.raises(ValueError, match="必填"):
        run_database_query(bundle, QUERY_SCORE_LINES_TOOL_NAME, {"category": "物理类"})

    injected = run_database_query(
        bundle,
        QUERY_SCORE_LINES_TOOL_NAME,
        {"province": "浙江'; DROP TABLE v_score_lines;--"},
    )
    assert injected["row_count"] == 0
    conn = sqlite3.connect(score_db.removeprefix("sqlite:///"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM v_score_lines").fetchone()[0] == 3
    finally:
        conn.close()
