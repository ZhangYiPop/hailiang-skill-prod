import json
import pytest
from test_harness import make_runtime, advance, call, postgres_test_backend
from test_harness_context_v2 import v2_policy
from hailiang_skills.harness.contracts import HarnessError
from hailiang_skills.skill_runtime.models import AssistantTurnResult


def test_public_events_are_immutable_and_scope_isolated(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: call('finalize_response', {}))
    rid = runtime.create('s', 'u', 'p', '分析', v2_policy(), wire_protocol='hailiang.sse.v3')
    before = runtime.store.public_snapshot('s', 'u', 'p')
    events = runtime.store.public_events('s', 'u', 'p', 0)
    assert events and events[0]['protocol'] == 'hailiang.sse.v3'
    runtime.cancel(rid)
    assert runtime.store.public_events('s', 'u', 'p', 0)[:len(events)] == events
    after = runtime.store.public_snapshot('s', 'u', 'p')
    assert after['cursor'] > before['cursor']
    assert after['runs'][0]['status'] == 'cancelled'
    assert runtime.store.public_snapshot('s', 'other', 'p')['runs'] == []
    assert 'trace' not in json.dumps(after)
    with pytest.raises(HarnessError): runtime.store.public_events('s', 'u', 'p', 999999)
    runtime.close()


