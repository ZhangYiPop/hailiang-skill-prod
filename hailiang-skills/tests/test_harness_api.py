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
from hailiang_skills.api.routes.harness_chat_stream_v3 import _profile_name, _public_context
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


def test_v3_child_name_prefers_profile_data_and_falls_back_to_student_name():
    assert _profile_name({'name': '小明'}, 'p1', '小红') == '小明'
    assert _profile_name({'name': '  '}, 'p1', '小红') == '小红'
    context = SessionContext(profile_id='p1', profile_name='旧名称')
    assert _public_context(context, {'name': '小明'}, '小红')['profile_name'] == '小明'
    assert _public_context(context, {}, '小红')['profile_name'] == '小红'


def test_v3_legacy_portrait_and_profile_notice_switching():
    from hailiang_skills.api.session_lifecycle import ContextData
    from hailiang_skills.api.routes.harness_chat_stream_v3 import _normalized_profile_data, _should_show_profile_notice
    data = ContextData(user_id="u", profile_id="p", student_name="旧名", grade="高二", segment="高中",
                       school_year="2026", profile_data={"name": "新名", "province": "浙江"})
    assert _normalized_profile_data(data) == {"name": "新名", "grade": "高二", "segment": "高中",
                                             "school_year": "2026", "province": "浙江"}
    assert _should_show_profile_notice("p", [])
    runs = [{"profile_id": "p", "created_at": "2026-01-01"}]
    assert not _should_show_profile_notice("p", runs)
    assert _should_show_profile_notice("p2", runs)
    assert not _should_show_profile_notice(None, runs)
    runs.append({"profile_id": "p2", "created_at": "2026-01-02"})
    assert _should_show_profile_notice("p", runs)


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


def test_debug_trace_includes_execution_diagnostics_and_stays_private(tmp_path, monkeypatch):
    import hailiang_skills.api.routes.harness as api
    monkeypatch.setattr(api, 'capture_policy', lambda *_: policy())
    runtime = make_runtime(tmp_path, lambda *_: call('finalize_response', {}))
    repository = Repository()
    repository.save(SessionContext(session_id='debug', user_id='u', session_meta={'runtime_mode': 'harness_v1', 'workbench_debug_session_id': 'dbg_1'}))
    app = FastAPI()
    app.include_router(build_harness_router(repository, Facts(), None, runtime), prefix='/api/v2')
    rid = runtime.create('debug', 'u', None, '测试诊断', policy())
    with TestClient(app) as client:
        response = client.get(f'/api/v2/sessions/debug/harness/runs/{rid}/trace?user_id=u')
        assert response.status_code == 200
        diagnostics = response.json()['execution_diagnostics']
        assert diagnostics['worker_wait_ms'] is not None
        assert diagnostics['phases']
        assert 'execution_diagnostics' not in client.get(f'/api/v2/sessions/debug/harness?user_id=u').text
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


