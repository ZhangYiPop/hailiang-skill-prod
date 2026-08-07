"""Select file storage for local development or PostgreSQL for production."""

from __future__ import annotations

import os
from dataclasses import dataclass

from hailiang_skills.storage.database import Base, build_engine, build_session_factory
from hailiang_skills.storage.repositories.postgres_repo import (
    PostgresProfileRepository,
    PostgresSessionRepository,
    PostgresUserFactRepository,
    PostgresUserMetadataRepository,
)
from hailiang_skills.storage.repositories.profile_repo import FileBackedProfileRepository
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository
from hailiang_skills.storage.repositories.user_fact_repo import FileBackedUserFactRepository
from hailiang_skills.storage.repositories.user_metadata_repo import FileBackedUserMetadataRepository


@dataclass(slots=True)
class StorageBundle:
    session_repository: object
    user_fact_repository: object
    user_metadata_repository: object
    profile_repository: object
    engine: object | None = None
    session_factory: object | None = None
    backend: str = "file"

    def ready(self) -> bool:
        if self.engine is None:
            return self.backend != "postgres"
        try:
            with self.engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return True
        except Exception:
            return False


def build_storage_from_env() -> StorageBundle:
    backend = os.getenv("HAILIANG_STORAGE_BACKEND", "file").lower().strip()
    if backend in {"file", "memory", "dev"}:
        return StorageBundle(
            session_repository=InMemorySessionRepository(),
            user_fact_repository=FileBackedUserFactRepository(),
            user_metadata_repository=FileBackedUserMetadataRepository(),
            profile_repository=FileBackedProfileRepository(),
            backend="file",
        )
    if backend != "postgres":
        raise RuntimeError("HAILIANG_STORAGE_BACKEND must be 'postgres' or 'file'")
    engine = build_engine()
    if os.getenv("HAILIANG_DATABASE_AUTO_CREATE", "false").lower() == "true":
        Base.metadata.create_all(engine)
    session_factory = build_session_factory(engine)
    return StorageBundle(
        session_repository=PostgresSessionRepository(session_factory),
        user_fact_repository=PostgresUserFactRepository(session_factory),
        user_metadata_repository=PostgresUserMetadataRepository(session_factory),
        profile_repository=PostgresProfileRepository(session_factory),
        engine=engine,
        session_factory=session_factory,
        backend="postgres",
    )
