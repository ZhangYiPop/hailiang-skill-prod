"""PostgreSQL persistence primitives used by the production storage backend.

The API remains synchronous today, therefore this module deliberately uses
SQLAlchemy's pooled sync engine.  It is safe to call from the bounded worker
pool used by SSE and avoids a per-request database connection.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, create_engine
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
    __tablename__ = "chat_sessions"

    session_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(160), index=True)
    profile_id: Mapped[str | None] = mapped_column("active_profile_id", String(80), nullable=True, index=True)
    profile_name: Mapped[str | None] = mapped_column("active_profile_name", String(160), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class SharedFactsRow(Base):
    __tablename__ = "chat_shared_facts"

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
    __tablename__ = "application_profile_projections"

    profile_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(160), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    facts: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class SessionProfileRow(Base):
    __tablename__ = "chat_session_profiles"

    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    profile_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    state: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    branch_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class SessionItemRow(Base):
    __tablename__ = "chat_session_items"
    __table_args__ = (UniqueConstraint("session_id", "ordinal", name="uq_chat_session_item_ordinal"),)

    item_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    item_type: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    model_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ChatRunRow(Base):
    __tablename__ = "chat_runs"

    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[str] = mapped_column(String(80), index=True)
    branch_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    action: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(40), default="running", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ContextCheckpointRow(Base):
    __tablename__ = "chat_context_checkpoints"
    __table_args__ = (UniqueConstraint("session_id", "profile_id", "covered_ordinal", name="uq_chat_checkpoint_coverage"),)

    checkpoint_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[str] = mapped_column(String(80), index=True)
    covered_ordinal: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    source_item_ids: Mapped[list[Any]] = mapped_column(_json_type(), default=list)
    token_metrics: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ContextJobRow(Base):
    __tablename__ = "chat_context_jobs"

    job_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.session_id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[str] = mapped_column(String(80), index=True)
    target_ordinal: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class WorkbenchActorRow(Base):
    __tablename__ = "workbench_actors"

    actor_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), index=True)
    device_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkbenchObjectRow(Base):
    __tablename__ = "workbench_objects"
    __table_args__ = (UniqueConstraint("object_type", "object_key", name="uq_workbench_object_type_key"),)

    object_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    object_type: Mapped[str] = mapped_column(String(32), index=True)
    object_key: Mapped[str] = mapped_column(String(160), index=True)
    current_release_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class WorkbenchRevisionRow(Base):
    __tablename__ = "workbench_revisions"
    __table_args__ = (UniqueConstraint("object_id", "revision_no", name="uq_workbench_revision_no"),)

    revision_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    object_id: Mapped[str] = mapped_column(ForeignKey("workbench_objects.object_id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    base_revision_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    dependency_locks: Mapped[list[Any]] = mapped_column(_json_type(), default=list)
    validation: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkbenchAssetRow(Base):
    __tablename__ = "workbench_revision_assets"
    __table_args__ = (UniqueConstraint("revision_id", "relative_path", name="uq_workbench_revision_asset_path"),)

    asset_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("workbench_revisions.revision_id"), index=True)
    relative_path: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)


class WorkbenchReleaseRow(Base):
    __tablename__ = "workbench_releases"
    __table_args__ = (UniqueConstraint("object_id", "release_no", name="uq_workbench_release_no"),)

    release_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    object_id: Mapped[str] = mapped_column(ForeignKey("workbench_objects.object_id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("workbench_revisions.revision_id"), unique=True, index=True)
    release_no: Mapped[int] = mapped_column(Integer, nullable=False)
    dependency_locks: Mapped[list[Any]] = mapped_column(_json_type(), default=list)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    verification: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published_by: Mapped[str] = mapped_column(String(80), default="system")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkbenchSoulRevisionRow(Base):
    __tablename__ = "workbench_soul_revisions"
    __table_args__ = (UniqueConstraint("revision_no", name="uq_workbench_soul_revision_no"),)

    soul_revision_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkbenchDebugSessionRow(Base):
    __tablename__ = "workbench_debug_sessions"

    debug_session_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("workbench_revisions.revision_id"), index=True)
    baseline_release_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    runtime_context: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    transcript: Mapped[list[Any]] = mapped_column(_json_type(), default=list)
    trace: Mapped[list[Any]] = mapped_column(_json_type(), default=list)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    conclusion: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkbenchEvaluationSuiteRow(Base):
    __tablename__ = "workbench_evaluation_suites"

    suite_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    object_id: Mapped[str] = mapped_column(ForeignKey("workbench_objects.object_id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    cases: Mapped[list[Any]] = mapped_column(_json_type(), default=list)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class WorkbenchEvaluationRunRow(Base):
    __tablename__ = "workbench_evaluation_runs"

    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    suite_id: Mapped[str] = mapped_column(ForeignKey("workbench_evaluation_suites.suite_id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("workbench_revisions.revision_id"), index=True)
    baseline_release_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    results: Mapped[list[Any]] = mapped_column(_json_type(), default=list)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    manual_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manual_notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkbenchDeploymentRow(Base):
    __tablename__ = "workbench_deployments"

    deployment_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), default="prod", index=True)
    root_release_id: Mapped[str] = mapped_column(String(80), index=True)
    package_hash: Mapped[str] = mapped_column(String(64), index=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    package_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="staged", index=True)
    previous_deployment_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    imported_by: Mapped[str] = mapped_column(String(80), default="system")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    activated_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkbenchAuditRow(Base):
    __tablename__ = "workbench_audit_events"

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), default="system", index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    target_type: Mapped[str] = mapped_column(String(64), default="")
    target_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(_json_type(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


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
