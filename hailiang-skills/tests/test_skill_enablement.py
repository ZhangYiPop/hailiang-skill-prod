from __future__ import annotations

from pathlib import Path

import pytest

from hailiang_skills.core.skill_display import build_skill_catalog
from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.streaming_runner import StreamingRunner
from hailiang_skills.runtime_bridge.runtime_config import load_runtime_bridge_config
from hailiang_skills.runtime_bridge.main_planner import PROJECT_RUNTIME_SKILLS_ROOT
from hailiang_skills.skill_runtime.intent_router import IntentRouter
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository


def test_runtime_config_defaults_missing_skill_entries_to_enabled(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.yml"
    config_path.write_text("runtime_dir: runtime\n", encoding="utf-8")

    config = load_runtime_bridge_config(config_path)

    assert config.skill_enabled_by_id == {}


def test_runtime_config_normalizes_legacy_skill_aliases(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.yml"
    config_path.write_text(
        "skill_management:\n  enabled:\n    admission: false\n    unknown_skill: false\n",
        encoding="utf-8",
    )

    config = load_runtime_bridge_config(config_path)

    assert config.skill_enabled_by_id["mock_admission"] is False
    assert config.skill_enabled_by_id["unknown_skill"] is False


def test_runtime_config_defaults_to_ms_agent_tool_routing(tmp_path: Path) -> None:
    config = load_runtime_bridge_config(tmp_path / "missing.yml")

    assert config.tool_routing_mode == "ms_agent"
    assert config.tool_routing_fallback_on_invalid is True


def test_runtime_config_can_restore_standalone_tool_routing(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.yml"
    config_path.write_text(
        "tool_routing:\n  mode: standalone\n  fallback_on_invalid: false\n",
        encoding="utf-8",
    )

    config = load_runtime_bridge_config(config_path)

    assert config.tool_routing_mode == "standalone"
    assert config.tool_routing_fallback_on_invalid is False


def test_runtime_config_tool_routing_environment_overrides_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "runtime.yml"
    config_path.write_text(
        "tool_routing:\n  mode: standalone\n  fallback_on_invalid: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HAILIANG_TOOL_ROUTING_MODE", "ms_agent")
    monkeypatch.setenv("HAILIANG_TOOL_ROUTING_FALLBACK_ON_INVALID", "true")

    config = load_runtime_bridge_config(config_path)

    assert config.tool_routing_mode == "ms_agent"
    assert config.tool_routing_fallback_on_invalid is True


def test_runtime_config_progress_simulation_defaults_and_yaml_override(tmp_path: Path) -> None:
    default = load_runtime_bridge_config(tmp_path / "missing.yml")
    assert default.progress_simulation_enabled is True
    assert default.progress_simulation_interval_s == 0.45
    assert default.progress_simulation_jitter_s == 0.25
    assert default.progress_simulation_min_duration_s == 1.2

    config_path = tmp_path / "runtime.yml"
    config_path.write_text(
        "progress_simulation:\n"
        "  enabled: false\n"
        "  interval_s: 0.8\n"
        "  jitter_s: 0.1\n"
        "  min_duration_s: 2.5\n",
        encoding="utf-8",
    )
    overridden = load_runtime_bridge_config(config_path)
    assert overridden.progress_simulation_enabled is False
    assert overridden.progress_simulation_interval_s == 0.8
    assert overridden.progress_simulation_jitter_s == 0.1
    assert overridden.progress_simulation_min_duration_s == 2.5


def test_runtime_config_invalid_tool_routing_mode_fails_safe(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_path = tmp_path / "runtime.yml"
    config_path.write_text("tool_routing:\n  mode: unknown\n", encoding="utf-8")

    config = load_runtime_bridge_config(config_path)

    assert config.tool_routing_mode == "ms_agent"
    assert "defaulting to ms_agent" in caplog.text


def test_disabled_skill_is_absent_from_registry_catalog_and_intent_examples() -> None:
    registry = load_local_skill_registry(
        PROJECT_RUNTIME_SKILLS_ROOT,
        enabled_by_id={"multi_path_planning": False},
    )

    assert registry.get("multi_path_planning") is None
    assert registry.get("convergence") is None
    assert "multi_path_planning" not in registry.enabled_bundles()
    catalog_ids = {item["skill_id"] for item in build_skill_catalog(registry)}
    assert "multi_path_planning" not in catalog_ids

    router = IntentRouter(bundles=registry.enabled_bundles(), embedding_client=None, llm_client=None)
    assert all(item.skill_id != "multi_path_planning" for item in router._examples)


def test_enabled_skill_remains_available() -> None:
    registry = load_local_skill_registry(
        PROJECT_RUNTIME_SKILLS_ROOT,
        enabled_by_id={"multi_path_planning": True},
    )

    assert registry.is_enabled("multi_path_planning") is True
    assert registry.get("multi_path_planning") is not None


def test_route_thresholds_are_loaded_from_the_central_intent_router_config() -> None:
    registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
    config = registry.get_raw("career_plan_entity").runtime_metadata.planner.intent_router

    assert config.route_suggestion_min_confidence == 0.72
    assert config.route_suggestion_min_confidence_without_context == 0.72
    assert config.route_suggestion_card_threshold == 0.72
    assert config.specialist_switch_threshold == 0.92


class _FactService:
    def hydrate_context(self, context):
        return context


class _Orchestrator:
    def __init__(self, runtime_registry) -> None:
        self.runtime_registry = runtime_registry


def test_disabled_skill_transition_is_rejected_server_side() -> None:
    registry = load_local_skill_registry(
        PROJECT_RUNTIME_SKILLS_ROOT,
        enabled_by_id={"multi_path_planning": False},
    )
    session = SessionContext(session_id="enablement-session", user_id="user")
    repository = InMemorySessionRepository()
    repository.create(session)
    runner = StreamingRunner(repository, _FactService(), _Orchestrator(registry))

    with pytest.raises(ValueError, match="SKILL_DISABLED"):
        runner.prepare_skill_transition(
            session.session_id,
            session.user_id,
            action="enter",
            target_skill_id="multi_path_planning",
            source="toolbar",
        )
