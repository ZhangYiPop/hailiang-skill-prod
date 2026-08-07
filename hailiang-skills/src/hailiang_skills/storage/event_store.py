"""Optional PostgreSQL event index; JSONL remains development-only."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hailiang_skills.storage.database import SessionEventRow

_session_factory = None


def configure_event_store(session_factory) -> None:
    global _session_factory
    _session_factory = session_factory


def append_events(session_id: str, events: list[dict[str, Any]]) -> bool:
    if _session_factory is None:
        return False
    with _session_factory.begin() as db:
        for event in events:
            created_at = event.get("created_at")
            if isinstance(created_at, str):
                try:
                    created_at = datetime.fromisoformat(created_at)
                except ValueError:
                    created_at = None
            db.merge(SessionEventRow(
                event_id=str(event.get("event_id")),
                session_id=session_id,
                event_type=str(event.get("event_type") or "unknown"),
                created_at=created_at or datetime.now(timezone.utc),
                payload=dict(event.get("payload") or {}),
            ))
    return True


def read_events(session_id: str) -> list[dict[str, Any]] | None:
    """Read persisted events from PostgreSQL when it is the configured sink.

    ``None`` means PostgreSQL is not configured; an empty list is a valid
    result for a configured store with no events.
    """
    if _session_factory is None:
        return None
    from sqlalchemy import select

    with _session_factory() as db:
        rows = db.scalars(
            select(SessionEventRow)
            .where(SessionEventRow.session_id == session_id)
            .order_by(SessionEventRow.created_at.asc(), SessionEventRow.event_id.asc())
        ).all()
        return [
            {
                "event_id": row.event_id,
                "event_type": row.event_type,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "payload": dict(row.payload or {}),
            }
            for row in rows
        ]


def read_events_in_range(start_at: datetime | None, end_at: datetime | None) -> list[dict[str, Any]] | None:
    """Read all domain events in an inclusive time range for operational reports."""
    if _session_factory is None:
        return None
    from sqlalchemy import select

    with _session_factory() as db:
        statement = select(SessionEventRow).order_by(SessionEventRow.created_at.asc(), SessionEventRow.event_id.asc())
        if start_at is not None:
            statement = statement.where(SessionEventRow.created_at >= start_at)
        if end_at is not None:
            statement = statement.where(SessionEventRow.created_at <= end_at)
        rows = db.scalars(statement).all()
        return [
            {
                "event_id": row.event_id,
                "session_id": row.session_id,
                "event_type": row.event_type,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "payload": dict(row.payload or {}),
            }
            for row in rows
        ]
