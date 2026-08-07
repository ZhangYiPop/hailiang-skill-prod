"""Process-level encrypted audit sink, isolated from ordinary application logs."""

from __future__ import annotations

from typing import Any

from hailiang_skills.core.telemetry import ERRORS, current_telemetry, text_fingerprint
from hailiang_skills.core.deployment import raw_audit_enabled

_store: Any | None = None


def set_audit_store(store: Any | None) -> None:
    global _store
    _store = store


def audit_text(kind: str, content: str, *, session_id: str | None = None) -> dict[str, Any]:
    """Persist full text encrypted when configured; never expose it to logs."""
    if _store is None or not raw_audit_enabled():
        return {"audit_enabled": False, **text_fingerprint(content, preview_chars=0)}
    try:
        return {"audit_enabled": True, **_store.write(kind, content, session_id=session_id)}
    except Exception as exc:  # audit failure is visible in metrics/events, not text logs
        if ERRORS:
            ERRORS.labels(node="audit_write", error_type=type(exc).__name__).inc()
        return {"audit_enabled": True, "audit_write_failed": True, **text_fingerprint(content, preview_chars=0)}