def test_v3_snapshot_projects_grouped_input_queue(tmp_path):
    from hailiang_skills.harness.contracts import AnswerSubmission
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='unused'))
    rid = runtime.create('s', 'u', 'p', '分析', v2_policy(), wire_protocol='hailiang.sse.v3')
    def add_questions(run, changes, db):
        root = run['tasks'][run['root_task_id']]
        for index, key in enumerate(('province', 'budget')):
            tool = {'id': f'group-tool-{index}', 'name': 'request_user_input', 'arguments': {
                'title': key, 'fields': [{'key': key, 'label': key, 'scope': 'profile'}],
            }, 'task_id': root['id'], 'call_id': f'group-call-{index}', 'status': 'running', 'attempts': 1,
                'created_at': None, 'started_at': None, 'ended_at': None}
            run['tools'][tool['id']] = tool
            runtime._request(run, root, tool, changes, db)
    runtime.store.mutate(rid, add_questions)
    snapshot = runtime.store.public_snapshot('s', 'u', 'p')
    entity = next(item for item in snapshot['runs'] if item['id'] == rid)
    assert entity['input_queue']['active_group_total'] == 1
    assert len(entity['input_queue']['queued_group_ids']) == 1
    groups = [item for item in snapshot['input_groups'] if item['run_id'] == rid]
    assert [(item['status'], item['total_count']) for item in groups] == [('active', 1), ('queued', 1)]
    active = next(item for item in snapshot['input_requests'] if item['status'] == 'pending')
    runtime.answer(active['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='group-answer', expected_version=active['request_version'], text='浙江'))
    snapshot = runtime.store.public_snapshot('s', 'u', 'p')
    entity = next(item for item in snapshot['runs'] if item['id'] == rid)
    assert entity['input_queue']['active_group_total'] == 1
    assert len([item for item in snapshot['input_requests'] if item['status'] == 'pending']) == 1
    runtime.close()


def test_idempotent_start_and_conflicting_submission(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='unused'))
    sub = {'key': 'idempotent', 'fingerprint': 'one'}
    a = runtime.create('s', 'u', 'p', '分析', v2_policy(), wire_protocol='hailiang.sse.v3', submission=sub)
    b = runtime.create('s', 'u', 'p', '分析', v2_policy(), wire_protocol='hailiang.sse.v3', submission=sub)
    assert a == b
    with pytest.raises(HarnessError, match='SUBMISSION_ID_REUSED'):
        runtime.create('s', 'u', 'p', 'different', v2_policy(), wire_protocol='hailiang.sse.v3', submission={**sub, 'fingerprint': 'two'})
    runtime.close()


def test_real_stream_interface_unicode_and_cancel(tmp_path):
    from types import SimpleNamespace
    runtime = make_runtime(tmp_path, lambda *_: call('finalize_response', {}))
    class Client:
        def complete_with_tools(self, *args, **kwargs): return call('finalize_response', {})
        def stream_complete(self, *args, **kwargs):
            yield SimpleNamespace(content_delta='中文')
            yield SimpleNamespace(content_delta='😀建议')
        def last_request_metrics(self): return {'input_tokens': 20, 'output_tokens': 5, 'finish_reason': 'stop'}
    runtime.client_factory = lambda _: Client()
    rid = runtime.create('s', 'u', 'p', '分析', v2_policy(), wire_protocol='hailiang.sse.v3')
    advance(runtime, rid, 8)
    run = runtime.store.get(rid)
    assert run['status'] == 'completed', run.get('error')
    assert run['result'] == '中文😀建议'
    snap = runtime.store.public_snapshot('s', 'u', 'p')
    assert snap['messages'][-1]['content'] == run['result']
    deltas = [e for e in runtime.store.public_events('s', 'u', 'p', 0) if e['type'] == 'message.delta']
    assert ''.join(e['data']['text'] for e in deltas) == run['result']
    events = runtime.store.public_events('s', 'u', 'p', 0)
    finalized = [e for e in events if e['type'] == 'run.finalized']
    done = [e for e in events if e['type'] == 'done']
    assert len(finalized) == 1 and len(done) == 1 and done[0] == events[-1]
    assert done[0]['snapshot']['runs'][0]['status'] == 'completed'
    record = runtime.store.public_record('s', 'u', 'p', rid)
    assert record['assistant_message']['content'] == run['result']
    assert record['execution']['status'] == 'completed'
    assert run['input_tokens_actual'] == 40
    root = run['tasks'][run['root_task_id']]
    assert not runtime.store.stream_text(rid, root['id'], root['lease_token'], '迟到')
    runtime.close()


def test_v3_http_start_retry_and_capability_gate(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from test_harness_api import Repository, Facts
    import hailiang_skills.api.routes.harness as api
    runtime = make_runtime(tmp_path, lambda *_: call('finalize_response', {}))
    monkeypatch.setattr(api, 'capture_policy', lambda *_: v2_policy())
    app = FastAPI()
    app.include_router(api.build_harness_router(Repository(), Facts(), None, runtime, protocol_v3=True), prefix='/api/v3')
    with TestClient(app) as client:
        path = '/api/v3/sessions/new/harness'
        monkeypatch.setenv('HAILIANG_SSE_V3', 'false')
        assert client.get(path + '?user_id=u').status_code == 404
        monkeypatch.setenv('HAILIANG_SSE_V3', 'true')
        body = {'goal': '规划', 'submission_id': 'same', 'context_data': {'user_id': 'u'}}
        first = client.post(path + '/runs', json=body)
        assert first.status_code == 202, first.text
        assert client.post(path + '/runs', json=body).json()['run_id'] == first.json()['run_id']
        assert client.post(path + '/runs', json={**body, 'goal': 'changed'}).status_code == 409
        replacement = client.post(path + '/runs', json={**body, 'submission_id': 'new', 'goal': '新问题'})
        assert replacement.status_code == 202
        old = runtime.store.get(first.json()['run_id'])
        assert old['status'] == 'cancelled'
        assert old['cancellation_reason'] == 'superseded_by_new_run'
        assert old['superseded_by_run_id'] == replacement.json()['run_id']
        assert replacement.json()['superseded_run_id'] == first.json()['run_id']
        new = runtime.store.get(replacement.json()['run_id'])
        assert new['tasks'][new['root_task_id']]['inherited_history_count'] == 0
        record = client.get(path + f"/runs/{first.json()['run_id']}/record?user_id=u")
        assert record.status_code == 200
        assert record.json()['execution']['cancellation_reason'] == 'superseded_by_new_run'
        finite = client.get(path + f"/runs/{first.json()['run_id']}/events?user_id=u")
        assert finite.status_code == 200
        assert 'event: state' in finite.text and 'event: done' in finite.text
        assert '"snapshot"' in finite.text
        monkeypatch.setenv('HAILIANG_SSE_V3_RUN_STREAM_SECONDS', '0.1')
        timed = client.get(path + f"/runs/{replacement.json()['run_id']}/events?user_id=u")
        assert timed.status_code == 200
        assert 'event: timeout' in timed.text
        active_id = replacement.json()['run_id']
        def suspend(run, changes, _db):
            root = run['tasks'][run['root_task_id']]
            root.update(status='waiting_for_user', lease_token='', lease_until='')
            run['inputs']['input_finite'] = {
                'id': 'input_finite', 'title': '补充省份', 'fields': [], 'status': 'pending',
                'version': 1, 'round': 1, 'bindings': [], 'owner_task_id': root['id'], 'actor': '专家 A',
            }
            changes.append({'event': 'input_requested', 'node_id': root['id']})
        runtime.store.mutate(active_id, suspend)
        suspended = client.get(path + f"/runs/{active_id}/events?user_id=u")
        assert suspended.status_code == 200
        assert 'event: suspended' in suspended.text
        assert client.get(path + '?user_id=other').status_code == 403
        snap = client.get(path + '?user_id=u').json()
        assert snap['protocol'] == 'hailiang.sse.v3'
        # Invalid cursor stream is finite and explicitly requests synchronization.
        invalid = client.get(path + '/events?user_id=u&after=999999')
        assert 'resync_required' in invalid.text
    runtime.close()


def test_restart_revision_and_late_delta_are_safe(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='unused'))
    rid = runtime.create('s', 'u', 'p', '分析', v2_policy(), wire_protocol='hailiang.sse.v3')
    tid, token = runtime.claim(rid)
    assert runtime.store.stream_text(rid, tid, token, reset=True)
    runtime.store.stream_text(rid, tid, token, '旧😀')
    runtime.store.mutate(rid, lambda r, changes, db: (r['tasks'][tid].update(lease_until='2000'), changes.append({'event':'expired'})))
    tid, replacement = runtime.claim(rid)
    assert not runtime.store.stream_text(rid, tid, token, '迟到')
    runtime.store.stream_text(rid, tid, replacement, reset=True)
    runtime.store.stream_text(rid, tid, replacement, '新')
    runtime.cancel(rid)
    assert not runtime.store.stream_text(rid, tid, replacement, '迟到')
    message = runtime.store.public_snapshot('s', 'u', 'p')['messages'][0]
    assert message['revision'] == 2 and message['content'] == '新' and message['status'] == 'interrupted'
    runtime.close()


def test_superseding_run_is_atomic_and_does_not_inherit_drafts(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='unused'))
    first = runtime.create('s', 'u', 'p', '旧问题', v2_policy(), wire_protocol='hailiang.sse.v3')
    task_id, token = runtime.claim(first)
    assert runtime.store.stream_text(first, task_id, token, reset=True)
    assert runtime.store.stream_text(first, task_id, token, '未完成草稿')
    second = runtime.create('s', 'u', 'p', '新问题', v2_policy(), history=[],
                            wire_protocol='hailiang.sse.v3', supersede_active=True)
    old = runtime.store.get(first)
    new = runtime.store.get(second)
    assert old['status'] == 'cancelled' and old['superseded_by_run_id'] == second
    assert new['status'] == 'running'
    messages = new['tasks'][new['root_task_id']]['messages']
    assert [(message['role'], message['content']) for message in messages] == [('user', '新问题')]
    events = runtime.store.public_events('s', 'u', 'p', 0)
    final_index = next(i for i, event in enumerate(events) if event['type'] == 'run.finalized' and event['run_id'] == first)
    new_index = next(i for i, event in enumerate(events) if event['type'] == 'run.updated' and event['run_id'] == second)
    assert final_index < new_index
    record = runtime.store.public_record('s', 'u', 'p', first)
    assert record['assistant_message']['content'] == '未完成草稿'
    assert record['assistant_message']['status'] == 'interrupted'
    runtime.close()


