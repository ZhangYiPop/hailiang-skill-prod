from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from hailiang_skills.api.routes import diagnostics
from hailiang_skills.storage.database import Base, WorkbenchDebugSessionRow


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
    assert payload["errors"] == [{
        "source": "http_request",
        "timestamp": None,
        "request_id": "req_001",
        "run_id": "run_001",
        "status_code": 422,
        "code": "HTTP_422",
        "message": None,
        "detail": None,
        "error": None,
        "upstream_detail": None,
    }]


def test_session_diagnostics_includes_test_only_v3_harness_runtime_trace(monkeypatch) -> None:
    run = {
        "run_id": "harness_001", "session_id": "sess_001", "profile_id": "profile_001",
        "status": "running", "created_at": "2026-10-25T10:00:00+08:00", "updated_at": "2026-10-25T10:00:01+08:00",
        "wire_protocol": "hailiang.sse.v3", "root_task_id": "task_001", "inputs": {},
        "tasks": {"task_001": {"id": "task_001", "kind": "main", "status": "running", "goal": "敏感问题", "trace": [], "depends_on": [], "created_at": "2026-10-25T10:00:00+08:00", "started_at": None, "ended_at": None}},
        "tools": {"tool_001": {"id": "tool_001", "task_id": "task_001", "name": "delegate_tasks", "status": "completed", "arguments": {"tasks": []}, "created_at": "2026-10-25T10:00:00+08:00", "started_at": None, "ended_at": None}},
        "policy": {"team_id": "team_001", "orchestrated": True, "identity_gate": "text", "direct_expert_id": "", "settings": {"max_parallel": 3},
                   "orchestrator": {"members": [{"expert_id": "expert_001"}], "consultation_policy": {"mode": "consult"}},
                   "snapshot": {"deployment_id": "deploy_001", "package_hash": "hash_001", "root": {"object_type": "expert_team", "object_key": "team_001"},
                                "orchestration_rules": {"orchestration_revision_id": "orch_001", "revision_no": 3, "content_hash": "rules_hash", "rule_versions": {"orchestrate_head": {"version": "v1.1", "state": "current"}}},
                                "entries": [{"object_type": "expert_team", "object_key": "team_001", "release_id": "release_001", "version": "v1"}]}},
    }
    harness = SimpleNamespace(store=SimpleNamespace(runs=lambda session_id: [run]))
    monkeypatch.setattr(diagnostics, "deployment_environment", lambda: "test-smoke")
    redacted = diagnostics._harness_runtime_trace(harness, "sess_001", run_id=None, limit=10, include_content=False)
    assert redacted[0]["configuration"]["expert_team"] == {"team_id": "team_001", "release_id": "release_001", "version": "v1"}
    assert redacted[0]["configuration"]["orchestration_rules"]["rule_versions"]["orchestrate_head"]["version"] == "v1.1"
    assert redacted[0]["orchestration"]["enabled"] is True
    assessment = redacted[0]["execution_assessment"]
    assert assessment["parallelism"]["configured_max_parallel"] == 3
    assert assessment["final_reply"]["streaming_enabled"] is True
    assert assessment["findings"][0]["code"] == "HISTORICAL_WAIT_TRACE_UNAVAILABLE"
    assert "goal" not in redacted[0]["tasks"][0]
    detailed = diagnostics._harness_runtime_trace(harness, "sess_001", run_id="harness_001", limit=10, include_content=True)
    assert detailed[0]["tasks"][0]["goal"] == "敏感问题"


def test_harness_execution_assessment_separates_synthesis_from_capacity_wait() -> None:
    run = {
        "root_task_id": "root", "wire_protocol": "hailiang.sse.v3",
        "tasks": {
            "root": {"id": "root", "kind": "main", "status": "waiting_for_dependency"},
            "expert_a": {"id": "expert_a", "kind": "expert", "expert_id": "a", "status": "running", "depends_on": []},
            "expert_b": {"id": "expert_b", "kind": "expert", "expert_id": "b", "status": "queued", "depends_on": []},
        },
    }
    execution = {
        "max_parallel": 1, "running_task_count": 1, "worker_wait_ms": 8,
        "phases": [{"id": "expert_b", "queue_reason": "run_parallelism_limit"}],
        "model_phases": [], "first_output_at": None, "first_output_ms": None,
    }
    assessment = diagnostics._harness_execution_assessment(run, execution)
    codes = {item["code"] for item in assessment["findings"]}
    assert codes == {"RUN_PARALLELISM_LIMIT_REACHED", "SYNTHESIS_BARRIER"}
    assert assessment["parallelism"]["independent_expert_task_count"] == 2


