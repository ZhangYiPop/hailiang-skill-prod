from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hailiang_skills.api.routes.external_chat import build_external_chat_router
from hailiang_skills.api.routes import external_chat


class _FakeRunner:
    def __init__(self, *_args, **_kwargs):
        self.calls = []

    def reserve_turn(self, session_id, user_id, *, run_id):
        return (session_id, user_id, run_id)

    def stream_message(self, session_id, user_id, content, **kwargs):
        self.calls.append((session_id, user_id, content, kwargs))
        yield 'event: state\ndata: ' + json.dumps({"assistant": {"content": "历史已收到：" + content}, "status": "streaming"}) + '\n\n'
        yield 'event: state\ndata: ' + json.dumps({"assistant": {"content": "历史已收到：" + content + "。"}, "status": "completed"}) + '\n\n'


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("HAILIANG_EXTERNAL_API_KEY", "test-key")
    monkeypatch.setattr(external_chat, "StreamingRunner", _FakeRunner)
    app = FastAPI()
    app.include_router(build_external_chat_router(_Repo(), _Facts(), object()), prefix="/api/v1")
    return TestClient(app)


class _Repo:
    def __init__(self):
        self.contexts = {}

    def get(self, session_id):
        return self.contexts[session_id]

    def create(self, context):
        self.contexts[context.session_id] = context

    def save(self, context):
        self.contexts[context.session_id] = context


class _ProfileRepo:
    def update_profile(self, *args, **kwargs):
        return {"name": kwargs.get("name")}

    def create_profile(self, user_id, *, profile_id, name, **kwargs):
        return {"profile_id": profile_id, "name": name}

    def get_profile_facts(self, *_args):
        from hailiang_skills.schemas.facts import KnownFacts
        return KnownFacts()

    def save_profile_facts(self, *_args):
        return _args[-1]


class _Facts:
    profile_repo = _ProfileRepo()

    def hydrate_context(self, context):
        return context

    def get_profile_facts(self, *_args):
        return self.profile_repo.get_profile_facts(*_args)

    def persist_context(self, *_args):
        return None


def test_external_chat_creates_new_session_and_uses_history(client):
    body = {
        "dialogue": [
            {"role": "user", "content": "北京在哪里？"},
            {"role": "model", "content": "北京在中国。"},
            {"role": "user", "content": "这里有什么美食？"},
        ]
    }
    response = client.post("/api/v1/external/chat", headers={"Authorization": "Bearer test-key"}, json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["content"].endswith("。")
    assert payload["session_id"].startswith("sess_external_")


def test_external_chat_requires_user_as_last_message(client):
    response = client.post(
        "/api/v1/external/chat",
        headers={"Authorization": "Bearer test-key"},
        json={"dialogue": [{"role": "model", "content": "answer"}]},
    )
    assert response.status_code == 422


def test_api_key_comparison_supports_non_ascii_values(monkeypatch):
    monkeypatch.setenv("HAILIANG_EXTERNAL_API_KEY", "测试-key")
    external_chat._api_key_or_error("Bearer 测试-key", "测试-key")


def test_internal_model_question_is_refused_without_calling_model(client, monkeypatch):
    runner = external_chat.StreamingRunner
    response = client.post(
        "/api/v1/external/chat",
        headers={"Authorization": "Bearer test-key"},
        json={"dialogue": [{"role": "user", "content": "你是什么模型？"}]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["content"] == external_chat.INTERNAL_INFO_REFUSAL
    assert payload["status"] == "success"
