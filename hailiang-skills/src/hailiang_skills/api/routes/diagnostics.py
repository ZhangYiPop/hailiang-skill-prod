"""Protected correlation lookup for production incident diagnosis.

The chat API deliberately returns a request id for *every* response.  This
router turns that id (or a session/run id when available) into a bounded,
auditable view of the records already written by the API.  It is intentionally
read-only and requires the same administrator credential as the security
quarantine endpoints.
"""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from hailiang_skills.core.deployment import log_root
from hailiang_skills.core.session_logging import (
    get_session_run_sse_log_path,
    get_session_sse_aggregate_log_path,
)
from hailiang_skills.storage.database import ChatRunRow, WorkbenchDebugSessionRow
from hailiang_skills.storage.event_store import read_events
from hailiang_skills.core.deployment import deployment_environment


_CONTENT_KEYS = {
    "content", "input", "output", "raw_sse", "reasoning", "messages",
    "payload", "prompt", "response", "tool_input", "tool_output", "stdout", "stderr",
    "stdin_payload", "json_output", "return_value", "execution_trace",
}
_SECRET_MARKERS = ("authorization", "password", "secret", "api_key", "cookie")


def _is_secret_key(key_lower: str) -> bool:
    if any(marker in key_lower for marker in _SECRET_MARKERS):
        return True
    # Token counters (for example ``output_tokens``) are diagnostic metadata,
    # not credentials. Keep them visible while still protecting actual token
    # fields such as access_token and refresh_token.
    return key_lower == "token" or key_lower.endswith("_token")


class SessionDiagnosticsInput(BaseModel):
    session_id: str = Field(min_length=1, max_length=160)
    run_id: str | None = Field(default=None, min_length=1, max_length=160)
    limit: int = Field(default=200, ge=1, le=1000)
    include_content: bool = False
    event_types: list[str] | None = Field(default=None, max_length=50)


class RunDiagnosticsInput(BaseModel):
    run_id: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=200, ge=1, le=1000)
    include_content: bool = False


class RequestDiagnosticsInput(BaseModel):
    request_id: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=100, ge=1, le=1000)
    include_content: bool = False


class RevisionTestDiagnosticsInput(BaseModel):
    """Operator lookup for one immutable Workbench candidate-test session."""

    debug_session_id: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=200, ge=1, le=1000)
    include_content: bool = False


def _authorize(token: str | None, authorization: str | None) -> None:
    expected = os.getenv("HAILIANG_SECURITY_ADMIN_TOKEN", "")
    bearer = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    candidate = token or bearer
    if not expected or not candidate or not hmac.compare_digest(candidate, expected):
        raise HTTPException(status_code=403, detail="security administrator access required")


def _safe_id(value: str, *, label: str) -> str:
    value = value.strip()
    if not value or len(value) > 160 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-." for char in value):
        raise HTTPException(status_code=422, detail=f"invalid {label}")
    return value


def _redact(value: Any, *, include_content: bool) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            key_lower = key.lower()
            if _is_secret_key(key_lower):
                result[key] = "[REDACTED]"
            # Event ``payload`` is a structured envelope, not response body
            # content itself. Preserve its safe fields so operators can see
            # finish/truncation reasons while nested content stays redacted.
            elif not include_content and key_lower in _CONTENT_KEYS and key_lower != "payload":
                result[key] = "[OMITTED: set include_content=true]"
            else:
                result[key] = _redact(item, include_content=include_content)
        return result
    if isinstance(value, list):
        return [_redact(item, include_content=include_content) for item in value]
    return value


def _read_jsonl(path: Path, *, predicate, limit: int, include_content: bool) -> list[dict[str, Any]]:
    """Read a bounded JSONL diagnostic file; malformed lines never break lookup."""
    if not path.is_file():
        return []
    matches: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and predicate(record):
                    matches.append(_redact(record, include_content=include_content))
    except OSError:
        return []
    return matches[-limit:]


