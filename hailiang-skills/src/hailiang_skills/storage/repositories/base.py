from __future__ import annotations

from abc import ABC, abstractmethod

from hailiang_skills.core.context import SessionContext


class BaseSessionRepository(ABC):
    @abstractmethod
    def create(self, context: SessionContext) -> SessionContext:
        raise NotImplementedError

    @abstractmethod
    def get(self, session_id: str) -> SessionContext:
        raise NotImplementedError

    @abstractmethod
    def save(self, context: SessionContext) -> SessionContext:
        raise NotImplementedError

    @abstractmethod
    def delete(self, session_id: str, *, user_id: str, profile_id: str | None) -> None:
        """Delete one session after verifying its owner and child profile."""
        raise NotImplementedError

    @abstractmethod
    def list(self) -> list[SessionContext]:
        raise NotImplementedError

    @abstractmethod
    def load_from_snapshot(self, session_id: str) -> SessionContext:
        raise NotImplementedError
