from types import SimpleNamespace

import pytest

from hailiang_skills.api.routes import chat_stream
from hailiang_skills.api.routes.chat_stream import ChatInput, _tester_model_name
from hailiang_skills.llm.config import UnknownTesterModelError, load_llm_config
from hailiang_skills.runtime_bridge import main_planner
from hailiang_skills.runtime_bridge.main_planner import MainPlannerOrchestrator


def _chat_input(model: str) -> ChatInput:
    return ChatInput.model_validate(
        {
            "action": "chat",
            "content": "你好",
            "source": "chat",
            "model": model,
            "expert_context": {
                "expert_team_id": None,
                "expert_id": None,
                "operation": "continue",
            },
        }
    )


def test_default_config_lists_flash_and_pro_on_the_same_key() -> None:
    config = load_llm_config()

    assert config.model == "deepseek-v4-flash-0731"
    assert [(item.id, item.model) for item in config.selectable_models] == [
        ("flash", "deepseek-v4-flash-0731"),
        ("pro", "deepseek-v4-pro"),
        ("flash41", "deepseek-v4.1-flash"),
        ("flash41_1017", "deepseek-v4.1-flash-1017"),
    ]
    assert config.resolve_tester_model("pro", environment="test") == "deepseek-v4-pro"
    assert config.resolve_tester_model("flash", environment="test") == config.model
    assert config.resolve_tester_model("flash41", environment="test") == "deepseek-v4.1-flash"
    assert config.resolve_tester_model("flash41_1017", environment="test") == "deepseek-v4.1-flash-1017"
    assert config.resolve_tester_model("pro", environment="prod") == ""
    assert config.resolve_tester_model("", environment="test") == ""
    with pytest.raises(UnknownTesterModelError):
        config.resolve_tester_model("other", environment="test")


def test_tester_request_resolves_pro_and_rejects_unknown_ids(monkeypatch) -> None:
    monkeypatch.setattr(chat_stream, "deployment_environment", lambda: "test")
    config = load_llm_config()
    orchestrator = SimpleNamespace(llm_config=config)

    assert _tester_model_name(orchestrator, _chat_input("pro")) == "deepseek-v4-pro"
    assert _tester_model_name(orchestrator, _chat_input("")) == ""
    with pytest.raises(Exception, match="UNKNOWN_MODEL"):
        _tester_model_name(orchestrator, _chat_input("other"))


def test_context_model_switch_keeps_endpoint_and_key(monkeypatch) -> None:
    config = load_llm_config()
    probe = SimpleNamespace(
        llm_config=config,
        test_llm_routing=SimpleNamespace(matches=lambda *_args, **_kwargs: False),
    )
    monkeypatch.setattr(main_planner, "deployment_environment", lambda: "test")

    selected = MainPlannerOrchestrator._llm_config_for_context(
        probe,
        SimpleNamespace(user_id="u1", session_meta={"requested_llm_model": "pro"}),
    )
    unchanged = MainPlannerOrchestrator._llm_config_for_context(
        probe,
        SimpleNamespace(user_id="u1", session_meta={"requested_llm_model": "flash"}),
    )

    assert selected.model == "deepseek-v4-pro"
    assert selected.base_url == config.base_url
    assert selected.api_key_env == config.api_key_env
    assert unchanged is config

    monkeypatch.setattr(main_planner, "deployment_environment", lambda: "prod")
    production = MainPlannerOrchestrator._llm_config_for_context(
        probe,
        SimpleNamespace(user_id="u1", session_meta={"requested_llm_model": "pro"}),
    )
    assert production is config
