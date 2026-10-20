from __future__ import annotations
from copy import deepcopy
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hailiang_skills.api.routes.harness import build_harness_router
from hailiang_skills.core.context import SessionContext
from hailiang_skills.llm.config import load_llm_config
from hailiang_skills.schemas.facts import KnownFacts
from hailiang_skills.harness.contracts import AnswerSubmission
from test_harness import make_runtime, policy, advance, call, QUESTION, postgres_test_backend
from hailiang_skills.skill_runtime.models import AssistantTurnResult


class Repository:
    def __init__(self):
        self.items = {}
    def get(self, key):
        return deepcopy(self.items[key])
    def save(self, context):
        self.items[context.session_id] = deepcopy(context)
        return context
    create = save


class Facts:
    profile_repo = SimpleNamespace()
    def hydrate_context(self, context):
        context.refresh_effective_facts()
    def persist_context(self, context):
        pass
    def validate_configured_update(self, *args):
        return None


def test_run_api_ownership_idempotence_modes_and_private_trace(tmp_path, monkeypatch):
    import hailiang_skills.api.routes.harness as api
    monkeypatch.setattr(api, 'capture_policy', lambda *_: policy())
    monkeypatch.setenv('HAILIANG_HARNESS_NEW_SESSIONS', 'true')
    runtime = make_runtime(tmp_path, lambda messages, *_: AssistantTurnResult(final_text='汇总') if any(m.role == 'tool' for m in messages) else call('request_user_input', QUESTION))
    repository = Repository()
    app = FastAPI()
    app.include_router(build_harness_router(repository, Facts(), None, runtime), prefix='/api/v2')
    with TestClient(app) as client:
        assert client.get('/api/v2/sessions/new/harness?user_id=u').json()['runtime_mode'] == 'harness_v1'
        body = {'goal': '规划', 'context_data': {'user_id': 'u'}}
        started = client.post('/api/v2/sessions/new/harness/runs', json=body)
        assert started.status_code == 202, started.text
        rid = started.json()['run_id']
        # Change the question to session scope for this unbound conversation.
        runtime.client_factory = lambda _: SimpleNamespace(complete_with_tools=lambda messages, *_args, **_kwargs: AssistantTurnResult(final_text='汇总') if any(m.role == 'tool' for m in messages) else call('request_user_input', {'title': '补充年级', 'fields': [{'key': 'grade', 'label': '年级', 'scope': 'session'}]}))
        advance(runtime, rid, 4)
        snapshot = client.get('/api/v2/sessions/new/harness?user_id=u').json()
        assert snapshot['runs'][0]['waiting_count'] == 1
        q = snapshot['runs'][0]['inputs'][0]
        assert client.post('/api/v2/sessions/new/harness/runs', json=body).status_code == 409
        assert client.get(f'/api/v2/sessions/new/harness/runs/{rid}?user_id=other').status_code == 403
        assert client.get(f'/api/v2/sessions/new/harness/runs/{rid}/trace?user_id=u').status_code == 403
        assert client.get('/api/v2/sessions/new/harness/events?user_id=u', headers={'Last-Event-ID': 'bad'}).status_code == 400
        answer = {'user_id': 'u', 'submission_id': 'answer', 'expected_version': 1, 'text': '高二'}
        url = f"/api/v2/sessions/new/input-requests/{q['id']}/answers"
        assert client.post(url, json={**answer, 'expected_version': 2}).status_code == 409
        assert client.post(url, json={**answer, 'user_id': 'other'}).status_code == 403
        assert client.post(url, json=answer).status_code == 202
        assert client.post(url, json=answer).status_code == 202
        advance(runtime, rid, 5)
        assert client.get(f'/api/v2/sessions/new/harness/runs/{rid}?user_id=u').json()['result'] == '汇总'
        saved = repository.items.pop('new')
        assert client.get('/api/v2/sessions/new/harness?user_id=other').status_code == 403
        repository.items['new'] = saved
        monkeypatch.setenv('HAILIANG_HARNESS_NEW_SESSIONS', 'false')
        assert client.get('/api/v2/sessions/new/harness?user_id=u').json()['runtime_mode'] == 'harness_v1'
        assert client.post('/api/v2/sessions/new/harness/runs', json=body).status_code == 202
        assert client.post('/api/v2/sessions/disabled/harness/runs', json=body).status_code == 409
        repository.save(SessionContext(session_id='legacy', user_id='u'))
        assert client.get('/api/v2/sessions/legacy/harness?user_id=u').json()['runtime_mode'] == 'legacy'
        assert client.post('/api/v2/sessions/legacy/harness/runs', json=body).status_code == 409
    runtime.close()


def test_run_goal_seeds_explicit_grade_before_requesting_missing_facts(tmp_path, monkeypatch):
    import hailiang_skills.api.routes.harness as api
    monkeypatch.setattr(api, 'capture_policy', lambda *_: policy())
    runtime = make_runtime(
        tmp_path,
        lambda messages, *_args, **_kwargs: (
            AssistantTurnResult(final_text='完成')
            if any(message.role == 'tool' for message in messages)
            else call('request_user_input', {
                'title': '补充年级',
                'fields': [{'key': 'grade', 'label': '当前年级', 'scope': 'session'}],
            })
        ),
    )
    app = FastAPI()
    app.include_router(build_harness_router(Repository(), Facts(), None, runtime), prefix='/api/v2')

    with TestClient(app) as client:
        started = client.post('/api/v2/sessions/grade-seed/harness/runs', json={
            'goal': '帮高二学生做选科规划：偏理科，目标冲强基，家里希望稳妥一点',
            'context_data': {'user_id': 'u'},
        })
        assert started.status_code == 202, started.text
        rid = started.json()['run_id']
        advance(runtime, rid, 5)
        run = runtime.store.get(rid)

    assert run['status'] == 'completed'
    assert not run['inputs']
    tool = next(iter(run['tools'].values()))
    assert tool['result'] == {'answers': {'grade': '高二'}, 'source': 'existing_facts'}
    runtime.close()


