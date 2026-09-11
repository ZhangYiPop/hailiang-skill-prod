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
from hailiang_skills.storage.database import ChatRunRow
from hailiang_skills.storage.event_store import read_events


_CONTENT_KEYS = {
    "content", "input", "output", "raw_sse", "reasoning", "messages",
    "payload", "prompt", "response", "tool_input", "tool_output",
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


class RunDiagnosticsInput(BaseModel):
    run_id: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=200, ge=1, le=1000)
    include_content: bool = False


class RequestDiagnosticsInput(BaseModel):
    request_id: str = Field(min_length=1, max_length=160)
    limit: int = Field(default=100, ge=1, le=1000)
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
            if status not in {"failed", "error", "rejected"} and not error and "error" not in event_type.lower() and "failed" not in event_type.lower():
                continue
            error_payload = error if isinstance(error, dict) else {}
            errors.append({
                "source": source,
                "timestamp": item.get("timestamp_utc") or item.get("timestamp") or item.get("created_at"),
                "run_id": item.get("run_id") or payload.get("run_id") or None,
                "event_type": event_type or None,
                "status": status or None,
                "code": error_payload.get("code") or payload.get("error_code") or None,
                "message": error_payload.get("message") if error_payload else (str(error) if error else None),
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


def _lookup_session(repository, engine, session_id: str, *, run_id: str | None, limit: int, include_content: bool) -> dict[str, Any]:
    try:
        context = repository.get(session_id)
    except Exception:
        # Keep the file-backed request trail available when the session
        # repository itself is the incident being investigated.
        context = None
    events, event_source = _session_events(session_id, limit=limit, include_content=include_content)
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
    return {
        "session_id": session_id,
        "found": context is not None or bool(events) or bool(http_records) or bool(runs),
        "session": _redact({
            "user_id": getattr(context, "user_id", "") if context else "",
            "active_profile_id": getattr(context, "profile_id", None) if context else None,
            "context_scope": getattr(context, "context_scope", None) if context else None,
            "branch_version": (getattr(context, "session_meta", {}) or {}).get("_active_branch_version") if context else None,
            "expert_context": (getattr(context, "session_meta", {}) or {}).get("expert_context") if context else None,
        }, include_content=include_content) if context else None,
        "runs": runs[-limit:],
        "events": events,
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
    }


def build_diagnostics_router(repository, engine) -> APIRouter:
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
        return _lookup_session(repository, engine, session_id, run_id=run_id, limit=body.limit, include_content=body.include_content)

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
        result = _lookup_session(repository, engine, str(run["session_id"]), run_id=run_id, limit=body.limit, include_content=body.include_content)
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
