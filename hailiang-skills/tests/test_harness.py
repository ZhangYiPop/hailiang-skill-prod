from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import json
import pytest
from copy import deepcopy
import threading
from hailiang_skills.harness.contracts import AnswerSubmission, HarnessError
from hailiang_skills.harness.policy import validate_skill_boundary
from hailiang_skills.harness.runtime import HarnessRuntime
from hailiang_skills.harness.store import HarnessStore
from hailiang_skills.skill_runtime.models import AssistantTurnResult, ToolCallRequest
from hailiang_skills.security.models import ModerationBlockedError, ModerationResult, OUTPUT_MODERATION_FALLBACK


def policy():
    return {'experts': {'a': {'name': '专家 A', 'brief': '分析 A', 'rules': '规则', 'skills': ['internal'], 'max_skill_calls': 3},
                        'b': {'name': '专家 B', 'brief': '分析 B', 'rules': '规则', 'skills': [], 'max_skill_calls': 3}},
            'skills': {'internal': {'name': '内部分析', 'rules': '分析任务', 'specs': []}, 'independent': {'name': '独立能力', 'rules': '分析任务', 'specs': []}},
            'independent_skills': ['independent'], 'settings': {'max_parallel': 3, 'max_model_calls': 24, 'max_tool_calls': 48, 'context_tokens': 32000}, 'soul': ''}


def call(name, args, call_id='call_1'):
    return AssistantTurnResult(tool_calls=(ToolCallRequest(call_id, name, args),))


QUESTION = {'title': '请补充', 'fields': [{'key': 'grade', 'label': '年级', 'input_type': 'text', 'scope': 'profile', 'fact_key': 'grade'}]}


def advance(runtime, run_id, count=1):
    for _ in range(count):
        lease = runtime.claim(run_id)
        if lease:
            runtime.execute(run_id, *lease)


def make_runtime(tmp_path, responder):
    class Client:
        def complete_with_tools(self, messages, specs, request_purpose, max_tokens=None):
            return responder(messages, specs, request_purpose)
    return HarnessRuntime(HarnessStore.local(tmp_path / 'harness.sqlite3'), lambda _run: Client())


def test_worker_pool_capacity_is_configurable_and_run_claims_rotate_fairly(monkeypatch, tmp_path):
    monkeypatch.setenv("HAILIANG_HARNESS_WORKERS", "9")
    runtime = make_runtime(tmp_path, lambda *_: call('finalize_response', {}))
    assert runtime.worker_count == 9
    runtime._last_claimed_run_id = "run_b"
    assert runtime._rotated_runnable_runs(["run_a", "run_b", "run_c"]) == ["run_c", "run_a", "run_b"]
    runtime.close()


def test_scheduler_candidates_include_queued_and_expired_not_user_wait(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='完成'))
    queued = runtime.create('queued', 'u', 'p', '分析', policy())
    waiting = runtime.create('waiting', 'u', 'p', '分析', policy())
    expired = runtime.create('expired', 'u', 'p', '分析', policy())
    def set_waiting(run, changes, _db):
        task = run['tasks'][run['root_task_id']]
        task['status'] = 'waiting_for_user'
        changes.append({'event': 'test_waiting', 'node_id': task['id']})
    runtime.store.mutate(waiting, set_waiting)
    task_id, _ = runtime.claim(expired)
    def expire(run, changes, _db):
        run['tasks'][task_id]['lease_until'] = '2000-01-01T00:00:00+00:00'
        changes.append({'event': 'test_expired', 'node_id': task_id})
    runtime.store.mutate(expired, expire)
    assert set(runtime.store.likely_runnable_runs()) == {queued, expired}
    assert waiting in runtime.store.runnable_runs()  # remains visible to the full audit
    assert runtime.claim(expired) is not None
    runtime.close()


def test_scheduler_full_audit_recovers_missing_job_hint(tmp_path):
    import time
    from sqlalchemy import delete
    from hailiang_skills.harness.models import HarnessJobRow
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='完成'))
    rid = runtime.create('s', 'u', 'p', '分析', policy())
    with runtime.store.engine.begin() as db:
        db.execute(delete(HarnessJobRow).where(HarnessJobRow.run_id == rid))
    assert runtime.store.likely_runnable_runs() == []
    runtime.start()
    try:
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            if runtime.store.get(rid)['tasks'][runtime.store.get(rid)['root_task_id']]['attempt']:
                break
            time.sleep(.05)
        assert runtime.store.get(rid)['tasks'][runtime.store.get(rid)['root_task_id']]['attempt'] > 0
    finally:
        runtime.close()


def test_saturated_worker_claim_is_preserved_in_wait_history(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text="完成"))
    run_id = runtime.create('s', 'u', 'p', '分析', policy())
    lease = runtime.claim(run_id, scheduler_context={
        "worker_pool_capacity": 6, "inflight_count": 5, "runnable_run_count": 4,
        "pool_was_saturated_since_last_claim": "2026-10-25T00:00:00+00:00",
    })
    assert lease
    run = runtime.store.get(run_id)
    root = run['tasks'][run['root_task_id']]
    assert root['scheduler_claim']['wait_reason'] == 'global_worker_pool_saturated'
    assert root['state_history'][0]['reason'] == 'global_worker_pool_saturated'
    assert root['execution_attempts'][0]['scheduler_claim']['inflight_count'] == 5
    runtime.close()


def test_read_tools_reuse_exact_result_and_enforce_exploration_limits(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text="完成"))
    run_id = runtime.create('s', 'u', 'p', '分析', policy())
    run = runtime.store.get(run_id)
    task_id = run['root_task_id']
    run['tools'] = {
        'old_read': {'id': 'old_read', 'task_id': task_id, 'name': 'read_skill_asset',
                     'arguments': {'skill_id': 'internal', 'source': 'guide.json', 'offset': 0},
                     'status': 'completed', 'result': {'records': [{'name': '已读'}]}, 'ended_at': '2026-10-25T00:00:00+00:00'},
        'old_non_read': {'id': 'old_non_read', 'task_id': task_id, 'name': 'submit_findings',
                         'arguments': {}, 'status': 'completed', 'result': {}, 'ended_at': '2026-10-25T00:00:01+00:00'},
    }
    same = {'id': 'new_read', 'task_id': task_id, 'name': 'read_skill_asset',
            'arguments': {'skill_id': 'internal', 'source': 'guide.json', 'offset': 0}}
    assert runtime._reusable_read_result(run, task_id, same)['id'] == 'old_read'
    assert runtime._reusable_read_result(run, task_id, {**same, 'name': 'request_user_input'}) is None
    run['policy']['settings'].update(max_asset_reads_per_source=1, max_consecutive_research_turns=4)
    different_page = {**same, 'arguments': {'skill_id': 'internal', 'source': 'guide.json', 'offset': 20}}
    assert runtime._read_budget_error(run, task_id, different_page) == 'ASSET_READ_BUDGET_EXCEEDED'
    run['policy']['settings'].update(max_asset_reads_per_source=4, max_consecutive_research_turns=1)
    run['tools'].pop('old_non_read')
    assert runtime._read_budget_error(run, task_id, different_page) == 'RESEARCH_TURN_LIMIT_REACHED'
    runtime.close()