def _http_records(*, field: str, value: str, limit: int, include_content: bool) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(log_root().glob("http_requests.*.jsonl")):
        records.extend(_read_jsonl(
            path,
            predicate=lambda item: str(item.get(field) or "") == value,
            limit=limit,
            include_content=include_content,
        ))
    records.sort(key=lambda item: str(item.get("timestamp_utc") or item.get("timestamp") or ""))
    return records[-limit:]


def _run_record(engine, run_id: str) -> dict[str, Any] | None:
    if engine is None:
        return None
    from sqlalchemy.orm import Session

    try:
        with Session(engine) as db:
            row = db.get(ChatRunRow, run_id)
            if row is None:
                return None
            return {
                "run_id": row.run_id,
                "session_id": row.session_id,
                "profile_id": row.profile_id or None,
                "branch_version": row.branch_version,
                "action": row.action,
                "status": row.status,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
    except Exception:
        # A diagnostic endpoint must still return file-backed correlation data
        # during a transient database incident.
        return None


def _session_events(session_id: str, *, limit: int, include_content: bool) -> tuple[list[dict[str, Any]], str]:
    try:
        events = read_events(session_id)
    except Exception:
        events = None
    if events is None:
        path = log_root() / "sessions" / session_id / "events.jsonl"
        return _read_jsonl(path, predicate=lambda _: True, limit=limit, include_content=include_content), "file"
    return [_redact(event, include_content=include_content) for event in events[-limit:]], "postgres"


def _candidate_trace(engine, session_id: str, *, limit: int, include_content: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Expose candidate-test evidence via the same session-id diagnostics API."""
    prefix = "revision_test_dbg_"
    if engine is None or not session_id.startswith(prefix):
        return [], []
    debug_session_id = "dbg_" + session_id[len(prefix):]
    from sqlalchemy.orm import Session

    try:
        with Session(engine) as db:
            row = db.get(WorkbenchDebugSessionRow, debug_session_id)
            if row is None:
                return [], []
            trace = list(row.trace or [])[-limit:]
    except Exception:
        return [], []
    events: list[dict[str, Any]] = []
    for turn in trace:
        if not isinstance(turn, dict):
            continue
        for event in turn.get("events") if isinstance(turn.get("events"), list) else []:
            if isinstance(event, dict):
                events.append(event)
    return _redact(trace, include_content=include_content), _redact(events[-limit:], include_content=include_content)


def _candidate_revision_test_trace(engine, debug_session_id: str, *, limit: int,
                                   include_content: bool) -> dict[str, Any] | None:
    """Return the durable candidate-test evidence in a V3-comparable shape.

    Candidate manual tests intentionally run against an unpublished immutable
    snapshot, so their evidence lives in ``workbench_debug_sessions`` rather
    than the formal session store.  Do not make operators reconstruct a turn
    from the Workbench UI or infer its frozen configuration from current data.
    """
    if engine is None:
        return None
    from sqlalchemy.orm import Session

    try:
        with Session(engine) as db:
            row = db.get(WorkbenchDebugSessionRow, debug_session_id)
            if row is None:
                return None
            snapshot = dict(row.snapshot or {})
            runtime_context = dict(row.runtime_context or {})
            trace = list(row.trace or [])[-limit:]
            transcript = list(row.transcript or [])[-(limit * 2):]
    except Exception:
        return None

    entries = [item for item in snapshot.get("entries", []) if isinstance(item, dict)]
    root = snapshot.get("root") if isinstance(snapshot.get("root"), dict) else {}
    root_entry = next((item for item in entries
                       if item.get("object_type") == root.get("object_type")
                       and item.get("object_key") == root.get("object_key")), {})
    meta = runtime_context.get("session_meta") if isinstance(runtime_context.get("session_meta"), dict) else {}
    team_id = str(meta.get("expert_team_id") or (root.get("object_key") if root.get("object_type") == "expert_team" else ""))
    team_entry = next((item for item in entries
                       if item.get("object_type") == "expert_team" and item.get("object_key") == team_id), {})
    rules = snapshot.get("orchestration_rules") if isinstance(snapshot.get("orchestration_rules"), dict) else {}
    events = [event for turn in trace if isinstance(turn, dict)
              for event in (turn.get("events") or []) if isinstance(event, dict)][-limit:]
    turns = []
    for turn in trace:
        if not isinstance(turn, dict):
            continue
        turns.append({
            "turn": turn.get("turn"),
            "events": turn.get("events") or [],
            "debug": turn.get("debug") or {},
            "error": turn.get("error") or None,
        })
    return _redact({
        "debug_session_id": row.debug_session_id,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "revision_id": row.revision_id,
        "baseline_release_id": row.baseline_release_id,
        "execution_runtime": "workbench_candidate",
        "configuration": {
            "snapshot_hash": snapshot.get("snapshot_hash"),
            "snapshot_source": "candidate_revision",
            "root": {"object_type": root.get("object_type"), "object_key": root.get("object_key"),
                     "revision_id": root_entry.get("revision_id"), "release_id": root_entry.get("release_id")},
            "expert_team": {"team_id": team_id or None, "revision_id": team_entry.get("revision_id"),
                            "release_id": team_entry.get("release_id"), "version": team_entry.get("version")},
            "orchestration_rules": {
                "revision_id": rules.get("orchestration_revision_id"),
                "revision_no": rules.get("revision_no"), "content_hash": rules.get("content_hash"),
                "rule_versions": rules.get("rule_versions") or {},
            },
        },
        "runtime_state": {
            "active_expert_id": meta.get("active_expert_id") or meta.get("expert_id"),
            "expert_selection_source": meta.get("expert_selection_source"),
            "branch_version": meta.get("_candidate_branch_version"),
            "interaction_state": runtime_context.get("interaction_state") or {},
        },
        "turns": turns,
        "events": events,
        **({"transcript": transcript, "runtime_context": runtime_context,
            "conclusion": row.conclusion} if include_content else {}),
    }, include_content=include_content)


def _sse_records(session_id: str, run_id: str | None, *, limit: int, include_content: bool) -> list[dict[str, Any]]:
    path = get_session_run_sse_log_path(session_id, run_id) if run_id else get_session_sse_aggregate_log_path(session_id)
    return _read_jsonl(path, predicate=lambda _: True, limit=limit, include_content=include_content)


def _diagnostic_errors(
    *,
    http_records: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    sse_records: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Project existing trails into a compact, operator-facing error timeline.

    A request can fail during validation before a run, transcript, or SSE file
    exists.  Returning only the raw trails made those incidents easy to miss.
    This is deliberately derived/read-only: it never invents a run or exposes
    content that has already been redacted by the caller's permissions.
    """
    errors: list[dict[str, Any]] = []
    for item in http_records:
        status_code = int(item.get("status_code") or 0)
        if status_code < 400:
            continue
        errors.append({
            "source": "http_request",
            "timestamp": item.get("timestamp_utc") or item.get("timestamp"),
            "request_id": item.get("request_id"),
            "run_id": item.get("run_id") or None,
            "status_code": status_code,
            "code": item.get("response_error_code") or f"HTTP_{status_code}",
            "message": item.get("response_error_message") or item.get("error") or None,
            "detail": item.get("response_error_detail"),
            "error": item.get("response_error_error") or None,
            "upstream_detail": item.get("response_error_upstream_detail") or None,
        })
    for item in runs:
        status = str(item.get("status") or "").lower()
        error = item.get("error") or item.get("error_summary")
        if status not in {"failed", "error", "rejected"} and not error:
            continue
        errors.append({
            "source": "run",
            "timestamp": item.get("updated_at") or item.get("completed_at") or item.get("started_at"),
            "run_id": item.get("run_id"),
            "status": status or None,
            "code": item.get("error_code") or None,
            "message": error or item.get("status_reason") or None,
        })

    for source, records in (("session_event", events), ("sse", sse_records)):
        for item in records:
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
            status = str(payload.get("status") or item.get("status") or "").lower()
            error = payload.get("error") or item.get("error")
            event_type = str(item.get("event_type") or item.get("event") or "")
            if (
                status not in {"failed", "error", "rejected"}
                and not error
                and "error" not in event_type.lower()
                and "failed" not in event_type.lower()
                and "unavailable" not in event_type.lower()
            ):
                continue
            error_payload = error if isinstance(error, dict) else {}
            errors.append({
                "source": source,
                "timestamp": item.get("timestamp_utc") or item.get("timestamp") or item.get("created_at"),
                "run_id": item.get("run_id") or payload.get("run_id") or None,
                "event_type": event_type or None,
                "status": status or None,
                "code": error_payload.get("code") or payload.get("error_code") or None,
                "message": (
                    error_payload.get("message")
                    if error_payload
                    else (payload.get("message") or (str(error) if error else None))
                ),
                "detail": error_payload.get("detail") or error_payload.get("upstream_detail") or None,
            })
    errors.sort(key=lambda item: str(item.get("timestamp") or ""))
    return errors[-limit:]


def _output_truncations(
    *,
    events: list[dict[str, Any]],
    sse_records: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """Return explicit output-limit evidence separately from general events.

    The raw event trail remains available for full audit. This projection makes
    an intermittent cut-off diagnosable without parsing a response body or
    guessing from punctuation. Only runtime-confirmed truncations are listed.
    """
    records: list[dict[str, Any]] = []
    for source, items in (("session_event", events), ("sse", sse_records)):
        for item in items:
            event_type = str(item.get("event_type") or item.get("event") or "")
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
            if event_type != "model_output_truncated" and not bool(payload.get("truncated")):
                continue
            reason_code = payload.get("truncation_reason_code")
            if not reason_code:
                continue
            records.append({
                "source": source,
                "timestamp": item.get("timestamp_utc") or item.get("timestamp") or item.get("created_at"),
                "run_id": item.get("run_id") or payload.get("run_id") or None,
                "expert_id": payload.get("expert_id"),
                "expert_turn_id": payload.get("expert_turn_id"),
                "model": payload.get("model"),
                "request_purpose": payload.get("request_purpose"),
                "reason_code": reason_code,
                "reason": payload.get("truncation_reason"),
                "finish_reason": payload.get("finish_reason"),
                "configured_max_tokens": payload.get("configured_max_tokens"),
                "configured_reply_max_chars": payload.get("configured_reply_max_chars"),
                "output_tokens": payload.get("output_tokens"),
                "received_chars": payload.get("received_chars"),
                "returned_chars": payload.get("returned_chars"),
            })
    records.sort(key=lambda item: str(item.get("timestamp") or ""))
    return records[-limit:]


def _output_diagnostics(*, events: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Expose completion evidence, including an explicit unknown state."""
    records: list[dict[str, Any]] = []
    for item in events:
        if str(item.get("event_type") or "") != "model_output_completion":
            continue
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        records.append({
            "timestamp": item.get("timestamp_utc") or item.get("timestamp") or item.get("created_at"),
            "run_id": item.get("run_id") or payload.get("run_id") or None,
            "source": payload.get("source"),
            "expert_id": payload.get("expert_id"),
            "expert_turn_id": payload.get("expert_turn_id"),
            "model": payload.get("model"),
            "request_purpose": payload.get("request_purpose"),
            "finish_reason": payload.get("finish_reason"),
            "finish_reason_reported": payload.get("finish_reason_reported"),
            "truncated": payload.get("truncated"),
            "truncation_status": payload.get("truncation_status"),
            "diagnostic_reason": payload.get("diagnostic_reason"),
            "configured_max_tokens": payload.get("configured_max_tokens"),
            "input_tokens": payload.get("input_tokens"),
            "output_tokens": payload.get("output_tokens"),
            "returned_chars": payload.get("returned_chars"),
        })
    records.sort(key=lambda item: str(item.get("timestamp") or ""))
    return records[-limit:]


def _test_environment() -> bool:
    environment = deployment_environment().strip().lower()
    return environment == "test" or environment.startswith("test-")


def _harness_runtime_trace(harness, session_id: str, *, run_id: str | None, limit: int,
                           include_content: bool) -> list[dict[str, Any]]:
    """Project durable V3 Harness state into an operator-only runtime trace."""
    if harness is None or not _test_environment():
        return []
    try:
        runs = harness.store.runs(session_id)
    except Exception:
        return []
    from hailiang_skills.harness.runtime import debug_step_io, execution_diagnostics

    traces: list[dict[str, Any]] = []
    for run in runs:
        if run_id and run.get("run_id") != run_id:
            continue
        policy = run.get("policy") if isinstance(run.get("policy"), dict) else {}
        snapshot = policy.get("snapshot") if isinstance(policy.get("snapshot"), dict) else {}
        rules = snapshot.get("orchestration_rules") if isinstance(snapshot.get("orchestration_rules"), dict) else {}
        root = snapshot.get("root") if isinstance(snapshot.get("root"), dict) else {}
        team_id = str(policy.get("team_id") or "")
        entries = snapshot.get("entries") if isinstance(snapshot.get("entries"), list) else []
        team_entry = next((item for item in entries if isinstance(item, dict)
                           and item.get("object_type") == "expert_team" and item.get("object_key") == team_id), {})
        task_nodes = []
        for task in run.get("tasks", {}).values():
            task_nodes.append({
                "id": task.get("id"), "parent_id": task.get("parent_id"), "kind": task.get("kind"),
                "expert_id": task.get("expert_id"), "skill_id": task.get("skill_id"),
                "status": task.get("status"), "attempt": task.get("attempt"),
                "depends_on": task.get("depends_on") or [], "created_at": task.get("created_at"),
                "started_at": task.get("started_at"), "ended_at": task.get("ended_at"),
                **({"goal": task.get("goal")} if include_content else {}),
            })
        tool_nodes = []
        for tool in run.get("tools", {}).values():
            tool_nodes.append({
                "id": tool.get("id"), "task_id": tool.get("task_id"), "parent_id": tool.get("parent_id"),
                "name": tool.get("name"), "status": tool.get("status"), "attempts": tool.get("attempts"),
                "created_at": tool.get("created_at"), "started_at": tool.get("started_at"), "ended_at": tool.get("ended_at"),
                "error": tool.get("error"),
            })
        input_requests = []
        for item in run.get("inputs", {}).values():
            input_requests.append({
                "id": item.get("id"), "status": item.get("status"), "version": item.get("version"),
                "round": item.get("round"), "owner_task_id": item.get("owner_task_id"),
                "bindings": item.get("bindings") or [],
                "fields": item.get("fields") if include_content else [field.get("key") for field in item.get("fields") or []],
                **({"answer": item.get("answer")} if include_content else {}),
            })
        orchestrator = policy.get("orchestrator") if isinstance(policy.get("orchestrator"), dict) else {}
        traces.append({
            "run_id": run.get("run_id"), "session_id": run.get("session_id"), "profile_id": run.get("profile_id"),
            "status": run.get("status"), "created_at": run.get("created_at"), "updated_at": run.get("updated_at"),
            "wire_protocol": run.get("wire_protocol"), "execution_runtime": run.get("execution_runtime") or "harness",
            "configuration": {
                "deployment_id": snapshot.get("deployment_id"), "package_hash": snapshot.get("package_hash"),
                "snapshot_hash": snapshot.get("snapshot_hash"), "snapshot_source": snapshot.get("source"),
                "root": {"object_type": root.get("object_type"), "object_key": root.get("object_key")},
                "expert_team": {"team_id": team_id, "release_id": team_entry.get("release_id"),
                                "version": team_entry.get("version")},
                "orchestration_rules": {
                    "revision_id": rules.get("orchestration_revision_id"),
                    "revision_no": rules.get("revision_no"), "content_hash": rules.get("content_hash"),
                    "rule_versions": rules.get("rule_versions") or {},
                },
            },
            "orchestration": {
                "enabled": bool(policy.get("orchestrated")), "identity_gate": policy.get("identity_gate"),
                "direct_expert_id": policy.get("direct_expert_id"),
                "members": orchestrator.get("members") or [], "consultation_policy": orchestrator.get("consultation_policy") or {},
            },
            "dispatch": run.get("dispatch") or {}, "tasks": task_nodes, "tools": tool_nodes,
            "input_requests": input_requests, "execution_diagnostics": execution_diagnostics(run),
            "steps": _redact(debug_step_io(run), include_content=include_content),
        })
    return traces[-limit:]


def _lookup_session(repository, engine, session_id: str, *, run_id: str | None, limit: int, include_content: bool,
                    harness=None,
                    event_types: list[str] | None = None) -> dict[str, Any]:
    try:
        context = repository.get(session_id)
    except Exception:
        # Keep the file-backed request trail available when the session
        # repository itself is the incident being investigated.
        context = None
    events, event_source = _session_events(session_id, limit=limit, include_content=include_content)
    candidate_trace, candidate_events = _candidate_trace(
        engine, session_id, limit=limit, include_content=include_content,
    )
    if candidate_events:
        seen = {
            str(item.get("event_id") or "") or json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            for item in events
        }
        for item in candidate_events:
            key = str(item.get("event_id") or "") or json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                events.append(item)
                seen.add(key)
        events = events[-limit:]
        event_source = "postgres+workbench_candidate"
    if event_types:
        allowed = {str(item).strip() for item in event_types if str(item).strip()}
        events = [item for item in events if str(item.get("event_type") or item.get("event") or "") in allowed]
    http_records = _http_records(field="session_id", value=session_id, limit=limit, include_content=include_content)
    runs: list[dict[str, Any]] = []
    ledger = (getattr(context, "session_meta", {}) or {}).get("run_ledger", {}) if context else {}
    if isinstance(ledger, dict):
        for candidate_run_id, item in ledger.items():
            if run_id is None or candidate_run_id == run_id:
                runs.append({"run_id": str(candidate_run_id), **_redact(dict(item) if isinstance(item, dict) else {}, include_content=include_content)})
    if run_id:
        database_run = _run_record(engine, run_id)
        if database_run and not any(item["run_id"] == run_id for item in runs):
            runs.append(database_run)
    runs.sort(key=lambda item: str(item.get("started_at") or item.get("created_at") or item["run_id"]))
    sse_records = _sse_records(session_id, run_id, limit=limit, include_content=include_content)
    harness_trace = _harness_runtime_trace(harness, session_id, run_id=run_id, limit=limit, include_content=include_content)
    sandbox_events = [
        item for item in events
        if str(item.get("event_type") or item.get("event") or "").startswith("sandbox_")
    ]
    latest_pressure: dict[str, Any] | None = None
    for item in reversed(sandbox_events):
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
        if any(key in payload for key in ("pool_size", "busy_workers", "queue_depth")):
            latest_pressure = {
                "pool_key_hash": payload.get("worker_pool_key_hash"),
                "pool_size": payload.get("pool_size", 0),
                "busy_workers": payload.get("busy_workers", 0),
                "idle_workers": payload.get("idle_workers", 0),
                "queue_depth": payload.get("queue_depth", 0),
                "acquire_wait_ms": payload.get("acquire_wait_ms", 0),
                "status": "saturated" if payload.get("queue_depth", 0) else "healthy",
            }
            break
    return {
        "session_id": session_id,
        "found": context is not None or bool(events) or bool(http_records) or bool(runs) or bool(candidate_trace) or bool(harness_trace),
        "session": _redact({
            "user_id": getattr(context, "user_id", "") if context else "",
            "active_profile_id": getattr(context, "profile_id", None) if context else None,
            "context_scope": getattr(context, "context_scope", None) if context else None,
            "branch_version": (getattr(context, "session_meta", {}) or {}).get("_active_branch_version") if context else None,
            "expert_context": (getattr(context, "session_meta", {}) or {}).get("expert_context") if context else None,
        }, include_content=include_content) if context else None,
        "runs": runs[-limit:],
        "events": events,
        "sandbox_events": sandbox_events,
        "resource_pressure": latest_pressure,
        "event_source": event_source,
        "http_requests": http_records,
        "sse_records": sse_records,
        "errors": _diagnostic_errors(
            http_records=http_records,
            runs=runs,
            events=events,
            sse_records=sse_records,
            limit=limit,
        ),
        "output_truncations": _output_truncations(
            events=events,
            sse_records=sse_records,
            limit=limit,
        ),
        "output_diagnostics": _output_diagnostics(events=events, limit=limit),
        "candidate_execution_trace": candidate_trace,
        "execution_trace": candidate_trace,
        "harness_runtime_trace": harness_trace,
        "harness_runtime_trace_available": _test_environment(),
    }


def build_diagnostics_router(repository, engine, harness=None) -> APIRouter:
    router = APIRouter(prefix="/operations/diagnostics", tags=["operations"])

    @router.post("/sessions/query")
    def get_session_diagnostics(
        body: SessionDiagnosticsInput,
        x_security_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _authorize(x_security_admin_token, authorization)
        session_id = _safe_id(body.session_id, label="session_id")
        run_id = _safe_id(body.run_id, label="run_id") if body.run_id else None
        return _lookup_session(repository, engine, session_id, run_id=run_id, limit=body.limit,
                                include_content=body.include_content, harness=harness, event_types=body.event_types)

    @router.post("/revision-tests/query")
    def get_revision_test_diagnostics(
        body: RevisionTestDiagnosticsInput,
        x_security_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Read one candidate manual-test trail without relying on the UI.

        This deliberately sits beside the formal-session diagnostics endpoint:
        both are privileged, read-only incident-analysis APIs, but their
        durable stores are different.
        """
        _authorize(x_security_admin_token, authorization)
        debug_session_id = _safe_id(body.debug_session_id, label="debug_session_id")
        trace = _candidate_revision_test_trace(
            engine, debug_session_id, limit=body.limit, include_content=body.include_content,
        )
        return {
            "debug_session_id": debug_session_id,
            "found": trace is not None,
            "candidate_revision_test_trace": trace,
        }

    @router.post("/runs/query")
    def get_run_diagnostics(
        body: RunDiagnosticsInput,
        x_security_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _authorize(x_security_admin_token, authorization)
        run_id = _safe_id(body.run_id, label="run_id")
        run = _run_record(engine, run_id)
        if run is None:
            records = _http_records(field="run_id", value=run_id, limit=body.limit, include_content=body.include_content)
            return {"run_id": run_id, "found": bool(records), "run": None, "http_requests": records,
                    "hint": "No persisted run was found; use request_id for validation, authentication, or pre-session failures."}
        result = _lookup_session(repository, engine, str(run["session_id"]), run_id=run_id, limit=body.limit,
                                 include_content=body.include_content, harness=harness)
        result["run"] = run
        return result

    @router.post("/requests/query")
    def get_request_diagnostics(
        body: RequestDiagnosticsInput,
        x_security_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _authorize(x_security_admin_token, authorization)
        request_id = _safe_id(body.request_id, label="request_id")
        records = _http_records(field="request_id", value=request_id, limit=body.limit, include_content=body.include_content)
        trace_ids = sorted({str(item.get("trace_id") or "") for item in records if item.get("trace_id")})
        telemetry: list[dict[str, Any]] = []
        for trace_id in trace_ids:
            for path in sorted((log_root() / "telemetry").glob(f"{trace_id}.*.jsonl")):
                telemetry.extend(_read_jsonl(path, predicate=lambda _: True, limit=body.limit, include_content=body.include_content))
        return {
            "request_id": request_id,
            "found": bool(records),
            "http_requests": records,
            "telemetry": telemetry[-body.limit:],
            "hint": "Every API response exposes X-Request-Id. Use it when validation/authentication fails before a session or run exists.",
        }

    return router
