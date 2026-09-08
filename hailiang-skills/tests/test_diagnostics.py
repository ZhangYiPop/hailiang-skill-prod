from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hailiang_skills.api.routes import diagnostics


class _Repository:
    def __init__(self) -> None:
        self.contexts = {
            "sess_001": SimpleNamespace(
                user_id="user_001",
                profile_id="profile_001",
                context_scope="profile",
                session_meta={"_active_branch_version": 4, "run_ledger": {"run_001": {"status": "completed"}}},
            )
        }

    def get(self, session_id: str):
        if session_id not in self.contexts:
            raise KeyError(session_id)
        return self.contexts[session_id]


def _client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("HAILIANG_SECURITY_ADMIN_TOKEN", "diagnostic-token")
    monkeypatch.setattr(diagnostics, "log_root", lambda: tmp_path)
    monkeypatch.setattr(diagnostics, "read_events", lambda session_id: [
        {"event_id": "evt_001", "event_type": "run_completed", "payload": {"content": "private reply", "status": "completed"}}
    ])
    (tmp_path / "http_requests.1.jsonl").write_text(
        json.dumps({"request_id": "req_001", "trace_id": "trace_001", "session_id": "sess_001", "run_id": "run_001", "status_code": 422}) + "\n",
        encoding="utf-8",
    )
    app = FastAPI()
    app.include_router(diagnostics.build_diagnostics_router(_Repository(), None), prefix="/api/v1")
    return TestClient(app)


def test_session_diagnostics_requires_admin_token(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    response = client.post("/api/v1/operations/diagnostics/sessions/query", json={"session_id": "sess_001"})
    assert response.status_code == 403


def test_session_diagnostics_redacts_content_by_default(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    response = client.post(
        "/api/v1/operations/diagnostics/sessions/query",
        json={"session_id": "sess_001"},
        headers={"X-Security-Admin-Token": "diagnostic-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["found"] is True
    assert payload["runs"] == [{"run_id": "run_001", "status": "completed"}]
    assert payload["events"][0]["payload"]["content"] == "[OMITTED: set include_content=true]"
    assert payload["http_requests"][0]["request_id"] == "req_001"


def test_request_diagnostics_handles_pre_session_failure(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    response = client.post(
        "/api/v1/operations/diagnostics/requests/query",
        json={"request_id": "req_001"},
        headers={"Authorization": "Bearer diagnostic-token"},
    )
    assert response.status_code == 200
    assert response.json()["http_requests"][0]["status_code"] == 422
    assert "before a session or run exists" in response.json()["hint"]