def test_v3_minimal_chat_entry_persists_portrait_and_answers_without_client_versions(tmp_path, monkeypatch):
    import json
    import hailiang_skills.api.routes.harness_chat_stream_v3 as v3
    from hailiang_skills.api.routes.harness_chat_stream_v3 import build_harness_chat_stream_v3_router
    from hailiang_skills.schemas.facts import KnownFacts

    class Profiles:
        def __init__(self):
            self.items = {}
            self.facts = {}
        def list_profiles(self, user_id):
            return [deepcopy(value) for value in self.items.values() if value['user_id'] == user_id]
        def get_profile_by_id(self, profile_id):
            if profile_id not in self.items:
                raise KeyError(profile_id)
            return deepcopy(self.items[profile_id])
        def create_profile(self, user_id, *, profile_id, name, shared_facts_initialized=False, **_):
            item = {'profile_id': profile_id, 'name': name, 'user_id': user_id}
            self.items[profile_id] = item
            return deepcopy(item)
        def update_profile(self, user_id, profile_id, *, name=None, **_):
            item = self.items[profile_id]
            assert item['user_id'] == user_id
            if name:
                item['name'] = name
            return deepcopy(item)
        def get_profile_facts(self, user_id, profile_id):
            return self.facts.setdefault((user_id, profile_id), KnownFacts())
        def save_profile_facts(self, user_id, profile_id, facts):
            self.facts[(user_id, profile_id)] = facts
            return facts

    class V3Facts(Facts):
        profile_repo = Profiles()
        def get_profile_facts(self, user_id, profile_id):
            return self.profile_repo.get_profile_facts(user_id, profile_id)

    frozen = policy()
    frozen['team_id'] = 'team-a'
    monkeypatch.setattr(v3, 'capture_policy', lambda *_: deepcopy(frozen))
    monkeypatch.setenv('HAILIANG_HARNESS_SSE_WAIT_SECONDS', '1')
    monkeypatch.setenv('HAILIANG_HARNESS_SSE_PING_SECONDS', '0.05')
    expert = SimpleNamespace(name='团队协调专家')
    team = SimpleNamespace(team_id='team-a', coordinator_expert_id='expert-a', member_expert_ids=('expert-a',))
    orchestrator = SimpleNamespace(
        expert_registry=SimpleNamespace(get=lambda key: expert if key == 'expert-a' else None),
        expert_team_registry=SimpleNamespace(get=lambda key: team if key == 'team-a' else None),
    )
    runtime = make_runtime(tmp_path, lambda messages, *_: AssistantTurnResult(final_text='完成') if any(m.role == 'tool' for m in messages) else call('request_user_input', QUESTION))
    app = FastAPI()
    app.include_router(build_harness_chat_stream_v3_router(Repository(), V3Facts(), orchestrator, runtime), prefix='/api/v3')
    body = {
        'session_id': 's', 'run_id': 'client-1',
        'input': json.dumps({'action': 'chat', 'content': '请规划', 'expert_team_id': 'team-a'}, ensure_ascii=False),
        'context_data': {'user_id': 'u', 'profile_id': 'p', 'student_name': '旧姓名', 'grade': '高二', 'segment': '高中', 'school_year': '2026', 'profile_data': {'name': '小明', 'province': '浙江', 'recent_exam': {'total_score': 620}}},
    }
    with TestClient(app) as client:
        response = client.post('/api/v3/sessions/chat/stream', json=body)
        assert response.status_code == 200
        assert 'event: state' in response.text and '"protocol": "hailiang.sse.v3"' in response.text
        assert 'event: ping\ndata: {}\n\n' in response.text
        assert '"state"' in response.text and '"input_groups"' in response.text
        assert '"snapshot"' not in response.text
        assert '"client_run_id"' not in response.text
        assert '"harness_run_id"' not in response.text
        assert '"run_id": "client-1"' in response.text
        assert '"assistant_message_id": "msg_' in response.text
        assert 'msg_harness_' not in response.text
        assert '"should_store_snapshot": true' in response.text
        frame = json.loads(next(line.removeprefix('data: ') for line in response.text.splitlines() if line.startswith('data: ')))
        assert frame['run_id'] == 'client-1'
        assistant_message_id = frame['assistant_message_id']
        assert frame['state']['nodes'][0]['id'] == 'node_1'
        assert 'root_task_id' not in frame['state']
        run = runtime.store.active('s')
        assert run
        facts = runtime.store.scoped_facts('u', 'profile', 'p')
        assert facts['grade']['value'] == '高二'
        assert facts['school_stage']['value'] == '高中'
        assert facts['profile.school_year']['value'] == '2026'
        assert facts['profile.name']['value'] == '小明'
        assert response.text.count('本轮回答将结合') == 1
        assert facts['profile.province']['value'] == '浙江'
        assert facts['exam_score']['value'] == 620
        assert frame['expert_team_id'] == 'team-a'
        assert frame['expert_team_ids'] == ['team-a']
        assert frame['team_orchestration_version'] == 'single_team_v1'
        assert frame['state']['expert_team_ids'] == ['team-a']
        assert frame['state']['team_orchestration_version'] == 'single_team_v1'
        # Same client run is an idempotent retry, not a second dispatch.
        replay = client.post('/api/v3/sessions/chat/stream', json=body)
        assert replay.status_code == 200
        assert '本轮回答将结合' not in replay.text
        assert len(runtime.store.runs('s')) == 1
        advance(runtime, run['run_id'], 4)
        request = next(iter(runtime.store.get(run['run_id'])['inputs'].values()))
        # A fresh BFF request ID still restores the same task via the stable
        # assistant bubble ID; it must not require the original chat run_id.
        resumed = client.post('/api/v3/sessions/chat/stream', json={
            'session_id': 's', 'run_id': 'resume-1',
            'input': json.dumps({'action': 'resume', 'assistant_message_id': assistant_message_id, 'expert_team_id': 'team-a'}, ensure_ascii=False),
            'context_data': {'user_id': 'u', 'profile_id': 'p'},
        })
        assert resumed.status_code == 200
        assert f'"run_id": "resume-1"' in resumed.text
        assert f'"assistant_message_id": "{assistant_message_id}"' in resumed.text
        answer = {
            'session_id': 's', 'run_id': 'answer-1',
            # Legacy V3 callers omit card version and submission ID.  The
            # adapter derives both without relaxing strict answer semantics.
            'input': json.dumps({'action': 'answer_input_request', 'assistant_message_id': assistant_message_id, 'request_id': request['id'], 'text': '高二', 'expert_team_id': 'team-a'}, ensure_ascii=False),
            'context_data': {'user_id': 'u', 'profile_id': 'p'},
        }
        accepted = client.post('/api/v3/sessions/chat/stream', json=answer)
        assert accepted.status_code == 200
        stored = runtime.store.get(run['run_id'])['inputs'][request['id']]
        assert stored['status'] == 'answered'
        # Candidate-test semantics are strict: a resolved card cannot be
        # corrected by sending a later public V3 request.
        correction = deepcopy(answer)
        correction['run_id'] = 'answer-2'
        correction['input'] = json.dumps({'action': 'answer_input_request', 'assistant_message_id': assistant_message_id, 'request_id': request['id'], 'submission_id': 'answer-2', 'expected_version': request['version'] + 1, 'text': '高三', 'expert_team_id': 'team-a'}, ensure_ascii=False)
        assert client.post('/api/v3/sessions/chat/stream', json=correction).status_code == 409
        assert runtime.store.scoped_facts('u', 'profile', 'p')['grade']['value'] == '高二'
        mismatch = deepcopy(answer)
        mismatch['run_id'] = 'answer-mismatch'
        mismatch['input'] = json.dumps({'action': 'answer_input_request', 'assistant_message_id': assistant_message_id, 'request_id': request['id'], 'submission_id': 'answer-mismatch', 'expected_version': request['version'] + 1, 'text': '高三', 'expert_team_id': 'team-b'}, ensure_ascii=False)
        assert client.post('/api/v3/sessions/chat/stream', json=mismatch).status_code == 409
        missing_team = deepcopy(body)
        missing_team['run_id'] = 'client-missing-team'
        missing_team['input'] = json.dumps({'action': 'chat', 'content': '请规划'}, ensure_ascii=False)
        assert client.post('/api/v3/sessions/chat/stream', json=missing_team).status_code == 422
        stop = {
            'session_id': 's', 'run_id': 'client-1',
            'input': json.dumps({'action': 'stop', 'source': 'composer'}, ensure_ascii=False),
            'context_data': {'user_id': 'u', 'profile_id': 'p'},
        }
        stopped = client.post('/api/v3/sessions/chat/stream', json=stop)
        assert stopped.status_code == 200
        assert runtime.store.get(run['run_id'])['status'] == 'cancelled'
        # The canonical selector form is a list; a single selected team stays
        # compatible with the current single-team orchestration head.
        list_selector = deepcopy(body)
        list_selector['run_id'] = 'client-list-selector'
        list_selector['input'] = json.dumps({'action': 'chat', 'content': '请规划另一个问题', 'expert_team_id': ['team-a']}, ensure_ascii=False)
        list_response = client.post('/api/v3/sessions/chat/stream', json=list_selector)
        assert list_response.status_code == 200
        list_frame = json.loads(next(line.removeprefix('data: ') for line in list_response.text.splitlines() if line.startswith('data: ')))
        assert list_frame['expert_team_ids'] == ['team-a']
        multi_selector = deepcopy(body)
        multi_selector['run_id'] = 'client-multi-selector'
        multi_selector['input'] = json.dumps({'action': 'chat', 'content': '请规划多个团队', 'expert_team_id': ['team-a', 'team-b']}, ensure_ascii=False)
        multi_response = client.post('/api/v3/sessions/chat/stream', json=multi_selector)
        assert multi_response.status_code == 422
        assert multi_response.json()['detail'] == 'MULTI_EXPERT_TEAMS_NOT_ENABLED'
    runtime.close()


