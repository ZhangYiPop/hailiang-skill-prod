import json

import pytest

from hailiang_skills.harness import context_policy as cp
from hailiang_skills.harness.contracts import HarnessError
from hailiang_skills.skill_runtime.models import AssistantTurnResult
from test_harness import make_runtime, advance, call, postgres_test_backend  # noqa: F401
from hailiang_skills.harness.runtime import task_node
from hailiang_skills.harness.context import compose
from test_harness_context_v2 import v2_policy, setup_run


def test_external_subject_excludes_profile_at_runtime_and_projection(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    run['goal'] = '替别人询问'
    tool = {'name': 'set_task_subject', 'arguments': {'mode': 'external', 'evidence': '替别人'}, 'id': 'subject'}
    runtime._internal(run, root, tool, [], None)
    assert root['brief']['subject'] == {'mode': 'external', 'profile_id': None}
    run['seed_facts'] = {
        'profile': {'key': 'grade', 'scope': 'profile', 'value': '初中', 'status': 'confirmed', 'version': 1},
        'local': {'key': 'grade', 'scope': 'task', 'value': '高三', 'status': 'confirmed', 'version': 1}}
    assert set(runtime.facts(run, root)) == {'local'}
    selected, _ = cp.project_facts(run, root, run['seed_facts'])
    assert set(selected) == {'local'}
    with pytest.raises(HarnessError, match='EXTERNAL_SUBJECT_REQUIRES_TASK_SCOPE'):
        runtime._internal(run, root, {'name': 'propose_fact_updates', 'arguments': {'updates': [
            {'key': 'grade', 'value': '高三', 'scope': 'profile', 'status': 'user_statement', 'evidence': '高三'}]}}, [], None)
    runtime.close()


def test_subject_change_requires_user_evidence_and_precedes_delegation(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    with pytest.raises(HarnessError, match='SUBJECT_EVIDENCE_REQUIRED'):
        runtime._internal(run, root, {'name': 'set_task_subject', 'arguments': {'mode': 'external', 'evidence': '模型猜测'}}, [], None)
    run['tasks']['child'] = {'parent_id': root['id']}
    with pytest.raises(HarnessError, match='SUBJECT_CHANGE_REQUIRES_NEW_RUN'):
        runtime._internal(run, root, {'name': 'set_task_subject', 'arguments': {'mode': 'external', 'evidence': run['goal']}}, [], None)
    runtime.close()


def test_escaped_evidence_envelope_has_bound_and_lossless_continuation(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    run['policy']['settings']['max_tool_evidence_chars'] = 1024
    eid = cp.remember_evidence(run, root, '\\"\n' * 3000, source='test', version=1)
    content, offset = '', 0
    while offset is not None:
        result = cp.read_evidence(run, root, {'evidence_id': eid, 'offset': offset, 'limit': 8000})
        assert len(json.dumps(result, ensure_ascii=False)) <= 1024
        assert result['content']
        content += result['content']
        offset = result['next_offset']
    assert content == run['evidence'][eid]['content']
    runtime.close()


def test_plain_question_cannot_silently_complete_expert_or_parent(tmp_path):
    def respond(messages, specs, purpose):
        if purpose == 'harness_main':
            if any(m.role == 'tool' for m in messages):
                return AssistantTurnResult(final_text='未经验证的推荐')
            return call('delegate_tasks', {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': '分析'}]})
        return AssistantTurnResult(final_text='请问孩子几年级？')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '分析', v2_policy())
    advance(runtime, rid, 16)
    run = runtime.store.get(rid)
    expert = next(t for t in run['tasks'].values() if t['kind'] == 'expert')
    assert expert['error'] == 'RESULT_PROTOCOL_REQUIRED'
    assert expert['model_calls'] == 2
    assert run['status'] == 'failed'
    assert run['error'] == 'REQUIRED_TASK_FAILED'
    assert '未经验证' not in run['result']
    assert not run['inputs']  # No fabricated request from model failure.
    runtime.close()


def test_length_finish_is_not_completion_and_usage_is_persisted(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='截断正文'))
    class Client:
        def complete_with_tools(self, *args, **kwargs):
            return AssistantTurnResult(final_text='截断正文')
        def last_request_metrics(self):
            return {'finish_reason': 'length', 'input_tokens': 123, 'output_tokens': 8192}
    runtime.client_factory = lambda _: Client()
    rid = runtime.create('s', 'u', 'p', '分析', v2_policy())
    advance(runtime, rid, 2)
    run = runtime.store.get(rid)
    assert run['status'] == 'failed'
    assert run['error'] == 'MODEL_OUTPUT_TRUNCATED'
    assert run['input_tokens_actual'] == 123
    assert not run.get('result')
    runtime.close()


def test_synthesis_tools_are_narrow_and_child_question_status_is_visible(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    child = task_node('expert', '分析', root['id'], expert_id='a')
    child['status'] = 'completed'
    run['tasks'][child['id']] = child
    run['inputs']['answered'] = {'id': 'answered', 'status': 'answered', 'round': 1,
        'fields': [{'key': 'grade'}], 'bindings': [{'task_id': child['id'], 'tool_id': 'q'}]}
    specs = runtime.specs(run, root)
    assert 'delegate_tasks' not in {s.name for s in specs}
    assert 'execute_skill' not in {s.name for s in specs}
    assert 'search_skill_assets' not in {s.name for s in specs}
    messages, diagnostics = compose(run, root, {}, specs)
    assert diagnostics['phase'] == 'synthesis'
    assert 'answered' in messages[1].content
    child['status'] = 'queued'
    assert cp.task_phase(run, root) == 'planning'
    runtime.close()
