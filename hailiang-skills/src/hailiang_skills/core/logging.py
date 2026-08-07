from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from hailiang_skills.core.telemetry import enrich_payload, redact_log_payload

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": f"evt_{uuid4().hex[:12]}",
        "event_type": event_type,
        "timestamp": datetime.now(SHANGHAI_TZ).isoformat(),
        "created_at": utc_now_iso(),
        **_current_event_context(),
        # All domain events inherit the request/trace context.  This keeps
        # existing event consumers compatible while allowing a request to be
        # reconstructed from the session audit trail.
        "payload": redact_log_payload(enrich_payload(payload)),
    }


def _current_event_context() -> dict[str, str]:
    from hailiang_skills.core.telemetry import current_telemetry

    context = current_telemetry()
    if not context:
        return {
            "session_id": "",
            "run_id": "",
            "user_id": "",
        }
    return {
        "session_id": context.session_id,
        "run_id": context.run_id,
        "user_id": context.user_id,
    }