def test_scope_sequence_serializes_concurrent_writers(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='unused'))
    rid = runtime.create('s', 'u', 'p', '分析', v2_policy(), wire_protocol='hailiang.sse.v3')
    def update(i):
        def mutate(run, changes, db):
            run['phase'] = str(i)
            changes.append({'event': 'phase'})
        runtime.store.mutate(rid, mutate)
    with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(update, range(12)))
    events = runtime.store.public_events('s', 'u', 'p', 0)
    assert [e['seq'] for e in events] == list(range(1, len(events) + 1))
    runtime.close()


def test_schema_validates_generated_wire_fixture():
    from pathlib import Path
    from jsonschema import Draft202012Validator
    root = Path(__file__).parents[1] / 'guides/sse_v3'
    validator = Draft202012Validator(json.loads((root/'event.schema.json').read_text()))
    events = json.loads((root/'parallel-partial-stream.events.json').read_text())
    for event in events: validator.validate(event)
    assert any(e['type']=='message.delta' for e in events)
    assert any(e['type']=='input_request.updated' and e['data'].get('answer',{}).get('skipped_fields') for e in events)
    assert any(e['type']=='run.updated' and e['data'].get('outcome')=='partial' for e in events)


def test_final_moderation_retracts_published_text(tmp_path):
    from types import SimpleNamespace
    from hailiang_skills.security.models import ModerationBlockedError
    runtime = make_runtime(tmp_path, lambda *_: call('finalize_response', {}))
    class Client:
        def complete_with_tools(self,*args,**kw): return call('finalize_response',{})
        def stream_complete(self,*args,**kw): yield SimpleNamespace(content_delta='待检查内容')
        def last_request_metrics(self): return {'input_tokens':10,'output_tokens':4}
    class Moderation:
        count = 0
        def check(self,text,**kw):
            self.count += 1
            if self.count == 2: raise ModerationBlockedError(SimpleNamespace(risk_level='blocked'),stage='output')
    runtime.client_factory = lambda _:Client()
    runtime.moderation = Moderation()
    rid = runtime.create('s','u','p','分析',v2_policy(),wire_protocol='hailiang.sse.v3')
    advance(runtime,rid,8)
    run = runtime.store.get(rid)
    assert run['status']=='failed'
    message = runtime.store.public_snapshot('s','u','p')['messages'][0]
    assert message['status']=='blocked' and message['content']==''
    assert run['input_tokens_actual']==20
    runtime.close()


