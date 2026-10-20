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


def _team_orchestrator(monkeypatch):
    expert = SimpleNamespace(name='专家 A', brief='分析 A', rules_markdown='规则', authorized_skill_ids=[], max_skill_calls=3)
    experts = SimpleNamespace(definitions={'a': expert}, get=lambda key: expert if key == 'a' else None)
    member = SimpleNamespace(expert_id='a', mention_name='专家 A', routing_brief='分析 A', applicable_stages=())
    team = SimpleNamespace(member_expert_ids=('a',), members=(member,), consultation_policy=None, fallback_expert_id='a',
                           coordinator_expert_id='a', collaboration_rules='')
    teams = SimpleNamespace(get=lambda key: team if key == 'team' else None)
    skills = SimpleNamespace(get=lambda _: None)
    monkeypatch.setattr(policy, 'load_settings', lambda: {'independent_skill_ids': []})
    return SimpleNamespace(runtime_registry=skills, expert_registry=experts, expert_team_registry=teams,
                           _configured_bundle=lambda _, bundle: bundle, _soul_context=lambda _: {},
                           _runtime_client_for_context=lambda _: None)


def _formal_context(**meta):
    snapshot = {'root': {'object_type': 'expert_team', 'object_key': 'team'}, 'entries': [],
                'orchestration_rules': {'head_rules': '编排首规则', 'tail_rules': '编排尾规则'}}
    return SessionContext(session_id='s', user_id='u', session_meta={'expert_team_id': 'team', 'configuration_snapshot': snapshot, **meta})


def test_formal_session_runs_team_orchestration_with_form_gate(monkeypatch):
    monkeypatch.delenv('HAILIANG_HARNESS_FORMAL_ORCHESTRATION', raising=False)
    frozen = policy.capture_policy(_team_orchestrator(monkeypatch), _formal_context())
    assert frozen['orchestrated'] is True
    assert frozen['orchestrator']['enabled'] is True
    assert frozen['identity_gate'] == 'form'
    assert frozen['direct_expert_id'] == ''


def test_formal_session_without_deployment_snapshot_uses_bundled_orchestration_rules(monkeypatch):
    from hailiang_skills.workbench import orchestration
    monkeypatch.setattr(orchestration, 'load_seed_rules', lambda: {
        'head_rules': 'bundled head', 'tail_rules': 'bundled tail',
        'recommendation_rules': 'bundled recommendation', 'recommendations': [],
    })
    context = SessionContext(session_id='s', user_id='u', session_meta={'expert_team_id': 'team'})
    frozen = policy.capture_policy(_team_orchestrator(monkeypatch), context)
    assert frozen['orchestrated'] is True
    assert frozen['snapshot']['source'] == 'filesystem_bootstrap'
    assert frozen['orchestrator']['head_rules'] == 'bundled head'


def test_formal_session_with_default_coordinator_still_runs_team_orchestration(monkeypatch):
    frozen = policy.capture_policy(
        _team_orchestrator(monkeypatch),
        _formal_context(expert_id='a', active_expert_id='a', expert_selection_source='deployment_snapshot'),
    )
    assert frozen['orchestrated'] is True
    assert tuple(frozen['experts']) == ('a',)


def test_formal_session_with_selected_expert_skips_orchestration(monkeypatch):
    frozen = policy.capture_policy(
        _team_orchestrator(monkeypatch),
        _formal_context(expert_id='a', expert_selection_source='manual'),
    )
    assert frozen['orchestrated'] is False


def test_minimal_v3_team_uses_system_rules_when_legacy_snapshot_lacks_them(monkeypatch):
    from hailiang_skills.workbench import orchestration
    monkeypatch.setattr(orchestration, 'load_seed_rules', lambda: {
        'head_rules': 'v3 head', 'tail_rules': 'v3 tail',
        'recommendation_rules': 'v3 recommendation', 'recommendations': [],
    })
    context = _formal_context(harness_v3_minimal=True)
    context.session_meta['configuration_snapshot']['orchestration_rules'] = {}
    frozen = policy.capture_policy(_team_orchestrator(monkeypatch), context)
    assert frozen['orchestrated'] is True
    assert frozen['snapshot']['source'] == 'v3_system_skill_compat'
    assert frozen['orchestrator']['head_rules'] == 'v3 head'


def test_formal_session_without_published_head_rules_skips_orchestration(monkeypatch):
    context = _formal_context()
    context.session_meta['configuration_snapshot']['orchestration_rules'] = {}
    assert policy.capture_policy(_team_orchestrator(monkeypatch), context)['orchestrated'] is False


def test_formal_session_orchestration_switch_can_turn_it_off(monkeypatch):
    monkeypatch.setenv('HAILIANG_HARNESS_FORMAL_ORCHESTRATION', 'false')
    assert policy.capture_policy(_team_orchestrator(monkeypatch), _formal_context())['orchestrated'] is False


def test_experts_disabled_formal_session_skips_orchestration(monkeypatch):
    frozen = policy.capture_policy(_team_orchestrator(monkeypatch), _formal_context(harness_experts_disabled=True))
    assert frozen['orchestrated'] is False


def test_debug_team_session_keeps_text_identity_gate(monkeypatch):
    frozen = policy.capture_policy(_team_orchestrator(monkeypatch), _formal_context(workbench_debug_session_id='debug'))
    assert frozen['orchestrated'] is True
    assert frozen['identity_gate'] == 'text'


def test_debug_single_expert_session_is_not_orchestrated(monkeypatch):
    context = _formal_context(workbench_debug_session_id='debug')
    context.session_meta['configuration_snapshot']['root'] = {'object_type': 'expert', 'object_key': 'a'}
    frozen = policy.capture_policy(_team_orchestrator(monkeypatch), context)
    assert frozen['orchestrated'] is False
    assert frozen['direct_expert_id'] == 'a'
