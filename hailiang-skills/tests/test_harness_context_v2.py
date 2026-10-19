from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from hailiang_skills.harness import context_policy as cp
from hailiang_skills.harness.context import compose
from hailiang_skills.harness.contracts import AnswerSubmission, HarnessError
from hailiang_skills.harness.runtime import task_node
from hailiang_skills.skill_runtime.models import AssistantTurnResult
from hailiang_skills.skill_runtime.llm_client import _extract_usage_payload
from test_harness import policy, make_runtime, advance, call, QUESTION
from test_harness import postgres_test_backend  # noqa: F401 -- run the same suite on PostgreSQL


def v2_policy():
    p = policy()
    p['settings']['context_policy_version'] = 'v2'
    p['skills']['internal']['fact_keys'] = ['score']
    return p


def test_schema_compaction_preserves_named_title_and_validation():
    schema = {'title': 'Form', 'type': 'object', 'required': ['title'],
              'properties': {'title': {'title': 'Title', 'type': 'string', 'minLength': 1},
                             'default': {'type': 'boolean', 'default': False}}}
    compact = cp.compact_schema(schema)
    assert compact['required'] == ['title']
    assert compact['properties']['title'] == {'type': 'string', 'minLength': 1}
    assert compact['properties']['default'] == {'type': 'boolean'}
    assert schema['title'] == 'Form'


def test_current_fact_projection_replaces_old_read_without_mutating_checkpoint(tmp_path):
    runtime, run, task = setup_run(tmp_path)
    task['read_fact_keys'] = ['score']
    original = {'role': 'tool', 'name': 'read_facts', 'tool_call_id': 'read-1',
                'content': json.dumps({'score-id': {'key': 'score', 'value': 60, 'version': 1}})}
    task['messages'] = [deepcopy(original)]
    facts = {'score-id': {'key': 'score', 'value': 80, 'status': 'confirmed', 'version': 2}}
    messages, diagnostics = compose(run, task, facts, [])
    assert json.loads(messages[-1].content) == {'facts_ref': ['score-id'], 'location': 'current_facts'}
    assert diagnostics['fact_versions'] == {'score-id': 2}
    assert task['messages'] == [original]
    runtime.close()


def setup_run(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='结论'))
    rid = runtime.create('s', 'u', 'p', '仅比较方案，不推荐学校', v2_policy())
    run = runtime.store.get(rid)
    return runtime, run, run['tasks'][run['root_task_id']]


