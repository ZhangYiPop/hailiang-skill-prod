from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from hailiang_skills.storage.database import Base, _json_type


class HarnessRunRow(Base):
    __tablename__ = "harness_runs"
    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    user_id: Mapped[str] = mapped_column(String(160))
    profile_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    active_session: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    payload: Mapped[dict] = mapped_column(_json_type())
    version: Mapped[int] = mapped_column(Integer, default=1)


class HarnessTaskRow(Base):
    __tablename__ = "harness_tasks"
    task_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict] = mapped_column(_json_type())


class HarnessToolRow(Base):
    __tablename__ = "harness_tool_calls"
    node_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(_json_type())


class HarnessInputRow(Base):
    __tablename__ = "harness_input_requests"
    request_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(_json_type())


class HarnessCheckpointRow(Base):
    __tablename__ = "harness_checkpoints"
    task_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(_json_type())


class HarnessJobRow(Base):
    __tablename__ = "harness_jobs"
    task_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    lease_token: Mapped[str] = mapped_column(String(80), default="")
    lease_until: Mapped[str] = mapped_column(String(40), default="")
    payload: Mapped[dict] = mapped_column(_json_type())


class HarnessEventRow(Base):
    __tablename__ = "harness_events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    event: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(_json_type())


class HarnessAttemptRow(Base):
    __tablename__ = "harness_execution_attempts"
    execution_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(_json_type())


class HarnessFactRow(Base):
    __tablename__ = "harness_facts"
    identity: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[dict] = mapped_column(_json_type())
    version: Mapped[int] = mapped_column(Integer)


TABLES = [model.__table__ for model in (
    HarnessRunRow, HarnessTaskRow, HarnessToolRow, HarnessInputRow,
    HarnessCheckpointRow, HarnessJobRow, HarnessEventRow, HarnessAttemptRow, HarnessFactRow,
)]

from .protocol_v3 import TABLES as PUBLIC_TABLES
TABLES.extend(PUBLIC_TABLES)