def test_expert_parallel_wait_and_multi_round_resume(tmp_path):
    rounds = {'a': 0}
    def respond(messages, specs, purpose):
        if purpose == 'harness_main':
            if not any(m.role == 'tool' for m in messages):
                return call('delegate_tasks', {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': '任务 A'}, {'key': 'b', 'expert_id': 'b', 'goal': '任务 B'}]})
            return AssistantTurnResult(final_text='主 agent 汇总')
        if '任务 B' == messages[1].content:
            return AssistantTurnResult(final_text='B 内部结论')
        if not any(m.role == 'tool' for m in messages):
            return call('request_user_input', QUESTION)
        if rounds['a'] == 0:
            rounds['a'] += 1
            return call('request_user_input', {'title': '第二轮', 'fields': [{'key': 'goal', 'label': '目标'}]}, 'call_2')
        return AssistantTurnResult(final_text='A 内部结论')
    runtime = make_runtime(tmp_path, respond)
    run_id = runtime.create('s', 'u', 'p', '规划', policy())
    advance(runtime, run_id, 8)
    run = runtime.store.get(run_id)
    tasks = [t for t in run['tasks'].values() if t['kind'] == 'expert']
    assert {t['status'] for t in tasks} == {'waiting_for_user', 'completed'}
    assert 'B 内部结论' not in json.dumps(runtime.public(run), ensure_ascii=False)
    product_tree = runtime.public(run, include_internal_ids=False)
    assert product_tree['dispatch']['status'] == 'completed'
    assert product_tree['dispatch']['planned_expert_count'] == 2
    expert_nodes = [node for node in product_tree['nodes'] if node['kind'] == 'expert']
    completed_expert = next(node for node in expert_nodes if node['status'] == 'completed')
    assert completed_expert['agent_brief'] == '分析 B'
    assert completed_expert['result']['conclusion'] == 'B 内部结论'
    assert completed_expert['id'].startswith('node_')
    assert completed_expert['parent_id'] == 'node_1'
    request = next(iter(run['inputs'].values()))
    submission = AnswerSubmission(user_id='u', profile_id='p', submission_id='answer1', expected_version=1, text='高二')
    runtime.answer(request['id'], submission)
    assert runtime.answer(request['id'], submission)['status'] == 'answered'
    advance(runtime, run_id, 5)
    pending = next(q for q in runtime.store.get(run_id)['inputs'].values() if q['status'] == 'pending')
    assert pending['round'] == 2
    runtime.answer(pending['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='answer2', expected_version=1, skipped=True))
    advance(runtime, run_id, 12)
    final = runtime.store.get(run_id)
    assert final['status'] == 'completed', final
    assert final['result'] == '主 agent 汇总'
    assert len(final['inputs']) == 2
    runtime.close()


def test_unmatched_reply_is_delegated_to_fallback(tmp_path):
    def respond(messages, _specs, purpose):
        if purpose == 'harness_expert':
            return AssistantTurnResult(final_text='你好。下一步：告诉我你的年级。')
        if any(m.role == 'tool' for m in messages):
            return AssistantTurnResult(final_text='你好。下一步：告诉我你的年级。')
        return AssistantTurnResult(final_text='我是e生涯规划助手')
    frozen = policy()
    frozen['harness_fallback_expert_id'] = 'b'
    runtime = make_runtime(tmp_path, respond)
    run_id = runtime.create('s', 'u', 'p', '你好', frozen)
    advance(runtime, run_id, 8)
    final = runtime.store.get(run_id)
    experts = [task for task in final['tasks'].values() if task['kind'] == 'expert']
    assert final['status'] == 'completed', final.get('error')
    assert len(experts) == 1
    assert experts[0]['expert_id'] == 'b'
    assert experts[0]['status'] == 'completed'
    assert '我是e生涯规划助手' not in (final.get('result') or '')
    runtime.close()


def orchestrated_policy():
    frozen = policy()
    frozen['orchestrated'] = True
    frozen['harness_fallback_expert_id'] = 'b'
    frozen['soul'] = '安全约束'
    frozen['orchestrator'] = {
        'enabled': True, 'head_rules': '编排首规则', 'tail_rules': '编排尾规则', 'collaboration_rules': '',
        'recommendation_rules': '', 'recommend_catalog': None, 'recommendations': [],
        'consultation_policy': {'max_subtasks': 2, 'max_parallel': 2, 'max_depth': 1}, 'fallback_expert_id': 'b',
        'members': [{'expert_id': 'a', 'mention_name': '专家 A', 'routing_brief': '分析 A', 'applicable_stages': []},
                    {'expert_id': 'b', 'mention_name': '专家 B', 'routing_brief': '分析 B', 'applicable_stages': []}],
    }
    return frozen


def identity_seed():
    return {f'seed:{key}': {'key': key, 'value': value, 'scope': 'profile', 'status': 'confirmed', 'version': 1, 'source': {}}
            for key, value in (('user_role', '家长'), ('school_stage', '高中'))}


def run_orchestrated(tmp_path, head_json, expert_text='专家 B 原文', tail_text='出站正文'):
    seen = {'purposes': []}
    class Client:
        def complete(self, messages, request_purpose='', max_tokens=None, **_):
            seen['purposes'].append(request_purpose)
            return head_json if request_purpose == 'orchestrator_head' else tail_text
        def complete_with_tools(self, messages, specs, request_purpose, max_tokens=None):
            seen['purposes'].append(request_purpose)
            return AssistantTurnResult(final_text=expert_text)
    runtime = HarnessRuntime(HarnessStore.local(tmp_path / 'harness.sqlite3'), lambda _run: Client())
    run_id = runtime.create('s', 'u', 'p', '你好', orchestrated_policy(), identity_seed())
    advance(runtime, run_id, 10)
    return runtime, runtime.store.get(run_id), seen


def test_debug_step_io_exposes_head_expert_and_tail(tmp_path):
    from hailiang_skills.harness.runtime import debug_step_io
    head = json.dumps({'action': 'consult', 'subtasks': [{'subtask_id': 'st_1', 'question': '你好', 'target_expert_id': 'b', 'priority': 1}], 'reason': '寒暄'})
    runtime, run, _seen = run_orchestrated(tmp_path, head)
    public = runtime.public(run)
    assert '编排首规则' not in json.dumps(public, ensure_ascii=False)
    steps = debug_step_io(run)
    by_title = {node['title']: node['id'] for node in public['nodes']}
    head_step = steps[by_title['编排首：选专家']]
    assert head_step['output']['subtasks'][0]['target_expert_id'] == 'b'
    assert any('编排首规则' in str(message.get('content') or '') for message in head_step['input'])
    assert run['root_task_id'] not in steps
    expert = next(task for task in run['tasks'].values() if task['kind'] == 'expert')
    dispatch = steps[by_title['分派专家任务']]
    assert dispatch['input']['subtasks'][0]['target_expert_id'] == 'b'
    assert dispatch['output'] == {'tasks': [{'expert_id': 'b', 'question': '你好', 'depends_on': []}]}
    assert '专家 B 原文' not in json.dumps(dispatch, ensure_ascii=False)
    assert steps[expert['id']]['input'] == dispatch['output']['tasks'][0]
    assert steps[expert['id']]['output'] == '专家 B 原文'
    assert steps[by_title['编排尾：出站收口']]['output'] == '出站正文'
    runtime.close()


def test_debug_step_io_includes_recommend_cards(tmp_path):
    from hailiang_skills.harness.runtime import debug_step_io
    head = json.dumps({'action': 'consult', 'subtasks': [{'subtask_id': 'st_1', 'question': '你好', 'target_expert_id': 'b', 'priority': 1}], 'reason': '寒暄'})
    raw_card = json.dumps({'jump_cards': [{'intent': 'open', 'object_type': 'skill', 'sub_type': 'multi_path', 'filter': {}, 'title': '升学路径'}]})

    class Client:
        def complete(self, messages, request_purpose='', max_tokens=None, **_):
            if request_purpose == 'orchestrator_head':
                return head
            if request_purpose == 'recommend_jump_cards':
                return raw_card
            return '出站正文'
        def complete_with_tools(self, messages, specs, request_purpose, max_tokens=None):
            return AssistantTurnResult(final_text='专家 B 原文')

    frozen = orchestrated_policy()
    frozen['orchestrator']['recommendation_rules'] = '推荐规则'
    frozen['orchestrator']['recommend_catalog'] = {'items': [{'intent': 'open', 'object_type': 'skill', 'sub_type': 'multi_path', 'title': '升学路径', 'description': '了解升学路径'}]}
    runtime = HarnessRuntime(HarnessStore.local(tmp_path / 'harness.sqlite3'), lambda _run: Client())
    run_id = runtime.create('s', 'u', 'p', '你好', frozen, identity_seed())
    advance(runtime, run_id, 10)
    run = runtime.store.get(run_id)
    assert run['status'] == 'completed', run.get('error')
    public = runtime.public(run)
    assert '推荐规则' not in json.dumps(public, ensure_ascii=False)
    recommend_id = next(node['id'] for node in public['nodes'] if node['title'] == '推荐')
    step = debug_step_io(run)[recommend_id]
    assert step['input']['recommendation_rules'] == '推荐规则'
    assert step['input']['user_question'] == '你好'
    assert step['input']['catalog'][0]['sub_type'] == 'multi_path'
    assert step['output']['jump_cards'][0]['sub_type'] == 'multi_path'
    assert step['output']['jump_cards'][0]['title'] == '升学路径'
    runtime.close()


def test_team_flow_runs_head_expert_and_tail(tmp_path):
    head = json.dumps({'action': 'consult', 'subtasks': [{'subtask_id': 'st_1', 'question': '你好', 'target_expert_id': 'b', 'priority': 1}], 'reason': '寒暄'})
    runtime, run, seen = run_orchestrated(tmp_path, head)
    assert run['status'] == 'completed', run.get('error')
    assert run['result'] == '出站正文'
    # The public SSE projection must not leave the orchestration tail pending
    # once the run has emitted its final response.
    assert runtime.public(run)['synthesis_status'] == 'completed'
    # Runs persisted before the run-level marker existed remain readable.
    legacy_shape = deepcopy(run)
    legacy_shape.pop('tail_done', None)
    assert runtime.public(legacy_shape)['synthesis_status'] == 'completed'
    experts = [t for t in run['tasks'].values() if t['kind'] == 'expert']
    assert [t['expert_id'] for t in experts] == ['b']
    assert seen['purposes'].index('orchestrator_head') < seen['purposes'].index('harness_expert') < seen['purposes'].index('orchestrator_tail')
    names = {t['name'] for t in run['tools'].values()}
    assert {'orchestrate_head', 'delegate_tasks', 'orchestrate_tail'} <= names
    runtime.close()


def test_team_flow_clarify_skips_experts(tmp_path):
    head = json.dumps({'action': 'clarify', 'clarification_question': '你想了解哪方面？', 'reason': '信息不足'})
    runtime, run, seen = run_orchestrated(tmp_path, head)
    assert run['status'] == 'completed'
    assert run['result'] == '你想了解哪方面？'
    assert not [t for t in run['tasks'].values() if t['kind'] == 'expert']
    assert 'orchestrator_tail' not in seen['purposes']
    runtime.close()


def test_team_flow_without_identity_asks_first(tmp_path):
    class Client:
        def complete(self, *_a, **_k):
            raise AssertionError('model must not run before the identity gate')
    runtime = HarnessRuntime(HarnessStore.local(tmp_path / 'harness.sqlite3'), lambda _run: Client())
    run_id = runtime.create('s', 'u', 'p', '你好', orchestrated_policy())
    advance(runtime, run_id, 4)
    run = runtime.store.get(run_id)
    assert run['status'] == 'completed'
    assert '身份' in run['result']
    runtime.close()


def formal_gate_runtime(tmp_path, head_json):
    class Client:
        def complete(self, messages, request_purpose='', max_tokens=None, **_):
            return head_json if request_purpose == 'orchestrator_head' else '出站正文'
        def complete_with_tools(self, messages, specs, request_purpose, max_tokens=None):
            return AssistantTurnResult(final_text='专家 B 原文')
    frozen = orchestrated_policy()
    frozen['identity_gate'] = 'form'
    runtime = HarnessRuntime(HarnessStore.local(tmp_path / 'harness.sqlite3'), lambda _run: Client())
    return runtime, runtime.create('s', 'u', 'p', '你好', frozen)


FORMAL_HEAD = json.dumps({'action': 'consult', 'subtasks': [{'subtask_id': 'st_1', 'question': '你好', 'target_expert_id': 'b', 'priority': 1}], 'reason': '寒暄'})


def test_formal_session_identity_gate_asks_with_form_then_continues(tmp_path):
    runtime, run_id = formal_gate_runtime(tmp_path, FORMAL_HEAD)
    advance(runtime, run_id, 4)
    run = runtime.store.get(run_id)
    assert run['status'] not in {'completed', 'failed'}
    request = next(q for q in run['inputs'].values() if q['status'] == 'pending')
    assert [(f['key'], f['scope']) for f in request['fields']] == [('user_role', 'user'), ('school_stage', 'profile')]
    runtime.answer(request['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='identity', expected_version=1,
                                                   answers={'user_role': '家长', 'school_stage': '高中'}))
    advance(runtime, run_id, 12)
    final = runtime.store.get(run_id)
    assert final['status'] == 'completed', final.get('error')
    assert final['result'] == '出站正文'
    assert len(final['inputs']) == 1
    names = {t['name'] for t in final['tools'].values()}
    assert {'platform_gate', 'orchestrate_head', 'orchestrate_tail'} <= names
    runtime.close()


def test_formal_session_identity_form_skipped_ends_with_text_prompt(tmp_path):
    runtime, run_id = formal_gate_runtime(tmp_path, FORMAL_HEAD)
    advance(runtime, run_id, 4)
    request = next(q for q in runtime.store.get(run_id)['inputs'].values() if q['status'] == 'pending')
    runtime.answer(request['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='skip', expected_version=1, skipped=True))
    advance(runtime, run_id, 12)
    final = runtime.store.get(run_id)
    assert final['status'] == 'completed'
    assert '身份' in final['result']
    assert len(final['inputs']) == 1
    assert not [t for t in final['tasks'].values() if t['kind'] == 'expert']
    runtime.close()


def test_single_expert_test_goes_straight_to_the_expert(tmp_path):
    seen = {'purposes': []}
    class Client:
        def complete(self, *_a, **_k):
            raise AssertionError('single expert tests must not call head or tail')
        def complete_with_tools(self, messages, specs, request_purpose, max_tokens=None):
            seen['purposes'].append(request_purpose)
            return AssistantTurnResult(final_text='专家原文')
    frozen = policy()
    frozen['direct_expert_id'] = 'b'
    runtime = HarnessRuntime(HarnessStore.local(tmp_path / 'harness.sqlite3'), lambda _run: Client())
    run_id = runtime.create('s', 'u', 'p', '你好', frozen)
    advance(runtime, run_id, 8)
    run = runtime.store.get(run_id)
    assert run['status'] == 'completed', run.get('error')
    assert run['result'] == '专家原文'
    assert seen['purposes'] == ['harness_expert']
    assert [t['expert_id'] for t in run['tasks'].values() if t['kind'] == 'expert'] == ['b']
    assert 'recommend' not in {t['name'] for t in run['tools'].values()}
    runtime.close()


def test_single_expert_direct_still_runs_recommend(tmp_path):
    from hailiang_skills.harness.runtime import debug_step_io
    raw_card = json.dumps({'jump_cards': [{'intent': 'open', 'object_type': 'skill', 'sub_type': 'multi_path', 'filter': {}, 'title': '升学路径'}]})
    seen = {'purposes': []}

    class Client:
        def complete(self, messages, request_purpose='', max_tokens=None, **_):
            seen['purposes'].append(request_purpose)
            if request_purpose == 'recommend_jump_cards':
                return raw_card
            raise AssertionError(f'unexpected complete purpose: {request_purpose}')

        def complete_with_tools(self, messages, specs, request_purpose, max_tokens=None):
            seen['purposes'].append(request_purpose)
            return AssistantTurnResult(final_text='专家原文')

    frozen = policy()
    frozen['direct_expert_id'] = 'b'
    frozen['orchestrator'] = {
        'enabled': False, 'head_rules': '', 'tail_rules': '', 'collaboration_rules': '',
        'recommendation_rules': '推荐规则',
        'recommend_catalog': {'items': [{'intent': 'open', 'object_type': 'skill', 'sub_type': 'multi_path', 'title': '升学路径', 'description': '了解升学路径'}]},
        'recommendations': [], 'consultation_policy': {}, 'fallback_expert_id': 'b', 'members': [],
    }
    runtime = HarnessRuntime(HarnessStore.local(tmp_path / 'harness.sqlite3'), lambda _run: Client())
    run_id = runtime.create('s', 'u', 'p', '你好', frozen)
    advance(runtime, run_id, 8)
    run = runtime.store.get(run_id)
    assert run['status'] == 'completed', run.get('error')
    assert run['result'] == '专家原文'
    assert 'orchestrator_head' not in seen['purposes']
    assert 'orchestrator_tail' not in seen['purposes']
    assert 'recommend_jump_cards' in seen['purposes']
    public = runtime.public(run)
    recommend_id = next(node['id'] for node in public['nodes'] if node['title'] == '推荐')
    step = debug_step_io(run)[recommend_id]
    assert step['input']['recommendation_rules'] == '推荐规则'
    assert step['output']['jump_cards'][0]['title'] == '升学路径'
    runtime.close()


def test_candidate_revision_root_resolves_object_type():
    from hailiang_skills.harness.policy import snapshot_root
    snapshot = {'root': {'kind': 'revision', 'object_id': 'o1', 'revision_id': 'r1'},
                'entries': [{'object_id': 'o1', 'object_type': 'expert_team', 'object_key': 'team'}]}
    assert snapshot_root(snapshot)['object_type'] == 'expert_team'
    assert snapshot_root(snapshot)['object_key'] == 'team'
    assert snapshot_root({'root': {'object_type': 'skill', 'object_key': 'k'}})['object_key'] == 'k'


def test_main_cannot_bypass_expert(tmp_path):
    def respond(messages, specs, purpose):
        if not any(m.role == 'tool' for m in messages):
            return call('execute_skill', {'skill_id': 'internal', 'task': '绕过专家'})
        return AssistantTurnResult(final_text='正确处理失败')
    runtime = make_runtime(tmp_path, respond)
    run_id = runtime.create('s', 'u', 'p', 'test', policy())
    advance(runtime, run_id, 8)
    run = runtime.store.get(run_id)
    assert len(run['tasks']) == 1
    assert next(iter(run['tools'].values()))['status'] == 'failed'
    assert validate_skill_boundary(['standalone'], {'a': {'skills': ['internal']}}) == ['standalone']
    with pytest.raises(HarnessError):
        validate_skill_boundary(['main_planner'], {'a': {'skills': ['career_plan_entity']}})
    runtime.close()


@pytest.mark.parametrize(
    ("advice", "expected", "source"),
    [
        ("阿里云代答", "阿里云代答", "aliyun_advice"),
        (None, OUTPUT_MODERATION_FALLBACK, "project_fallback"),
    ],
)
def test_blocked_final_main_reply_uses_safe_replacement(tmp_path, advice, expected, source):
    class BlockingModeration:
        def check(self, _content, *, stage, **_kwargs):
            if stage == "output":
                result = ModerationResult(
                    matched=True,
                    risk_level="high",
                    labels=["violence"],
                    provider="aliyun",
                    mode="cloud",
                    raw={"Data": {"Advice": [{"Answer": advice}]}} if advice else {},
                )
                raise ModerationBlockedError(result, stage="output")

    class Client:
        def complete_with_tools(self, *_args, **_kwargs):
            return AssistantTurnResult(final_text="不应公开的原始模型回复")

    runtime = HarnessRuntime(HarnessStore.local(tmp_path / "harness.sqlite3"), lambda _run: Client(), moderation=BlockingModeration())
    run_id = runtime.create("s", "u", "p", "规划", policy())
    advance(runtime, run_id, 2)
    run = runtime.store.get(run_id)

    assert run["status"] == "completed"
    assert run["result"] == expected
    assert run["outcome"] == "partial"
    assert run["safety"] == {"replacement_applied": True, "replacement_source": source}
    assert runtime.public(run)["result"] == expected
    runtime.close()


def test_two_expert_cards_resume_only_the_answered_node(tmp_path):
    def respond(messages, specs, purpose):
        if purpose == 'harness_main':
            if not any(m.role == 'tool' for m in messages):
                return call('delegate_tasks', {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': '分析 A'}, {'key': 'b', 'expert_id': 'b', 'goal': '分析 B'}]})
            return AssistantTurnResult(final_text='共同汇总')
        if not any(m.role == 'tool' for m in messages):
            return call('request_user_input', {'title': messages[1].content + '问题', 'fields': [{'key': 'detail', 'label': '补充详情'}]})
        return AssistantTurnResult(final_text='专家结果')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '共同分析', policy())
    advance(runtime, rid, 12)
    snapshot = runtime.public(runtime.store.get(rid))
    assert snapshot['waiting_count'] == 2
    cards = sorted(snapshot['inputs'], key=lambda q: q['title'])
    assert {q['status'] for q in cards} == {'pending', 'queued'}
    assert {q['actor'] for q in cards} == {'专家 A', '专家 B'}
    owner_a = cards[0]['bindings'][0]['task_id']
    owner_b = cards[1]['bindings'][0]['task_id']
    assert owner_a != owner_b
    queued = next(q for q in cards if q['status'] == 'queued')
    with pytest.raises(HarnessError):
        runtime.answer(queued['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='too-early', expected_version=1, text='提前回答'))
    active = next(q for q in cards if q['status'] == 'pending')
    runtime.answer(active['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='first-answer', expected_version=1, text='第一位的详情'))
    advance(runtime, rid, 8)
    waiting = runtime.store.get(rid)
    assert waiting['tasks'][active['bindings'][0]['task_id']]['status'] == 'completed'
    assert waiting['tasks'][queued['bindings'][0]['task_id']]['status'] == 'waiting_for_user'
    assert waiting['inputs'][queued['id']]['status'] == 'pending'
    assert waiting['status'] not in {'completed', 'failed'}
    runtime.answer(queued['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='second-answer', expected_version=waiting['inputs'][queued['id']]['version'], skipped=True))
    advance(runtime, rid, 12)
    assert runtime.store.get(rid)['status'] == 'completed'
    runtime.close()


def test_input_groups_freeze_current_batch_and_promote_next(tmp_path):
    """The first question is immediately usable; concurrent later questions batch."""
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='unused'))
    rid = runtime.create('s', 'u', 'p', '分析', policy())
    def request(run, changes, db):
        root = run['tasks'][run['root_task_id']]
        for index, key in enumerate(('province', 'subject', 'budget')):
            tool = {'id': f'q{index}', 'name': 'request_user_input', 'arguments': {
                'title': key, 'fields': [{'key': key, 'label': key, 'scope': 'profile'}]}}
            run['tools'][tool['id']] = {**tool, 'task_id': root['id'], 'call_id': tool['id'], 'status': 'running', 'attempts': 1}
            runtime._request(run, root, run['tools'][tool['id']], changes, db)
    runtime.store.mutate(rid, request)
    run = runtime.store.get(rid)
    groups = sorted(run['input_groups'].values(), key=lambda group: group['created_at'])
    assert [(g['status'], len(g['request_ids'])) for g in groups] == [('active', 1), ('queued', 2)]
    first = run['inputs'][groups[0]['active_request_id']]
    runtime.answer(first['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='first', expected_version=first['version'], text='浙江'))
    run = runtime.store.get(rid)
    assert groups[0]['id'] and run['input_groups'][groups[0]['id']]['status'] == 'completed'
    active = next(group for group in run['input_groups'].values() if group['status'] == 'active')
    assert len(active['request_ids']) == 2
    assert run['inputs'][active['active_request_id']]['status'] == 'pending'
    runtime.close()


def test_cross_expert_compatible_question_reuses_one_card_and_answer(tmp_path):
    def respond(messages, specs, purpose):
        if purpose == 'harness_main':
            if not any(m.role == 'tool' for m in messages):
                return call('delegate_tasks', {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': '分析 A'}, {'key': 'b', 'expert_id': 'b', 'goal': '分析 B'}]})
            return AssistantTurnResult(final_text='汇总')
        if not any(m.role == 'tool' for m in messages):
            return call('request_user_input', {'title': '补充省份', 'fields': [{'key': 'province', 'label': '省份', 'scope': 'profile'}]})
        return AssistantTurnResult(final_text='专家结论')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '共同分析', policy())
    advance(runtime, rid, 12)
    run = runtime.store.get(rid)
    assert len(run['inputs']) == 1
    request = next(iter(run['inputs'].values()))
    assert request['status'] == 'pending' and len(request['bindings']) == 2
    runtime.answer(request['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='province', expected_version=request['version'], text='浙江'))
    run = runtime.store.get(rid)
    assert all(run['tools'][binding['tool_id']]['status'] == 'completed' for binding in request['bindings'])
    assert run['inputs'][request['id']]['answer']['answers'] == {'province': '浙江'}
    runtime.close()


def test_search_budget_failure_is_returned_to_agent(tmp_path):
    calls = {'value': 0}
    def respond(messages, specs, purpose):
        if not any(m.role == 'tool' for m in messages):
            return call('search_skill_assets', {'query': '资料'}, 'search-1')
        if calls['value'] == 0:
            calls['value'] += 1
            return call('search_skill_assets', {'query': '再次检索'}, 'search-2')
        return AssistantTurnResult(final_text='检索受限，基于已有信息回答')
    runtime = make_runtime(tmp_path, respond)
    configured = policy()
    configured['settings']['max_search_calls_per_task'] = 1
    # Local Skill asset search must be present in the authorized catalog for the call to reach its budget.
    configured['skills']['independent']['specs'] = [{'name': 'rag', 'description': '资料检索', 'parameters_schema': {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'required': ['query']}}]
    rid = runtime.create('s', 'u', 'p', '分析', configured)
    advance(runtime, rid, 12)
    run = runtime.store.get(rid)
    assert run['status'] == 'completed'
    assert any(t['status'] == 'failed' and t['result']['error'] == 'SEARCH_BUDGET_EXCEEDED' for t in run['tools'].values())
    assert not run['inputs']
    runtime.close()


def test_search_catalog_follows_actor_skill_grants(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='完成'))
    configured = policy()
    configured['skills']['internal']['specs'] = [{'name': 'rag', 'description': '内部资料', 'parameters_schema': {}}]
    rid = runtime.create('s', 'u', 'p', '分析', configured)
    run = runtime.store.get(rid)
    main = run['tasks'][run['root_task_id']]
    assert 'search_skill_assets' not in {spec.name for spec in runtime.specs(run, main)}
    runtime.close()


def test_harness_input_budget_and_output_limit_are_enforced(tmp_path):
    requests = []
    class Client:
        def complete_with_tools(self, messages, specs, request_purpose, max_tokens=None):
            requests.append(max_tokens)
            return AssistantTurnResult(final_text='完成')
        def last_request_metrics(self):
            return {'input_tokens': 123, 'output_tokens': 12, 'total_tokens': 135}
    client = Client()
    runtime = HarnessRuntime(HarnessStore.local(tmp_path / 'budget.sqlite3'), lambda _run: client)
    configured = policy()
    configured['settings'].update(context_tokens=64000, max_output_tokens=8192, max_run_input_tokens=300000)
    rid = runtime.create('s', 'u', 'p', '分析', configured)
    advance(runtime, rid, 2)
    run = runtime.store.get(rid)
    assert requests == [8192]
    assert run['status'] == 'completed'
    assert run['input_tokens_estimated'] > 0
    assert runtime.public(run)['input_tokens_actual'] == 123
    runtime.close()

    blocked = HarnessRuntime(HarnessStore.local(tmp_path / 'blocked.sqlite3'), lambda _run: client)
    configured['settings']['max_run_input_tokens'] = 1
    blocked_rid = blocked.create('blocked', 'u', 'p', '分析', configured)
    advance(blocked, blocked_rid, 2)
    failed = blocked.store.get(blocked_rid)
    assert failed['status'] == 'failed'
    assert failed['error'] == 'RUN_INPUT_BUDGET_EXCEEDED'
    assert requests == [8192]
    blocked.close()


@pytest.mark.parametrize(('option', 'expected'), [
    ({'key': 'undergrad', 'label': '本科'}, {'label': '本科', 'value': 'undergrad'}),
    ({'label': '本科', 'key': 'undergrad'}, {'label': '本科', 'value': 'undergrad'}),
    ({'label': '本科', 'value': 'undergrad'}, {'label': '本科', 'value': 'undergrad'}),
    ({'key': 'undergrad'}, {'label': 'undergrad', 'value': 'undergrad'}),
    ({'value': '本科'}, {'label': '本科', 'value': '本科'}),
    ({'label': '本科'}, {'label': '本科', 'value': '本科'}),
    ({'A': '本科'}, {'label': 'A', 'value': '本科'}),
])
def test_select_option_normalization_preserves_label_and_value(option, expected):
    from hailiang_skills.harness.contracts import InputArguments, normalize_input_arguments
    arguments = {'title': '补充信息', 'fields': [{'key': 'stage', 'label': '阶段',
                 'input_type': 'single_select', 'options': [option]}]}
    normalized = normalize_input_arguments(arguments)
    assert InputArguments.model_validate(normalized).fields[0].options == [expected]


def test_select_question_map_is_normalized_and_invalid_call_has_field_feedback(tmp_path):
    options = {'是本人': 'self', '帮别人问': 'other'}
    question = {'title': '确认对象', 'fields': [{'key': 'who', 'label': '是谁的成绩', 'input_type': 'single_select', 'options': options}]}
    runtime = make_runtime(tmp_path, lambda messages, *_: call('request_user_input', question) if not any(m.role == 'tool' for m in messages) else AssistantTurnResult(final_text='已确认'))
    rid = runtime.create('s', 'u', 'p', '确认', policy())
    advance(runtime, rid, 3)
    run = runtime.store.get(rid)
    request = next(iter(run['inputs'].values()))
    assert request['status'] == 'pending'
    assert request['fields'][0]['options'] == [{'label': '是本人', 'value': 'self'}, {'label': '帮别人问', 'value': 'other'}]
    runtime.answer(request['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='answer', expected_version=1, answers={'who': 'self'}))
    advance(runtime, rid, 3)
    assert runtime.store.get(rid)['status'] == 'completed'
    runtime.close()

    invalid = {**question, 'fields': [{**question['fields'][0], 'options': 42}]}
    rejected = make_runtime(tmp_path / 'rejected', lambda messages, *_: call('request_user_input', invalid) if not any(m.role == 'tool' for m in messages) else AssistantTurnResult(final_text='说明格式失败'))
    rejected_rid = rejected.create('bad', 'u', 'p', '确认', policy())
    advance(rejected, rejected_rid, 5)
    failed_run = rejected.store.get(rejected_rid)
    failed_tool = next(iter(failed_run['tools'].values()))
    assert failed_tool['result']['error'] == 'INVALID_TOOL_ARGUMENTS'
    assert failed_tool['result']['field'] == 'fields.0.options'
    assert failed_tool['attempts'] == 1
    assert failed_run['tasks'][failed_run['root_task_id']]['tool_calls'] == 1
    rejected.close()


def test_failed_expert_cannot_be_recreated_for_same_run(tmp_path):
    def respond(messages, specs, purpose):
        if purpose == 'harness_main':
            delegated = [m for m in messages if m.role == 'tool' and m.name == 'delegate_tasks']
            if not delegated:
                return call('delegate_tasks', {'tasks': [{'key': 'first', 'expert_id': 'a', 'goal': '首次分析'}]}, 'delegate-1')
            if len(delegated) == 1:
                return call('delegate_tasks', {'tasks': [{'key': 'retry', 'expert_id': 'a', 'goal': '换个说法再试'}]}, 'delegate-2')
            return AssistantTurnResult(final_text='专家失败，说明限制')
        raise RuntimeError('EXPERT_MODEL_FAILED')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '分析', policy())
    advance(runtime, rid, 12)
    run = runtime.store.get(rid)
    assert len([t for t in run['tasks'].values() if t['kind'] == 'expert']) == 1
    assert any(t['result']['error'] == 'DUPLICATE_EXPERT_TASK' for t in run['tools'].values() if t['name'] == 'delegate_tasks' and t['status'] == 'failed')
    runtime.close()


def test_restart_and_fenced_late_result(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: call('request_user_input', QUESTION))
    run_id = runtime.create('s', 'u', 'p', 'test', policy())
    advance(runtime, run_id, 3)
    request = next(iter(runtime.store.get(run_id)['inputs'].values()))
    runtime.close()
    restarted = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='恢复结果'))
    restarted.answer(request['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='a', expected_version=1, text='高一'))
    lease = restarted.claim(run_id)
    assert lease
    restarted.cancel(run_id)
    restarted.execute(run_id, *lease)
    assert restarted.store.get(run_id)['status'] == 'cancelled'
    assert 'result' not in restarted.store.get(run_id)
    with pytest.raises(HarnessError):
        restarted.answer(request['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='b', expected_version=1, text='高二'))
    restarted.close()


def test_concurrent_claim_and_input_validation(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: call('request_user_input', QUESTION))
    run_id = runtime.create('s', 'u', 'p', 'test', policy())
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: runtime.claim(run_id), range(4)))
    assert sum(c is not None for c in claims) == 1
    runtime.execute(run_id, *next(c for c in claims if c))
    advance(runtime, run_id, 3)
    request = next(iter(runtime.store.get(run_id)['inputs'].values()))
    with pytest.raises(HarnessError):
        runtime.answer(request['id'], AnswerSubmission(user_id='other', profile_id='p', submission_id='x', expected_version=1, text='高一'))
    with pytest.raises(HarnessError):
        runtime.answer(request['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='x', expected_version=1, answers={'bad': 1}))
    assert runtime.store.get(run_id)['inputs'][request['id']]['status'] == 'pending'
    runtime.close()


def test_expert_selects_skill_and_main_independent_skill(tmp_path):
    purposes = []
    def respond(messages, specs, purpose):
        purposes.append(purpose)
        returned = [m for m in messages if m.role == 'tool']
        if purpose == 'harness_main':
            if not returned:
                return AssistantTurnResult(tool_calls=(
                    ToolCallRequest('experts', 'delegate_tasks', {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': '专家分析'}]}),
                    ToolCallRequest('standalone', 'execute_skill', {'skill_id': 'independent', 'task': '独立分析'})))
            assert any('专家综合' in m.content for m in returned)
            assert any('独立分析结果' in m.content for m in returned)
            return AssistantTurnResult(final_text='总汇总')
        if purpose == 'harness_expert':
            if not returned:
                return call('execute_skill', {'skill_id': 'internal', 'task': '内部专项'})
            assert '内部结果' in returned[-1].content
            return AssistantTurnResult(final_text='专家综合')
        return AssistantTurnResult(final_text='内部结果' if messages[1].content == '内部专项' else '独立分析结果')
    runtime = make_runtime(tmp_path, respond)
    run_id = runtime.create('s', 'u', 'p', 'test', policy())
    advance(runtime, run_id, 30)
    run = runtime.store.get(run_id)
    assert run['status'] == 'completed', run
    assert 'harness_expert' in purposes
    internal = next(t for t in run['tasks'].values() if t.get('skill_id') == 'internal')
    assert run['tasks'][internal['parent_id']]['kind'] == 'expert'
    standalone = next(t for t in run['tasks'].values() if t.get('skill_id') == 'independent')
    assert run['tasks'][standalone['parent_id']]['kind'] == 'main'
    runtime.close()


def test_dependency_cycle_and_shared_answer(tmp_path):
    def respond(messages, specs, purpose):
        if purpose == 'harness_main':
            if not any(m.role == 'tool' for m in messages):
                return call('delegate_tasks', {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': 'A'}, {'key': 'b', 'expert_id': 'b', 'goal': 'B'}]})
            return AssistantTurnResult(final_text='汇总')
        if not any(m.role == 'tool' for m in messages):
            return call('request_user_input', QUESTION)
        return AssistantTurnResult(final_text='已分析')
    runtime = make_runtime(tmp_path, respond)
    run_id = runtime.create('s', 'u', 'p', 'test', policy())
    advance(runtime, run_id, 10)
    run = runtime.store.get(run_id)
    assert len(run['inputs']) == 1
    request = next(iter(run['inputs'].values()))
    assert len(request['bindings']) == 2
    runtime.answer(request['id'], AnswerSubmission(user_id='u', profile_id='p', expected_version=1, submission_id='shared', text='高二'))
    advance(runtime, run_id, 20)
    assert runtime.store.get(run_id)['status'] == 'completed'
    runtime.close()
    cycle = make_runtime(tmp_path, lambda *_: call('delegate_tasks', {'tasks': [
        {'key': 'a', 'expert_id': 'a', 'goal': 'A', 'depends_on': ['b']},
        {'key': 'b', 'expert_id': 'b', 'goal': 'B', 'depends_on': ['a']}]}))
    cid = cycle.create('cycle', 'u', 'p', 'cycle', policy())
    advance(cycle, cid, 2)
    assert len(cycle.store.get(cid)['tasks']) == 1
    assert next(iter(cycle.store.get(cid)['tools'].values()))['result']['error'] == 'INVALID_TASK_DEPENDENCY'
    cycle.close()


def test_skip_does_not_create_another_identical_question(tmp_path):
    count = {'value': 0}
    def respond(*_):
        count['value'] += 1
        return call('request_user_input', QUESTION, f"c{count['value']}") if count['value'] < 3 else AssistantTurnResult(final_text='受限回答')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', 'test', policy())
    advance(runtime, rid, 3)
    q = next(iter(runtime.store.get(rid)['inputs'].values()))
    runtime.answer(q['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='skip', expected_version=1, skipped=True))
    advance(runtime, rid, 10)
    run = runtime.store.get(rid)
    assert run['status'] == 'completed'
    assert len(run['inputs']) == 1
    runtime.close()


def test_expired_lease_can_resume_but_old_worker_is_fenced(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='完成'))
    rid = runtime.create('s', 'u', 'p', 'test', policy())
    first = runtime.claim(rid)
    def expire(run, changes, db):
        run['tasks'][first[0]]['lease_until'] = '2000-01-01T00:00:00+00:00'
        changes.append({'event': 'test_expire', 'node_id': first[0]})
    runtime.store.mutate(rid, expire)
    second = runtime.claim(rid)
    assert second and first[1] != second[1]
    runtime.execute(rid, *first)
    assert runtime.store.get(rid)['status'] == 'running'
    runtime.execute(rid, *second)
    assert runtime.store.get(rid)['status'] == 'completed'
    runtime.close()


def test_waiting_child_does_not_strand_other_tool_calls(tmp_path):
    def respond(messages, specs, purpose):
        if purpose == 'harness_main' and not any(m.role == 'tool' for m in messages):
            return AssistantTurnResult(tool_calls=(ToolCallRequest('delegate', 'delegate_tasks', {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': 'A'}]}), ToolCallRequest('facts', 'read_facts', {})))
        return AssistantTurnResult(final_text='完成')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '规划', policy())
    advance(runtime, rid, 15)
    run = runtime.store.get(rid)
    assert run['status'] == 'completed'
    assert all(t['status'] == 'completed' and t['attempts'] == 1 for t in run['tools'].values())
    runtime.close()


def test_fact_correction_reviews_result_before_main_summary(tmp_path):
    grade = HarnessStore.fact_identity({'user_id': 'u', 'session_id': 's', 'profile_id': 'p'}, {'id': 'seed'}, 'profile', 'grade')
    seen = []
    def respond(messages, specs, purpose):
        if purpose == 'harness_main':
            if not any(m.role == 'tool' for m in messages):
                return call('delegate_tasks', {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': 'A'}, {'key': 'b', 'expert_id': 'b', 'goal': 'B'}]})
            assert '更正后' in messages[-1].content
            return AssistantTurnResult(final_text='使用更正后结果')
        if messages[1].content == 'A':
            result = '更正后 A' if '高二' in messages[0].content else '旧 A'
            seen.append(result)
            return AssistantTurnResult(final_text=result)
        if not any(m.role == 'tool' for m in messages):
            return call('request_user_input', {**QUESTION, 'clarification_reason': '用户刚说明年级需要更正，请核对'})
        return AssistantTurnResult(final_text='更正后 B')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '规划', policy(), {grade: {'key': 'grade', 'scope': 'profile', 'value': '高一', 'status': 'confirmed', 'version': 1}})
    advance(runtime, rid, 8)
    q = next(iter(runtime.store.get(rid)['inputs'].values()))
    runtime.answer(q['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='correction', expected_version=1, text='高二'))
    advance(runtime, rid, 20)
    run = runtime.store.get(rid)
    assert run['status'] == 'completed', run
    assert seen == ['旧 A', '更正后 A']
    assert all(t['result'] != '旧 A' for t in run['tasks'].values())
    assert runtime.facts(run, run['tasks'][run['root_task_id']])[grade]['version'] == 2
    runtime.close()


def test_shared_question_different_labels_and_field_keys(tmp_path):
    def respond(messages, specs, purpose):
        if purpose == 'harness_main':
            if not any(m.role == 'tool' for m in messages):
                return call('delegate_tasks', {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': 'A'}, {'key': 'b', 'expert_id': 'b', 'goal': 'B'}]})
            return AssistantTurnResult(final_text='汇总')
        if not any(m.role == 'tool' for m in messages):
            question = deepcopy(QUESTION)
            if messages[1].content == 'B':
                question['fields'][0].update(key='student_grade', label='孩子现在上几年级')
            return call('request_user_input', question)
        answers = json.loads(next(m.content for m in messages if m.role == 'tool'))['answers']
        assert answers['grade' if messages[1].content == 'A' else 'student_grade'] == '高二'
        return AssistantTurnResult(final_text='完成')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '规划', policy())
    advance(runtime, rid, 12)
    run = runtime.store.get(rid)
    assert len(run['inputs']) == 1
    q = next(iter(run['inputs'].values()))
    runtime.answer(q['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='shared', expected_version=1, text='高二'))
    advance(runtime, rid, 15)
    assert runtime.store.get(rid)['status'] == 'completed'
    runtime.close()


def test_search_retry_preserves_other_tool_results_and_events(tmp_path):
    def respond(messages, specs, purpose):
        if not any(m.role == 'tool' for m in messages):
            return AssistantTurnResult(tool_calls=(ToolCallRequest('search', 'web_search', {'query': '资料'}), ToolCallRequest('facts', 'read_facts', {})))
        return AssistantTurnResult(final_text='完成')
    runtime = make_runtime(tmp_path, respond)
    attempts = []
    def tool(run, task, tool):
        attempts.append(tool['attempts'])
        return {'error': 'TIMEOUT'} if len(attempts) == 1 else {'source': 'https://example.org/reference', 'text': '检索结果'}
    runtime.tool_executor = tool
    configured = policy()
    configured['skills']['independent']['specs'] = [{'name': 'web_search', 'description': 'search', 'parameters_schema': {'type': 'object', 'properties': {'query': {'type': 'string'}}}}]
    rid = runtime.create('s', 'u', 'p', 'test', configured)
    advance(runtime, rid, 12)
    run = runtime.store.get(rid)
    assert run['status'] == 'completed'
    assert attempts == [1, 2]
    assert next(t for t in run['tools'].values() if t['name'] == 'read_facts')['attempts'] == 1
    events = runtime.store.events('s')
    assert len({e['seq'] for e in events}) == len(events)
    assert [e['version'] for e in events] == sorted(e['version'] for e in events)
    assert runtime.store.events('s', events[-1]['seq']) == []
    assert any(e['event'] == 'tool_retrying' for e in events)
    assert '检索结果' not in json.dumps(events, ensure_ascii=False)
    runtime.close()


def test_cancel_in_flight_never_publishes_late_response(tmp_path):
    started, release = threading.Event(), threading.Event()
    def respond(*_):
        started.set()
        assert release.wait(10)
        return AssistantTurnResult(final_text='迟到结果')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', 'test', policy())
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.execute, rid, *runtime.claim(rid))
        assert started.wait(5)
        runtime.cancel(rid)
        release.set()
        future.result(timeout=5)
    assert runtime.store.get(rid)['status'] == 'cancelled'
    assert not runtime.public(runtime.store.get(rid))['result']
    runtime.close()


def test_concurrent_answer_commits_once(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: call('request_user_input', QUESTION))
    rid = runtime.create('s', 'u', 'p', 'test', policy())
    advance(runtime, rid, 3)
    q = next(iter(runtime.store.get(rid)['inputs'].values()))
    submission = AnswerSubmission(user_id='u', profile_id='p', submission_id='same', expected_version=1, text='高二')
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: runtime.answer(q['id'], submission), range(2)))
    assert results[0] == results[1]
    assert runtime.store.get(rid)['inputs'][q['id']]['version'] == 2
    assert sum(e['event'] == 'input_answered' for e in runtime.store.events('s')) == 1
    runtime.close()


def test_main_reply_length_validation_is_bounded(tmp_path):
    count = []
    def respond(*_):
        count.append(1)
        return AssistantTurnResult(final_text='过长的结论' * 10 if len(count) == 1 else '精简结论')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '回复不超过10字', policy())
    advance(runtime, rid, 10)
    assert runtime.store.get(rid)['result'] == '精简结论'
    assert len(count) == 2
    assert len(runtime.store.get(rid)['tasks'][runtime.store.get(rid)['root_task_id']]['trace']) == 2
    runtime.close()


def test_skill_reuses_expert_local_answers(tmp_path):
    question = {**QUESTION, 'fields': [{**QUESTION['fields'][0], 'scope': 'task'}]}
    def respond(messages, specs, purpose):
        returned = any(m.role == 'tool' for m in messages)
        if purpose == 'harness_main':
            return AssistantTurnResult(final_text='汇总') if returned else call('delegate_tasks', {'tasks': [{'key': 'a', 'expert_id': 'a', 'goal': '专家任务'}]})
        if purpose == 'harness_expert':
            if not returned:
                return call('request_user_input', question)
            if not any(m.name == 'execute_skill' for m in messages):
                return call('execute_skill', {'skill_id': 'internal', 'task': '进一步分析'}, 'skill')
            return AssistantTurnResult(final_text='专家综合')
        if not returned:
            return call('request_user_input', question)
        assert '高二' in next(m.content for m in messages if m.role == 'tool')
        return AssistantTurnResult(final_text='专项结果')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '规划', policy())
    advance(runtime, rid, 10)
    q = next(iter(runtime.store.get(rid)['inputs'].values()))
    runtime.answer(q['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='answer', expected_version=1, text='高二'))
    advance(runtime, rid, 20)
    run = runtime.store.get(rid)
    assert run['status'] == 'completed'
    assert len(run['inputs']) == 1
    runtime.close()


def test_fact_candidates_do_not_overwrite_confirmed_answers(tmp_path):
    def respond(messages, *_):
        if not any(m.role == 'tool' for m in messages):
            return call('propose_fact_updates', {'updates': [{'key': 'grade', 'value': '高三', 'scope': 'profile', 'status': 'candidate', 'evidence': '模型尚未核对的猜测'}]})
        if not any(m.name == 'request_user_input' for m in messages):
            return call('request_user_input', QUESTION, 'question')
        return AssistantTurnResult(final_text='完成')
    runtime = make_runtime(tmp_path, respond)
    identity = HarnessStore.fact_identity({'user_id': 'u', 'profile_id': 'p', 'session_id': 's'}, {'id': 'seed'}, 'profile', 'grade')
    rid = runtime.create('s', 'u', 'p', '规划', policy(), {identity: {'key': 'grade', 'value': '高二', 'scope': 'profile', 'status': 'confirmed', 'version': 1}})
    advance(runtime, rid, 10)
    run = runtime.store.get(rid)
    assert not run['inputs']
    facts = runtime.facts(run, run['tasks'][run['root_task_id']])
    assert facts[identity]['value'] == '高二'
    assert sum(f['status'] == 'candidate' for f in facts.values()) == 1
    runtime.close()


def test_large_evidence_projection_retains_raw_source_and_native_id(tmp_path):
    from hailiang_skills.harness.evidence import project_evidence
    tool = {'id': 'task:call', 'status': 'completed', 'result': {'text': '证据内容' * 20000}}
    content, metadata = project_evidence(tool, 1000)
    assert metadata['source_tool_id'] == tool['id']
    assert metadata['truncated']
    assert len(content) < 1600
    assert len(tool['result']['text']) == 80000


def _crash_during_model(path, run_id):
    import os
    from sqlalchemy import create_engine
    url = os.getenv('HAILIANG_HARNESS_TEST_DATABASE_URL')
    store = HarnessStore(create_engine(url)) if url else HarnessStore.local(path)
    class Client:
        def complete_with_tools(self, *_args, **_kwargs):
            os._exit(29)
    runtime = HarnessRuntime(store, lambda _: Client(), lease_seconds=1)
    runtime.execute(run_id, *runtime.claim(run_id))


def test_process_crash_recovers_durable_model_checkpoint(tmp_path):
    import multiprocessing
    import time
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='重启后完成'))
    rid = runtime.create('s', 'u', 'p', '规划', policy())
    process = multiprocessing.get_context('spawn').Process(target=_crash_during_model, args=(tmp_path / 'harness.sqlite3', rid))
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        pytest.fail('Crash fixture did not exit')
    assert process.exitcode == 29
    checkpoint = runtime.store.get(rid)['tasks'][runtime.store.get(rid)['root_task_id']]
    assert checkpoint['status'] == 'running' and checkpoint['trace'][0]['input']
    deadline = time.monotonic() + 5
    lease = None
    while time.monotonic() < deadline and not lease:
        lease = runtime.claim(rid)
        if not lease:
            time.sleep(.05)
    assert lease
    runtime.execute(rid, *lease)
    run = runtime.store.get(rid)
    assert run['status'] == 'completed'
    assert len(run['tasks'][run['root_task_id']]['execution_attempts']) == 2
    assert run['tasks'][run['root_task_id']]['execution_attempts'][0]['status'] == 'superseded'
    runtime.close()


@pytest.mark.parametrize('evidence,expected', [('年级是高二', '高二'), ('模型猜测高二', '高一')])
def test_user_correction_requires_original_evidence(tmp_path, evidence, expected):
    def respond(messages, *_):
        if not any(m.role == 'tool' for m in messages):
            return call('propose_fact_updates', {'updates': [{'key': 'grade', 'scope': 'profile', 'value': '高二', 'status': 'user_statement', 'evidence': evidence}]})
        return AssistantTurnResult(final_text='完成')
    runtime = make_runtime(tmp_path, respond)
    identity = HarnessStore.fact_identity({'user_id': 'u', 'profile_id': 'p', 'session_id': 's'}, {'id': 'seed'}, 'profile', 'grade')
    rid = runtime.create('s', 'u', 'p', '之前说错了，年级是高二', policy(), {identity: {'key': 'grade', 'value': '高一', 'scope': 'profile', 'status': 'confirmed', 'version': 1}})
    advance(runtime, rid, 8)
    run = runtime.store.get(rid)
    assert runtime.facts(run, run['tasks'][run['root_task_id']])[identity]['value'] == expected
    runtime.close()


def test_skill_asset_snapshot_survives_changes_to_original_files(tmp_path, monkeypatch):
    from hailiang_skills.harness import assets
    monkeypatch.setattr(assets, 'state_root', lambda: tmp_path / 'state')
    original = tmp_path / 'source'
    original.mkdir()
    (original / 'SKILL.md').write_text('原有规则')
    snapshot = assets.snapshot_assets(original)
    (original / 'SKILL.md').write_text('发布后的新规则')
    restored = assets.materialize_assets(snapshot)
    assert (restored / 'SKILL.md').read_text() == '原有规则'
    with pytest.raises(HarnessError):
        assets.materialize_assets({**snapshot, 'digest': 'invalid'})


@pytest.fixture(autouse=True)
def postgres_test_backend(monkeypatch):
    import os
    url = os.getenv('HAILIANG_HARNESS_TEST_DATABASE_URL')
    if not url:
        yield
        return
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from hailiang_skills.harness.models import TABLES
    assert make_url(url).database == 'harness_test', 'Only the isolated harness_test database may be used.'
    engine = create_engine(url)
    with engine.begin() as connection:
        for table in reversed(TABLES):
            connection.execute(table.delete())
    monkeypatch.setattr(HarnessStore, 'local', classmethod(lambda cls, _path: cls(engine)))
    yield
    engine.dispose()