def test_revision_test_diagnostics_exposes_frozen_candidate_trace_with_content_opt_in(monkeypatch, tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'diagnostics.sqlite3'}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(WorkbenchDebugSessionRow(
            debug_session_id="dbg_candidate_001",
            revision_id="rev_candidate_001",
            snapshot={
                "root": {"object_type": "expert_team", "object_key": "team_candidate"},
                "orchestration_rules": {
                    "orchestration_revision_id": "orch_candidate", "revision_no": 2,
                    "content_hash": "rules_hash", "rule_versions": {"orchestrate_head": {"version": "1.1"}},
                },
                "entries": [{"object_type": "expert_team", "object_key": "team_candidate",
                             "revision_id": "rev_team", "release_id": None, "version": "candidate"}],
            },
            runtime_context={"session_meta": {"active_expert_id": "expert_candidate", "_candidate_branch_version": 3}},
            transcript=[{"role": "user", "content": "测试问题"}],
            trace=[{"turn": 1, "events": [{"event_type": "candidate_turn_started", "payload": {"content": "测试问题"}}],
                    "debug": {"expert": {"object_key": "expert_candidate"}}}],
        ))
        db.commit()

    monkeypatch.setenv("HAILIANG_SECURITY_ADMIN_TOKEN", "diagnostic-token")
    app = FastAPI()
    app.include_router(diagnostics.build_diagnostics_router(_Repository(), engine), prefix="/api/v1")
    client = TestClient(app)

    denied = client.post("/api/v1/operations/diagnostics/revision-tests/query", json={"debug_session_id": "dbg_candidate_001"})
    assert denied.status_code == 403
    redacted = client.post(
        "/api/v1/operations/diagnostics/revision-tests/query",
        json={"debug_session_id": "dbg_candidate_001"},
        headers={"X-Security-Admin-Token": "diagnostic-token"},
    )
    assert redacted.status_code == 200
    trace = redacted.json()["candidate_revision_test_trace"]
    assert trace["execution_runtime"] == "workbench_candidate"
    assert trace["configuration"]["expert_team"]["team_id"] == "team_candidate"
    assert trace["configuration"]["orchestration_rules"]["rule_versions"]["orchestrate_head"]["version"] == "1.1"
    assert "transcript" not in trace

    detailed = client.post(
        "/api/v1/operations/diagnostics/revision-tests/query",
        json={"debug_session_id": "dbg_candidate_001", "include_content": True},
        headers={"X-Security-Admin-Token": "diagnostic-token"},
    )
    assert detailed.json()["candidate_revision_test_trace"]["transcript"][0]["content"] == "测试问题"


def test_session_diagnostics_projects_confirmed_output_truncation(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(diagnostics, "read_events", lambda session_id: [{
        "event_id": "evt_truncated",
        "event_type": "model_output_truncated",
        "timestamp": "2026-09-09T10:00:00+08:00",
        "payload": {
            "source": "expert_agent",
            "expert_id": "study_abroad_consultant",
            "truncation_reason_code": "upstream_finish_reason_length",
            "truncation_reason": "上游模型以输出长度上限结束",
            "finish_reason": "length",
            "configured_max_tokens": 384000,
            "output_tokens": 384000,
            "returned_chars": 1000,
        },
    }])

    response = client.post(
        "/api/v1/operations/diagnostics/sessions/query",
        json={"session_id": "sess_001"},
        headers={"X-Security-Admin-Token": "diagnostic-token"},
    )

    assert response.status_code == 200
    assert response.json()["output_truncations"] == [{
        "source": "session_event",
        "timestamp": "2026-09-09T10:00:00+08:00",
        "run_id": None,
        "expert_id": "study_abroad_consultant",
        "expert_turn_id": None,
        "model": None,
        "request_purpose": None,
        "reason_code": "upstream_finish_reason_length",
        "reason": "上游模型以输出长度上限结束",
        "finish_reason": "length",
        "configured_max_tokens": 384000,
        "configured_reply_max_chars": None,
        "output_tokens": 384000,
        "received_chars": None,
        "returned_chars": 1000,
    }]


def test_session_diagnostics_surfaces_skill_unavailable_fallback(monkeypatch, tmp_path) -> None:
    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(diagnostics, "read_events", lambda session_id: [{
        "event_id": "evt_skill_fallback",
        "event_type": "expert_skill_unavailable_fallback",
        "timestamp": "2026-09-15T10:00:00+08:00",
        "payload": {
            "skill_id": "score_improve",
            "code": "EXPERT_SKILL_UNAVAILABLE_FALLBACK",
            "message": "指定 Skill 当前不可用，已由专家直接兜底",
        },
    }])

    response = client.post(
        "/api/v1/operations/diagnostics/sessions/query",
        json={"session_id": "sess_001"},
        headers={"X-Security-Admin-Token": "diagnostic-token"},
    )

    assert response.status_code == 200
    assert any(
        item["event_type"] == "expert_skill_unavailable_fallback"
        and item["code"] == "EXPERT_SKILL_UNAVAILABLE_FALLBACK"
        and item["message"] == "指定 Skill 当前不可用，已由专家直接兜底"
        for item in response.json()["errors"]
    )


def test_candidate_script_trace_is_redacted_unless_content_is_requested() -> None:
    trace = {
        "debug": {
            "scripts": [{
                "path": "scripts/calculate.py",
                "stdin_payload": {"budget": 100000},
                "stdout": '{"total": 100000}',
                "stderr": "",
                "json_output": {"total": 100000},
                "return_value": {"currency": "CNY"},
            }]
        }
    }

    redacted = diagnostics._redact(trace, include_content=False)
    assert redacted["debug"]["scripts"][0]["stdout"] == "[OMITTED: set include_content=true]"
    assert redacted["debug"]["scripts"][0]["json_output"] == "[OMITTED: set include_content=true]"
    assert diagnostics._redact(trace, include_content=True) == trace


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
