import pytest
from test_workbench import service, _actor, _publish_skill
from hailiang_skills.workbench.service import WorkbenchError


def test_publication_rechecks_skill_classification_after_configuration_change(service, monkeypatch):
    import hailiang_skills.harness.policy as policy
    actor_id = _actor(service)
    _, _, skill = _publish_skill(service, actor_id, key='skill_a')
    expert = service.create_object(object_type='expert', object_key='expert_a', name='专家 A', actor_id=actor_id)
    revision = service.save_revision(expert['object_id'], base_revision_id=None,
        payload={'rules_markdown': '专家规则', 'brief': '专家摘要', 'budget': {'max_iters': 4, 'max_skill_calls': 3}},
        dependency_locks=[{'release_id': skill['release_id']}], assets=[], actor_id=actor_id)
    assert revision['validation']['valid']
    debug = service.create_debug_session(revision['revision_id'], baseline_release_id=None, actor_id=actor_id)
    service.complete_debug_session(debug['debug_session_id'], conclusion='通过', actor_id=actor_id)
    monkeypatch.setattr(policy, 'load_settings', lambda: {'independent_skill_ids': ['skill_a']})
    with pytest.raises(WorkbenchError) as error:
        service.publish_revision(revision['revision_id'], evidence_id=debug['debug_session_id'], manual_confirmation=True, confirmation_notes='通过', actor_id=actor_id)
    assert error.value.code == 'REVISION_VALIDATION_FAILED'