def test_v3_legacy_sse_task_simulation(tmp_path, monkeypatch):
    import json
    import hailiang_skills.api.routes.harness_chat_stream_v3 as v3
    from hailiang_skills.api.routes.harness_chat_stream_v3 import build_harness_chat_stream_v3_router
    from hailiang_skills.schemas.facts import KnownFacts
    class Profiles:
        def __init__(self):
            self.items = {}
            self.facts = {}
        def list_profiles(self, user_id):
            return [deepcopy(value) for value in self.items.values() if value['user_id'] == user_id]
        def get_profile_by_id(self, profile_id):
            if profile_id not in self.items:
                raise KeyError(profile_id)
            return deepcopy(self.items[profile_id])
        def create_profile(self, user_id, *, profile_id, name, shared_facts_initialized=False, **_):
            item = {'profile_id': profile_id, 'name': name, 'user_id': user_id}
            self.items[profile_id] = item
            return deepcopy(item)
        def update_profile(self, user_id, profile_id, *, name=None, **_):
            item = self.items[profile_id]
            assert item['user_id'] == user_id
            if name:
                item['name'] = name
            return deepcopy(item)
        def get_profile_facts(self, user_id, profile_id):
            return self.facts.setdefault((user_id, profile_id), KnownFacts())
        def save_profile_facts(self, user_id, profile_id, facts):
            self.facts[(user_id, profile_id)] = facts
            return facts

    class V3Facts(Facts):
        profile_repo = Profiles()
        def get_profile_facts(self, user_id, profile_id):
            return self.profile_repo.get_profile_facts(user_id, profile_id)

    frozen = policy()
    frozen['team_id'] = 'team-a'
    monkeypatch.setattr(v3, 'capture_policy', lambda *_: deepcopy(frozen))
    monkeypatch.setenv('HAILIANG_HARNESS_SSE_WAIT_SECONDS', '1')
    monkeypatch.setenv('HAILIANG_HARNESS_SSE_PING_SECONDS', '0.05')
    expert = SimpleNamespace(name='团队协调专家')
    team = SimpleNamespace(team_id='team-a', coordinator_expert_id='expert-a', member_expert_ids=('expert-a',))
    orchestrator = SimpleNamespace(
        expert_registry=SimpleNamespace(get=lambda key: expert if key == 'expert-a' else None),
        expert_team_registry=SimpleNamespace(get=lambda key: team if key == 'team-a' else None),
    )
    runtime = make_runtime(tmp_path, lambda messages, *_: AssistantTurnResult(final_text='完成') if any(m.role == 'tool' for m in messages) else call('request_user_input', QUESTION))
    app = FastAPI()
    app.include_router(build_harness_chat_stream_v3_router(Repository(), V3Facts(), orchestrator, runtime), prefix='/api/v3')
    body = {
        'session_id': 'simulation', 'run_id': 'client-1',
        'input': json.dumps({'action': 'chat', 'content': '请规划', 'expert_team_id': 'team-a'}, ensure_ascii=False),
        'context_data': {'user_id': 'u', 'profile_id': 'p', 'student_name': '旧姓名', 'grade': '高二', 'segment': '高中', 'school_year': '2026', 'profile_data': {'name': '小明', 'province': '浙江', 'recent_exam': {'total_score': 620}}},
    }
    body['context_data'] = {'user_id': '99272', 'profile_id': '336650', 'student_name': '浙江人1', 'grade': '高二', 'segment': '高中', 'school_year': '2026'}
    def task(client, number, profile, name):
        body['run_id'] = f'client-{number}'
        body['context_data']['profile_id'] = profile
        body['context_data']['student_name'] = name
        response = client.post('/api/v3/sessions/chat/stream', json=body)
        assert response.status_code == 200, response.text
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ') and line != 'data: {}']
        notices = [frame['context_notice'] for frame in frames if frame.get('context_notice')]
        active = runtime.store.active('simulation')
        assert active
        runtime.cancel(active['run_id'])
        return notices
    with TestClient(app) as client:
        first = task(client, 1, '336650', '浙江人1')
        followup = task(client, 2, '336650', '浙江人1')
        switch = task(client, 3, 'other', '另一个孩子')
        followup2 = task(client, 4, 'other', '另一个孩子')
        assert len(first) == 1 and '浙江人1' in first[0]['text']
        assert followup == []
        assert len(switch) == 1 and '另一个孩子' in switch[0]['text']
        assert followup2 == []
        facts = runtime.store.scoped_facts('99272', 'profile', '336650')
        assert {key: facts[key]['value'] for key in ('profile.name', 'grade', 'school_stage', 'profile.school_year')} == {'profile.name': '浙江人1', 'grade': '高二', 'school_stage': '高中', 'profile.school_year': '2026'}
        from hailiang_skills.harness.context_policy import project_facts
        selected, _ = project_facts({'goal': '请规划', 'policy': frozen}, {'goal': '请规划', 'skill_id': None, 'expert_id': 'a'}, facts)
        assert {item['key'] for item in selected.values()} >= {'profile.name', 'grade', 'school_stage', 'profile.school_year'}
    runtime.close()


