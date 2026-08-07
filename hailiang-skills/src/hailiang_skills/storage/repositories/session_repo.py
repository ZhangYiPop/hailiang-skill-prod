from __future__ import annotations

from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.session_logging import ensure_session_log_dir, write_session_snapshot
from hailiang_skills.storage.repositories.base import BaseSessionRepository
from hailiang_skills.storage.repositories.file_session_repo import (
    load_session_context_from_snapshot,
)


class InMemorySessionRepository(BaseSessionRepository):
    def __init__(self) -> None:
        self._items: dict[str, SessionContext] = {}

    def create(self, context: SessionContext) -> SessionContext:
        self._items[context.session_id] = context
        ensure_session_log_dir(context.session_id)
        write_session_snapshot(context)
        return context

    def get(self, session_id: str) -> SessionContext:
        if session_id not in self._items:
            self._items[session_id] = self.load_from_snapshot(session_id)
        return self._items[session_id]

    def save(self, context: SessionContext) -> SessionContext:
        self._items[context.session_id] = context
        write_session_snapshot(context)
        return context

    def list(self) -> list[SessionContext]:
        return list(self._items.values())

    def load_from_snapshot(self, session_id: str) -> SessionContext:
        return load_session_context_from_snapshot(session_id)
