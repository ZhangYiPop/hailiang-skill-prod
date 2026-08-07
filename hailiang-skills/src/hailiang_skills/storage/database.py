"""PostgreSQL persistence primitives used by the production storage backend.

The API remains synchronous today, therefore this module deliberately uses
SQLAlchemy's pooled sync engine.  It is safe to call from the bounded worker
pool used by SSE and avoids a per-request database connection.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, Integer, LargeBinary, String, Text, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import JSON


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_type():
    # JSONB is selected by PostgreSQL while SQLite remains useful for local
    # migration/unit-test verification.
    return JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "advisor_sessions"

    session_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(160), index=True)
    profile_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    profile_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class SharedFactsRow(Base):
    __tablename__ = "advisor_shared_facts"

    user_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    facts: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class UserMetadataRow(Base):
    __tablename__ = "advisor_users"

    user_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    extra_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", _json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ProfileRow(Base):
    __tablename__ = "advisor_profiles"

    profile_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(160), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    facts: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class SessionEventRow(Base):
    __tablename__ = "advisor_session_events"

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)


class AuditPayloadRow(Base):
    __tablename__ = "advisor_audit_payloads"

    audit_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    request_id: Mapped[str] = mapped_column(String(80), index=True)
    session_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_length: Mapped[int] = mapped_column(Integer)
    key_id: Mapped[str] = mapped_column(String(80))
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    access_log: Mapped[str | None] = mapped_column(Text, nullable=True)


def database_url_from_env() -> str:
    return os.getenv("HAILIANG_DATABASE_URL", "postgresql+psycopg://hailiang:hailiang@postgres:5432/hailiang_skills")


def build_engine(url: str | None = None):
    return create_engine(
        url or database_url_from_env(),
        pool_size=int(os.getenv("HAILIANG_DB_POOL_SIZE", "20")),
        max_overflow=int(os.getenv("HAILIANG_DB_MAX_OVERFLOW", "10")),
        pool_pre_ping=True,
        pool_recycle=int(os.getenv("HAILIANG_DB_POOL_RECYCLE_SECONDS", "1800")),
        connect_args={"connect_timeout": int(os.getenv("HAILIANG_DB_CONNECT_TIMEOUT_SECONDS", "3"))},
    )


def build_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
