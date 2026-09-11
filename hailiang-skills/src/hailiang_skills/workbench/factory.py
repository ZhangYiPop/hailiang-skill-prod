from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from hailiang_skills.core.deployment import state_root
from hailiang_skills.storage.database import Base
from hailiang_skills.workbench.service import WorkbenchService


def build_workbench_service(storage, *, orchestrator=None) -> WorkbenchService:
    session_factory = getattr(storage, "session_factory", None)
    if session_factory is None:
        configured = os.getenv("HAILIANG_WORKBENCH_SQLITE_PATH", "").strip()
        db_path = Path(configured).expanduser().resolve() if configured else state_root() / "workbench.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        # Local SQLite has no Alembic lifecycle. Keep existing developer data
        # readable when the publication pointer is introduced.
        columns = {item["name"] for item in inspect(engine).get_columns("workbench_objects")}
        if "current_release_id" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE workbench_objects ADD COLUMN current_release_id VARCHAR(80)"))
                connection.execute(text("""
                    UPDATE workbench_objects SET current_release_id = (
                        SELECT release_id FROM workbench_releases
                        WHERE workbench_releases.object_id = workbench_objects.object_id
                          AND archived = 0
                        ORDER BY release_no DESC LIMIT 1
                    ) WHERE current_release_id IS NULL
                """))
        revision_columns = {item["name"] for item in inspect(engine).get_columns("workbench_revisions")}
        if "change_summary" not in revision_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE workbench_revisions ADD COLUMN change_summary TEXT NOT NULL DEFAULT ''"))
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    else:
        if os.getenv("HAILIANG_DATABASE_AUTO_CREATE", "false").lower() == "true":
            Base.metadata.create_all(storage.engine)
    return WorkbenchService(session_factory, orchestrator=orchestrator)
