from types import SimpleNamespace

import pytest

from hailiang_skills.core.context import SessionContext
from hailiang_skills.harness import policy
from hailiang_skills.harness.contracts import HarnessError
from hailiang_skills.harness.integration import HarnessIntegration
from hailiang_skills.skill_runtime.models import LLMConfig, SkillContract, ToolRegistry


def test_candidate_internal_skill_stays_expert_owned_and_model_is_frozen(tmp_path, monkeypatch):
    from hailiang_skills.workbench import catalog
    from hailiang_skills.harness import assets
    from hailiang_skills.skill_runtime import tools
    bundle = SimpleNamespace(runtime_metadata=SimpleNamespace(name='候选能力'), skill_markdown='规则',
        root_dir=tmp_path, metadata={}, tool_registry=ToolRegistry(), contract=SkillContract('internal'))
    skills = SimpleNamespace(get=lambda key: bundle if key == 'internal' else None)
    expert = SimpleNamespace(authorized_skill_ids=['internal'])
    live_experts = SimpleNamespace(definitions={'live': expert})
    empty_experts = SimpleNamespace(definitions={}, get=lambda _: None)
    teams = SimpleNamespace(get=lambda _: None)
    monkeypatch.setattr(catalog, 'build_runtime_registries', lambda _: (skills, empty_experts, teams))
    monkeypatch.setattr(policy, 'load_settings', lambda: {'independent_skill_ids': []})
    monkeypatch.setattr(policy, 'build_tool_specs', lambda *_: [])
    monkeypatch.setattr(tools, '_is_capability_enabled', lambda *_: False)
    monkeypatch.setattr(assets, 'snapshot_assets', lambda _: {'digest': 'fixture'})
    config = LLMConfig('openai', 'https://model.example/v1', 'frozen-model', 'TEST_HARNESS_CREDENTIAL', 'never-persist', 30, 0, 2048)
    orchestrator = SimpleNamespace(runtime_registry=skills, expert_registry=live_experts, expert_team_registry=teams,
        _configured_bundle=lambda _, bundle: bundle, _soul_context=lambda _: {},
        _runtime_client_for_context=lambda _: SimpleNamespace(_config=config))
    context = SessionContext(session_id='s', user_id='u', session_meta={'workbench_debug_session_id': 'debug',
        'configuration_snapshot': {'entries': [{}], 'root': {'object_type': 'skill', 'object_key': 'internal'}}})
    frozen = policy.capture_policy(orchestrator, context)
    assert frozen['independent_skills'] == []
    assert frozen['experts']['harness_candidate_skill']['skills'] == ['internal']
    with pytest.raises(HarnessError):
        policy.check_skill({'policy': frozen}, {'kind': 'main'}, 'internal')
    assert policy.check_skill({'policy': frozen}, {'kind': 'expert', 'expert_id': 'harness_candidate_skill'}, 'internal') == 'internal'
    assert 'api_key' not in frozen['model_config']
    config.model = 'changed-live-model'
    monkeypatch.setenv('TEST_HARNESS_CREDENTIAL', 'current-secret')
    client = HarnessIntegration(orchestrator).client({'policy': frozen})
    assert client._config.model == 'frozen-model'
    assert client._config.api_key == 'current-secret'