def test_manual_revision_test_selects_model_before_policy_capture(tmp_path, monkeypatch):
    import hailiang_skills.api.routes.harness as api

    seen = {}

    def capture(_orchestrator, context):
        seen["model"] = context.session_meta.get("requested_llm_model")
        return policy()

    monkeypatch.setattr(api, "capture_policy", capture)
    monkeypatch.setattr(api, "deployment_environment", lambda: "test")
    runtime = make_runtime(tmp_path, lambda *_args, **_kwargs: AssistantTurnResult(final_text="ok"))
    app = FastAPI()
    app.include_router(
        build_harness_router(Repository(), Facts(), SimpleNamespace(llm_config=load_llm_config()), runtime),
        prefix="/api/v2",
    )
    with TestClient(app) as client:
        started = client.post("/api/v2/sessions/model-pro/harness/runs", json={
            "goal": "规划",
            "context_data": {"user_id": "u"},
            "model": "pro",
        })
        assert started.status_code == 202, started.text
        rejected = client.post("/api/v2/sessions/model-bad/harness/runs", json={
            "goal": "规划",
            "context_data": {"user_id": "u"},
            "model": "other",
        })
        assert rejected.status_code == 422

    assert seen["model"] == "deepseek-v4-pro"
    runtime.close()


def test_model_failure_does_not_create_input_requests(tmp_path):
    def fail(*_):
        raise TimeoutError()
    runtime = make_runtime(tmp_path, fail)
    rid = runtime.create('s', 'u', 'p', '规划', policy())
    advance(runtime, rid, 4)
    run = runtime.store.get(rid)
    assert run['status'] == 'failed'
    assert not run['inputs']
    assert run['tasks'][run['root_task_id']]['trace'][0]['error'] == 'TimeoutError'
    assert not runtime.store.active('s')
    runtime.close()


def test_partial_known_fields_are_not_asked_again(tmp_path):
    from hailiang_skills.harness.store import HarnessStore
    identity = HarnessStore.fact_identity({'user_id': 'u', 'profile_id': 'p', 'session_id': 's'}, {'id': 'seed'}, 'profile', 'grade')
    def respond(messages, *_):
        if not any(m.role == 'tool' for m in messages):
            return call('request_user_input', {'title': '补充情况', 'fields': [QUESTION['fields'][0], {'key': 'goal', 'label': '目标', 'scope': 'profile'}]})
        return AssistantTurnResult(final_text='完成')
    runtime = make_runtime(tmp_path, respond)
    rid = runtime.create('s', 'u', 'p', '规划', policy(), {identity: {'key': 'grade', 'value': '高二', 'scope': 'profile', 'status': 'confirmed', 'version': 1}})
    advance(runtime, rid, 4)
    run = runtime.store.get(rid)
    q = next(iter(run['inputs'].values()))
    assert [f['key'] for f in q['fields']] == ['goal']
    runtime.answer(q['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='answer', expected_version=1, text='提升数学'))
    tool = next(iter(runtime.store.get(rid)['tools'].values()))
    assert tool['result']['answers'] == {'grade': '高二', 'goal': '提升数学'}
    runtime.close()


def test_sse_disconnect_replays_cursor_without_canceling_execution(tmp_path):
    import asyncio
    from hailiang_skills.core.context import SessionContext
    runtime = make_runtime(tmp_path, lambda messages, *_: AssistantTurnResult(final_text='完成') if any(m.role == 'tool' for m in messages) else call('request_user_input', QUESTION))
    repository = Repository()
    repository.save(SessionContext(session_id='s', user_id='u', profile_id='p', session_meta={'runtime_mode': 'harness_v1'}))
    router = build_harness_router(repository, Facts(), None, runtime)
    endpoint = next(route.endpoint for route in router.routes if route.path.endswith('/harness/events'))
    class Request:
        headers = {}
        async def is_disconnected(self):
            return False
    rid = runtime.create('s', 'u', 'p', '规划', policy())
    advance(runtime, rid, 4)
    async def read_one(after):
        response = await endpoint('s', Request(), 'u', 'p', after)
        item = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return int(item.splitlines()[0].removeprefix('id: '))
    cursor = asyncio.run(read_one(0))
    assert runtime.store.get(rid)['status'] not in {'failed', 'cancelled'}
    replay = asyncio.run(read_one(cursor))
    assert replay > cursor
    request = next(iter(runtime.store.get(rid)['inputs'].values()))
    runtime.answer(request['id'], AnswerSubmission(user_id='u', profile_id='p', submission_id='answer', expected_version=1, text='高二'))
    advance(runtime, rid, 5)
    assert runtime.store.get(rid)['status'] == 'completed'
    runtime.close()