def test_v3_form_answers_are_isolated_between_children(tmp_path, monkeypatch):
    import json
    import hailiang_skills.api.routes.harness_chat_stream_v3 as v3
    from hailiang_skills.api.routes.harness_chat_stream_v3 import build_harness_chat_stream_v3_router
    from hailiang_skills.schemas.facts import KnownFacts
    class Profiles:
        def __init__(self):
            self.items = {}
            self.facts = {}
        def list_profiles(self, user_id):
            return [deepcopy(value) for value in self.items.values() if value['user_id'] == user_id]
        def get_profile_by_id(self, profile_id):
            if profile_id not in self.items:
                raise KeyError(profile_id)
            return deepcopy(self.items[profile_id])
        def create_profile(self, user_id, *, profile_id, name, shared_facts_initialized=False, **_):
            item = {'profile_id': profile_id, 'name': name, 'user_id': user_id}
            self.items[profile_id] = item
            return deepcopy(item)
        def update_profile(self, user_id, profile_id, *, name=None, **_):
            item = self.items[profile_id]
            assert item['user_id'] == user_id
            if name:
                item['name'] = name
            return deepcopy(item)
        def get_profile_facts(self, user_id, profile_id):
            return self.facts.setdefault((user_id, profile_id), KnownFacts())
        def save_profile_facts(self, user_id, profile_id, facts):
            self.facts[(user_id, profile_id)] = facts
            return facts

    class V3Facts(Facts):
        profile_repo = Profiles()
        def get_profile_facts(self, user_id, profile_id):
            return self.profile_repo.get_profile_facts(user_id, profile_id)

    frozen = policy()
    frozen['team_id'] = 'team-a'
    monkeypatch.setattr(v3, 'capture_policy', lambda *_: deepcopy(frozen))
    monkeypatch.setenv('HAILIANG_HARNESS_SSE_WAIT_SECONDS', '1')
    monkeypatch.setenv('HAILIANG_HARNESS_SSE_PING_SECONDS', '0.05')
    expert = SimpleNamespace(name='团队协调专家')
    team = SimpleNamespace(team_id='team-a', coordinator_expert_id='expert-a', member_expert_ids=('expert-a',))
    orchestrator = SimpleNamespace(
        expert_registry=SimpleNamespace(get=lambda key: expert if key == 'expert-a' else None),
        expert_team_registry=SimpleNamespace(get=lambda key: team if key == 'team-a' else None),
    )
    runtime = make_runtime(tmp_path, lambda messages, *_: AssistantTurnResult(final_text='完成') if any(m.role == 'tool' for m in messages) else call('request_user_input', QUESTION))
    app = FastAPI()
    app.include_router(build_harness_chat_stream_v3_router(Repository(), V3Facts(), orchestrator, runtime), prefix='/api/v3')
    body = {
        'session_id': 's', 'run_id': 'client-1',
        'input': json.dumps({'action': 'chat', 'content': '请规划', 'expert_team_id': 'team-a'}, ensure_ascii=False),
        'context_data': {'user_id': 'u', 'profile_id': 'p', 'student_name': '旧姓名', 'grade': '高二', 'segment': '高中', 'school_year': '2026', 'profile_data': {'name': '小明', 'province': '浙江', 'recent_exam': {'total_score': 620}}},
    }
    body['context_data'] = {'user_id': 'u', 'profile_id': 'p', 'student_name': '孩子甲'}
    with TestClient(app) as client:
        first = client.post('/api/v3/sessions/chat/stream', json=body)
        assert first.status_code == 200
        run = runtime.store.active('s')
        message_id = run['assistant_message_id']
        def add_question(run, changes, db):
            root = run['tasks'][run['root_task_id']]
            tool = {'id': 'form-tool', 'name': 'request_user_input', 'arguments': {'title': '补充', 'fields': [{'key': 'score', 'label': '成绩', 'scope': 'session'}]}, 'task_id': root['id'], 'call_id': 'form-call', 'status': 'running', 'attempts': 1, 'created_at': None, 'started_at': None, 'ended_at': None}
            run['tools'][tool['id']] = tool
            runtime._request(run, root, tool, changes, db)
        runtime.store.mutate(run['run_id'], add_question)
        question = next(iter(runtime.store.get(run['run_id'])['inputs'].values()))
        answer = {'session_id': 's', 'run_id': 'answer-a', 'input': json.dumps({'action': 'answer_input_request', 'assistant_message_id': message_id, 'request_id': question['id'], 'text': '甲的成绩87', 'expert_team_id': 'team-a'}, ensure_ascii=False), 'context_data': body['context_data']}
        result = client.post('/api/v3/sessions/chat/stream', json=answer)
        assert result.status_code == 200, result.text
        a_run = runtime.store.get(run['run_id'])
        a_facts = runtime.facts(a_run, a_run['tasks'][a_run['root_task_id']])
        assert any(x.get('value') == '甲的成绩87' for x in a_facts.values())
        runtime.cancel(run['run_id'])
        body['run_id'] = 'child-b'
        body['context_data'] = {'user_id': 'u', 'profile_id': 'p2', 'student_name': '孩子乙'}
        response = client.post('/api/v3/sessions/chat/stream', json=body)
        assert response.status_code == 200, response.text
        b = runtime.store.active('s')
        b_facts = runtime.facts(b, b['tasks'][b['root_task_id']])
        assert all(x.get('value') != '甲的成绩87' for x in b_facts.values())
        runtime.cancel(b['run_id'])
        body['run_id'] = 'return-a'
        body['context_data'] = {'user_id': 'u', 'profile_id': 'p', 'student_name': '孩子甲'}
        response = client.post('/api/v3/sessions/chat/stream', json=body)
        assert response.status_code == 200, response.text
        again = runtime.store.active('s')
        again_facts = runtime.facts(again, again['tasks'][again['root_task_id']])
        assert any(x.get('value') == '甲的成绩87' for x in again_facts.values())
    runtime.close()