def test_old_run_bootstrap_snapshot_then_future_updates(tmp_path):
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='旧运行结果'))
    rid = runtime.create('s', 'u', 'p', '旧任务', v2_policy())
    snap = runtime.store.public_snapshot('s', 'u', 'p')
    assert snap['runs'][0]['id'] == rid
    advance(runtime, rid, 2)
    events = runtime.store.public_events('s', 'u', 'p', snap['cursor'])
    assert any(e['type'] == 'message.updated' and e['data']['content'] == '旧运行结果' for e in events)
    assert runtime.store.get(rid).get('wire_protocol') is None
    runtime.close()


def test_projection_transaction_rolls_back_events_and_cursor(tmp_path):
    from hailiang_skills.harness.protocol_v3 import lock_scope, upsert
    runtime = make_runtime(tmp_path, lambda *_: AssistantTurnResult(final_text='unused'))
    runtime.create('s','u','p','分析',v2_policy(),wire_protocol='hailiang.sse.v3')
    before = runtime.store.public_snapshot('s','u','p')
    with pytest.raises(RuntimeError):
        with runtime.store.transaction() as db:
            scope = lock_scope(db,'s','u','p')
            upsert(db,scope,'messages','message.updated','r',{'id':'fake','content':'不应出现'})
            db.flush()
            raise RuntimeError('crash before commit')
    assert runtime.store.public_snapshot('s','u','p') == before
    assert runtime.store.public_events('s','u','p',before['cursor']) == []
    runtime.close()