def test_facts_are_scoped_and_dynamic_data_is_not_system(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    task = task_node('skill', '比较方案', root['id'], skill_id='internal', expert_id='a')
    run['tasks'][task['id']] = task
    task['brief'] = cp.brief_for(run, task)
    facts = {'score_id': {'key': 'score', 'value': 80, 'status': 'confirmed', 'version': 2, 'source': {'huge': 'a' * 10000}},
             'other': {'key': 'unrelated', 'value': 'SECRET_UNRELATED', 'status': 'confirmed', 'version': 1}}
    messages, diag = compose(run, task, facts, runtime.specs(run, task))
    text = json.dumps([asdict(m) for m in messages], ensure_ascii=False)
    assert 'SECRET_UNRELATED' not in text
    assert '仅比较方案，不推荐学校' in messages[1].content
    assert 'score_id' not in messages[0].content
    assert diag['fact_versions'] == {'score_id': 2}
    assert set(diag['components']) == {'rules', 'task', 'facts', 'questions', 'summary', 'history', 'tool_evidence', 'tool_schemas'}
    facts['score_id'].update(status='expired', value=None, version=3)
    _, after = compose(run, task, facts, [])
    assert after['fact_versions']['score_id'] == 3
    runtime.close()


def test_version_is_frozen_and_old_runs_keep_legacy(tmp_path):
    runtime, run, task = setup_run(tmp_path)
    run['policy']['settings']['context_policy_version'] = 'v1'
    assert cp.enabled(run)
    run.pop('context_policy_version')
    assert not cp.enabled(run)
    assert 'read_evidence' not in {s.name for s in runtime.specs(run, task)}
    messages, _ = compose(run, task, {}, [])
    assert messages[1].content == task['goal']
    runtime.close()


def test_structured_submission_resumes_parent_without_extra_expert_call(tmp_path):
    seen = []
    def respond(messages, specs, purpose):
        seen.append(purpose)
        if purpose == 'harness_main':
            if not any(m.role == 'tool' for m in messages):
                return call('delegate_tasks', {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': '专项'}]})
            assert '限制' in ''.join(m.content for m in messages)
            return AssistantTurnResult(final_text='有条件的汇总')
        return call('submit_result', {'status': 'partial', 'conclusion': '结论', 'limitations': ['限制'], 'unresolved': ['缺少成绩']})
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '目标', v2_policy())
    advance(runtime, rid, 10)
    run = runtime.store.get(rid)
    assert run['status'] == 'completed'
    expert = next(t for t in run['tasks'].values() if t['kind'] == 'expert')
    assert expert['structured_result']['status'] == 'partial'
    assert seen.count('harness_expert') == 1
    runtime.close()


def test_submit_result_cannot_skip_pending_tools(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    child = task_node('expert', 'goal', root['id'], expert_id='a')
    child['pending_calls'] = ['one', 'two']
    with pytest.raises(HarnessError, match='RESULT_DEPENDENCIES_UNRESOLVED'):
        runtime._internal(run, child, {'name': 'submit_result', 'arguments': {'status': 'completed', 'conclusion': 'bad'}}, [], None)
    runtime.close()


def test_evidence_grants_explicit_and_results_keep_every_expert(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    children = [task_node('expert', 'goal', root['id'], expert_id=e) for e in ['a', 'b']]
    for child in children:
        run['tasks'][child['id']] = child
        child.update(status='completed', result='长结论' * 2000, structured_result=cp.TaskResult(status='partial', conclusion='长结论' * 2000, limitations=['不得承诺录取'], findings=['成绩80']).model_dump())
    first = cp.result_for(run, children[0], root)
    eid = first['evidence_refs'][0]
    with pytest.raises(HarnessError, match='EVIDENCE_NOT_AUTHORIZED'):
        cp.read_evidence(run, children[1], {'evidence_id': eid})
    assert '长结论' in cp.read_evidence(run, root, {'evidence_id': eid})['content']
    assert cp.result_for(run, children[1], root)['result']['limitations'] == ['不得承诺录取']
    assert first['result']['findings'] == ['成绩80']
    alien = deepcopy(run)
    alien['evidence'] = {}
    with pytest.raises(HarnessError):
        cp.read_evidence(alien, root, {'evidence_id': eid})
    runtime.close()


def test_task_brief_cannot_forge_permissions_or_evidence(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    child = task_node('expert', 'goal', root['id'], expert_id='a')
    with pytest.raises(Exception):
        cp.brief_for(run, child, {'permissions': ['all']})
    with pytest.raises(HarnessError):
        cp.brief_for(run, child, {'evidence_refs': ['foreign']})
    runtime.close()


def test_evidence_version_never_injects_configuration_and_old_checkpoint_is_safe(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    snapshot = {'entries': [{'body': 'PRIVATE_CONFIGURATION' * 15000}]}
    run['policy']['snapshot'] = snapshot
    version = {'snapshot': snapshot, 'facts': {'score': 2}}
    eid = cp.remember_evidence(run, root, '资料' * 5000, source='legacy', version=version)
    result = cp.read_evidence(run, root, {'evidence_id': eid, 'limit': 4000})
    assert len(json.dumps(result, ensure_ascii=False)) < 4500
    assert len(result['version']['sha256']) == 64
    assert cp.evidence_version_ref(version) != cp.evidence_version_ref({**version, 'facts': {'score': 3}})
    old_result = {**result, 'version': version}
    root['messages'] = [{'role': 'tool', 'name': 'read_evidence', 'tool_call_id': 'old-read', 'content': json.dumps(old_result)}]
    messages, diag = compose(run, root, {}, [])
    assert 'PRIVATE_CONFIGURATION' not in messages[-1].content
    assert diag['estimated_tokens'] < 10000
    assert 'PRIVATE_CONFIGURATION' in root['messages'][0]['content']
    _, metadata = cp.tool_projection(run, root, {'id': 'new', 'name': 'search_skill_assets', 'arguments': {'query': 'q'}, 'status': 'completed', 'result': 'data'})
    stored = run['evidence'][metadata['evidence_id']]['version']
    assert len(json.dumps(stored)) < 200
    assert run['evidence'][eid]['version'] == version
    runtime.close()


def test_invalid_second_brief_does_not_publish_first_child(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    args = {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': 'valid'},
                      {'key': 'b', 'expert_id': 'b', 'goal': 'invalid', 'brief': {'evidence_refs': ['not-granted']}}]}
    with pytest.raises(HarnessError):
        runtime._internal(run, root, {'name': 'delegate_tasks', 'arguments': args}, [], None)
    assert len(run['tasks']) == 1
    runtime.close()


def test_tool_dedup_respects_version_status_and_owner(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    tool = {'id': 't1', 'name': 'search_skill_assets', 'arguments': {'query': 'test'}, 'result': 'data', 'status': 'completed'}
    content, first = cp.tool_projection(run, root, tool)
    exposed = json.loads(content)
    assert exposed['evidence_id'] == first['evidence_id']
    assert exposed['data'] == 'data'
    assert cp.read_evidence(run, root, {'evidence_id': exposed['evidence_id']})['content'] == '"data"'
    _, second = cp.tool_projection(run, root, {**tool, 'id': 't2'})
    assert second['duplicate'] and first['evidence_id'] == second['evidence_id']
    root['fact_versions'] = {'score': 2}
    _, changed = cp.tool_projection(run, root, tool)
    assert not changed['duplicate']
    _, failure = cp.tool_projection(run, root, {**tool, 'status': 'failed'})
    assert failure['evidence_id'] != changed['evidence_id']
    _, web1 = cp.tool_projection(run, root, {**tool, 'name': 'web_search'})
    _, web2 = cp.tool_projection(run, root, {**tool, 'id': 't2', 'name': 'web_search'})
    assert web1['evidence_id'] != web2['evidence_id']
    runtime.close()


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_explicit_zero_disables_only_cumulative_input_guard(tmp_path, version):
    runtime, run, root = setup_run(tmp_path)
    run['context_policy_version'] = version
    settings = run['policy']['settings']
    settings['max_run_input_tokens'] = 0
    before = deepcopy(settings)
    for _ in range(10):
        runtime._reserve_model_input(run, root, {'estimated_tokens': 40000})
    assert run['input_tokens_estimated'] == 400000
    if version == 'v2':
        assert run['input_tokens_charged'] == 400000
    assert settings == before  # Per-request and invocation limits are untouched.
    runtime.close()


def test_main_role_preserves_delegation_and_tool_question_contract(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    messages, _ = compose(run, root, {}, [])
    assert '专业任务优先分派给专家' in messages[0].content
    assert 'request_user_input' in messages[0].content
    assert '不能以普通回复中的问题替代追问工具' in messages[0].content
    runtime.close()


def test_budget_actual_usage_corrects_reservations_once(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    d = {'estimated_tokens': 100}
    runtime._reserve_model_input(run, root, d)
    runtime._reserve_model_input(run, root, d)
    trace = {'diagnostics': d}
    runtime._record_model_usage(run, trace, {'input_tokens': 80, 'prompt_cache_hit_tokens': 60})
    assert run['input_tokens_charged'] == 180
    runtime._record_model_usage(run, trace, {'input_tokens': 80})
    assert run['input_tokens_charged'] == 180
    assert run['input_tokens_actual'] == 80
    assert trace['measurement_source'] == 'provider'
    assert _extract_usage_payload({'usage': {'prompt_tokens': 80, 'prompt_cache_hit_tokens': 60}})['prompt_cache_hit_tokens'] == 60
    runtime.close()


def test_fact_read_incremental_next_prompt_self_contained(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    identity = runtime.store.fact_identity(run, root, 'session', 'score')
    with runtime.store.transaction() as db:
        runtime.store.write_fact(db, identity, {'key': 'score', 'scope': 'session', 'value': 70, 'status': 'confirmed'})
        tool = {'id': 't', 'name': 'read_facts', 'arguments': {'keys': ['score'], 'known_versions': {identity: 1}}}
        runtime._internal(run, root, tool, [], db)
    assert tool['result'] == {}
    messages, d = compose(run, root, runtime.facts(run, root), [])
    assert '70' in messages[1].content
    assert d['fact_versions'][identity] == 1
    runtime.close()


def test_shared_skip_reuse_but_distinct_time_does_not(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    a, b = [task_node('expert', 'goal', root['id'], expert_id=e) for e in ['a', 'b']]
    run['tasks'].update({a['id']: a, b['id']: b})
    with runtime.store.transaction() as db:
        first = {'id': 't1', 'call_id': 'c1', 'name': 'request_user_input', 'arguments': deepcopy(QUESTION)}
        runtime._request(run, a, first, [], db)
        request = next(iter(run['inputs'].values()))
        request.update(status='skipped', answer={'skipped': True, 'answers': {}})
        second = {'id': 't2', 'call_id': 'c2', 'name': 'request_user_input', 'arguments': deepcopy(QUESTION)}
        runtime._request(run, b, second, [], db)
        assert second['status'] == 'skipped'
        different = deepcopy(second)
        different['id'] = 't3'
        different['arguments']['fields'][0]['applicable_time'] = '2027学年'
        runtime._request(run, b, different, [], db)
        assert different['status'] == 'waiting_for_user'
    runtime.close()


def test_complete_tool_groups_not_split():
    messages = [{'role': 'assistant', 'tool_calls': [{'id': 'a'}, {'id': 'b'}]},
                {'role': 'tool', 'tool_call_id': 'a'}, {'role': 'tool', 'tool_call_id': 'b'}, {'role': 'user', 'content': '更正'}]
    assert list(map(len, cp.message_groups(messages))) == [3, 1]


def test_protected_context_overflow_explicit(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    run['policy']['settings']['context_tokens'] = 100
    with pytest.raises(HarnessError, match='PROTECTED_CONTEXT_BUDGET_EXCEEDED'):
        runtime._prepare_context_v2(run['run_id'], root['id'], '', run, root, {}, [])
    runtime.close()


def test_summary_failure_keeps_checkpoint_and_does_not_ask_user(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    def seed(current, changes, db):
        task = current['tasks'][root['id']]
        current['policy']['settings']['context_tokens'] = 12000
        task['messages'] += [{'role': 'assistant', 'content': '历史分析' * 700} for _ in range(4)]
        task['messages'] += [{'role': 'assistant', 'content': '近期消息'} for _ in range(4)]
        changes.append({'event': 'fixture_seeded', 'node_id': task['id']})
    runtime.store.mutate(run['run_id'], seed)
    class BrokenSummary:
        def complete(self, *_args, **_kwargs):
            raise RuntimeError('summary unavailable')
    runtime.client_factory = lambda _: BrokenSummary()
    original = runtime.store.get(run['run_id'])['tasks'][root['id']]['messages']
    advance(runtime, run['run_id'])
    saved = runtime.store.get(run['run_id'])
    assert saved['tasks'][root['id']]['messages'] == original
    assert saved['tasks'][root['id']]['status'] == 'queued'
    assert not saved['inputs']
    assert saved['tasks'][root['id']]['summary_failed_at']
    runtime.close()


def test_summary_archives_only_complete_old_groups_and_keeps_corrections(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    def seed(current, changes, db):
        task = current['tasks'][root['id']]
        current['policy']['settings']['context_tokens'] = 12000
        task['messages'] += [{'role': 'assistant', 'content': '旧分析' * 1000} for _ in range(4)]
        task['messages'] += [{'role': 'user', 'content': '更正：分数是80，不是90'}]
        task['messages'] += [{'role': 'assistant', 'content': '近期消息'} for _ in range(4)]
        changes.append({'event': 'fixture_seeded', 'node_id': task['id']})
    runtime.store.mutate(run['run_id'], seed)
    class Summary:
        def complete(self, messages, **kwargs):
            assert len(messages) == 2
            assert '# 当前事实' not in messages[0].content
            return '既往分析摘要，保留来源。'
    runtime.client_factory = lambda _: Summary()
    advance(runtime, run['run_id'])
    saved = runtime.store.get(run['run_id'])['tasks'][root['id']]
    assert saved['summary']
    assert saved['archived_messages']
    assert any('更正' in m['content'] for m in saved['messages'])
    assert saved['compactions'][-1]['after'] < saved['compactions'][-1]['before']
    runtime.close()


def test_unrelated_fact_update_does_not_discard_model_response(tmp_path):
    runtime, run, root = setup_run(tmp_path)
    def responder(messages, specs, purpose):
        def update(current, changes, db):
            identity = runtime.store.fact_identity(current, current['tasks'][root['id']], 'profile', 'unrelated')
            runtime.store.write_fact(db, identity, {'key': 'unrelated', 'value': 'not relevant', 'status': 'confirmed'})
        runtime.store.mutate(run['run_id'], update)
        return AssistantTurnResult(final_text='完成')
    class Client:
        complete_with_tools = staticmethod(lambda messages, specs, request_purpose, max_tokens=None: responder(messages, specs, request_purpose))
    runtime.client_factory = lambda _: Client()
    advance(runtime, run['run_id'])
    assert runtime.store.get(run['run_id'])['status'] == 'completed'
    runtime.close()


def test_changed_fact_invalidates_old_evidence(tmp_path):
    runtime, run, task = setup_run(tmp_path)
    eid = cp.remember_evidence(run, task, 'old', source='task_result', version={'score': 1})
    runtime._invalidate_fact_version(run, 'score', 2, [])
    with pytest.raises(HarnessError, match='EVIDENCE_SUPERSEDED'):
        cp.read_evidence(run, task, {'evidence_id': eid})
    runtime.close()


def test_concurrent_budget_reservation_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    runtime, run, root = setup_run(tmp_path)
    def configure(current, changes, db):
        current['policy']['settings']['max_run_input_tokens'] = 100
        changes.append({'event': 'fixture_configured', 'node_id': root['id']})
    runtime.store.mutate(run['run_id'], configure)
    def reserve(_):
        def apply(current, changes, db):
            runtime._reserve_model_input(current, current['tasks'][root['id']], {'estimated_tokens': 60})
            changes.append({'event': 'model_started', 'node_id': root['id']})
        try:
            runtime.store.mutate(run['run_id'], apply)
            return True
        except HarnessError as exc:
            assert exc.code == 'RUN_INPUT_BUDGET_EXCEEDED'
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == [False, True]
    assert runtime.store.get(run['run_id'])['input_tokens_charged'] == 60
    runtime.close()


def test_parallel_shared_request_answers_resume_each_binding_once(tmp_path):
    def respond(messages, specs, purpose):
        if purpose == 'harness_main':
            if any(m.role == 'tool' for m in messages):
                return AssistantTurnResult(final_text='汇总')
            return call('delegate_tasks', {'tasks': [{'key': e, 'expert_id': e, 'goal': e} for e in ['a', 'b']]})
        if any(m.role == 'tool' for m in messages):
            return call('submit_result', {'status': 'completed', 'conclusion': '已分析'}, 'finish')
        return call('request_user_input', QUESTION)
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '分析', v2_policy())
    advance(runtime, rid, 12)
    run = runtime.store.get(rid)
    assert len(run['inputs']) == 1
    request = next(iter(run['inputs'].values()))
    assert len(request['bindings']) == 2
    submission = AnswerSubmission(user_id='u', profile_id='p', submission_id='answer', expected_version=1, text='高二')
    runtime.answer(request['id'], submission)
    runtime.answer(request['id'], submission)
    # Reconstruct the runtime from persisted state, including evidence and version.
    from hailiang_skills.harness.runtime import HarnessRuntime
    resumed = HarnessRuntime(runtime.store, runtime.client_factory)
    advance(resumed, rid, 12)
    assert resumed.store.get(rid)['status'] == 'completed'
    assert len(resumed.store.get(rid)['inputs']) == 1
    resumed.close()
    runtime.close()


def test_partial_submission_and_shared_skipped_field(tmp_path):
    from hailiang_skills.harness.contracts import InputField, validate_answers, AnswerSubmission
    fields = [InputField(key='grade', label='年级', scope='profile').model_dump(), InputField(key='province', label='省份', scope='profile').model_dump()]
    answer = validate_answers(fields, AnswerSubmission(user_id='u', submission_id='partial', expected_version=1, answers={'grade': '高二', 'province': '  '}))
    assert answer == {'answers': {'grade': '高二'}, 'skipped': False, 'skipped_fields': ['province']}
    assert validate_answers(fields, AnswerSubmission(user_id='u', submission_id='empty', expected_version=1))['skipped']
    number = InputField(key='score', label='成绩', input_type='integer').model_dump()
    assert validate_answers([number], AnswerSubmission(user_id='u', submission_id='zero', expected_version=1, answers={'score': 0}))['answers'] == {'score': 0}
    with pytest.raises(HarnessError, match='INVALID_NUMBER'):
        validate_answers([number], AnswerSubmission(user_id='u', submission_id='bad', expected_version=1, answers={'score': 'bad'}))
    runtime, run, root = setup_run(tmp_path)
    a, b = [task_node('expert', 'goal', root['id'], expert_id=e) for e in ['a', 'b']]
    run['tasks'].update({a['id']: a, b['id']: b})
    with runtime.store.transaction() as db:
        first = {'id': 't1', 'call_id': 'c1', 'name': 'request_user_input', 'arguments': {'title': '信息', 'fields': fields}}
        runtime._request(run, a, first, [], db)
        request = next(iter(run['inputs'].values()))
        request.update(status='answered', answer=answer)
        second = {'id': 't2', 'call_id': 'c2', 'name': 'request_user_input', 'arguments': {'title': '省份', 'fields': [fields[1]]}}
        runtime._request(run, b, second, [], db)
        assert second['status'] == 'skipped'
        assert second['result']['skipped_fields'] == ['province']
    runtime.close()


def test_partial_answer_persists_and_resumes_with_skip_metadata(tmp_path):
    from hailiang_skills.harness.contracts import AnswerSubmission
    def respond(messages, specs, purpose):
        if any(m.role == 'tool' for m in messages):
            return AssistantTurnResult(final_text='按已知信息继续，省份本次跳过')
        return call('request_user_input', {'title': '补充', 'fields': [
            {'key': 'grade', 'label': '年级', 'scope': 'profile'},
            {'key': 'province', 'label': '省份', 'scope': 'profile'}]})
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '分析', v2_policy())
    advance(runtime, rid, 4)
    request = next(iter(runtime.store.get(rid)['inputs'].values()))
    submission = AnswerSubmission(user_id='u', profile_id='p', submission_id='partial', expected_version=1, answers={'grade': '高二'})
    runtime.answer(request['id'], submission)
    runtime.answer(request['id'], submission)
    saved = runtime.store.get(rid)
    assert saved['inputs'][request['id']]['answer']['skipped_fields'] == ['province']
    tool = saved['tools'][request['bindings'][0]['tool_id']]
    assert tool['result']['answers'] == {'grade': '高二'}
    assert tool['result']['skipped_fields'] == ['province']
    root = saved['tasks'][saved['root_task_id']]
    facts = runtime.facts(saved, root)
    assert any(f['key'] == 'grade' and f['value'] == '高二' for f in facts.values())
    assert not any(f['key'] == 'province' for f in facts.values())
    advance(runtime, rid, 5)
    assert runtime.store.get(rid)['status'] == 'completed'
    runtime.close()
