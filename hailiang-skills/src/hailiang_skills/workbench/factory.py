from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
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
        session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    else:
        if os.getenv("HAILIANG_DATABASE_AUTO_CREATE", "false").lower() == "true":
            Base.metadata.create_all(storage.engine)
    return WorkbenchService(session_factory, orchestrator=orchestrator)
