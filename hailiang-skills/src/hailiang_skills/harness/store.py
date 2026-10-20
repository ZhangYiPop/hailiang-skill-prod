"""Short database transactions; no model/tool network calls under a lock."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Callable

from sqlalchemy import create_engine, event, select, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .contracts import HarnessError, TERMINAL, now
from .models import (
    TABLES, HarnessRunRow, HarnessTaskRow, HarnessToolRow, HarnessInputRow,
    HarnessCheckpointRow, HarnessJobRow, HarnessEventRow, HarnessFactRow, HarnessAttemptRow,
    HarnessClientRunRow, HarnessPortraitRow,
)


class HarnessStore:
    def __init__(self, engine):
        self.engine = engine
        self.wakeup = None

    @classmethod
    def local(cls, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})
        @event.listens_for(engine, "connect")
        def pragmas(connection, _record):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
        for table in TABLES:
            table.create(engine, checkfirst=True)
        return cls(engine)

    @contextmanager
    def transaction(self):
        with Session(self.engine, expire_on_commit=False) as db:
            try:
                if self.engine.dialect.name == "sqlite":
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                yield db
                db.commit()
                if self.wakeup and db.info.get("harness_changed"):
                    self.wakeup.publish()
            except BaseException:
                db.rollback()
                raise

    def _locked(self, db, run_id):
        row = db.scalar(select(HarnessRunRow).where(HarnessRunRow.run_id == run_id).with_for_update())
        if row is None:
            raise HarnessError("HARNESS_RUN_NOT_FOUND", 404)
        return row

    def create(self, run: dict):
        try:
            with self.transaction() as db:
                row = HarnessRunRow(run_id=run["run_id"], session_id=run["session_id"], user_id=run["user_id"],
                                    profile_id=run["profile_id"], active_session=run["session_id"], payload={}, version=0)
                db.add(row)
                for identity, record in run.get("seed_facts", {}).items():
                    from sqlalchemy.dialects.postgresql import insert as pg_insert
                    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                    insert = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
                    db.execute(insert(HarnessFactRow).values(identity=identity, version=1, payload=deepcopy(record)).on_conflict_do_nothing(index_elements=["identity"]))
                self._save(db, row, run, [{"event": "run_started", "node_id": run["root_task_id"]}])
        except IntegrityError as exc:
            raise HarnessError("ACTIVE_HARNESS_RUN_EXISTS", 409) from exc

    def mutate(self, run_id: str, apply: Callable):
        with self.transaction() as db:
            row = self._locked(db, run_id)
            run = deepcopy(row.payload)
            changes: list[dict] = []
            result = apply(run, changes, db)
            if changes:
                self._save(db, row, run, changes)
            return deepcopy(result)

    def _save(self, db, row, run, changes):
        db.info["harness_changed"] = True
        row.version += 1
        run["version"] = row.version
        run["updated_at"] = now()
        row.active_session = None if run["status"] in TERMINAL else run["session_id"]
        for task in run["tasks"].values():
            for index, message in enumerate(task["messages"]):
                message.setdefault("source_id", f"{task['id']}:message:{len(task.get('archived_messages', [])) + index}")
            db.merge(HarnessTaskRow(task_id=task["id"], run_id=row.run_id, parent_id=task.get("parent_id"), status=task["status"], payload=deepcopy(task)))
            db.merge(HarnessCheckpointRow(task_id=task["id"], run_id=row.run_id, version=row.version, payload={"messages": task["messages"], "pending_calls": task["pending_calls"], "summary": task.get("summary", ""), "trace": task.get("trace", [])}))
            db.merge(HarnessJobRow(task_id=task["id"], run_id=row.run_id, status=task["status"], lease_token=task.get("lease_token", ""), lease_until=task.get("lease_until", ""), payload={"attempt": task.get("attempt", 0)}))
            for attempt in task.get("execution_attempts", []):
                if attempt["status"] == "running":
                    if task["status"] == "running" and task["lease_token"] == attempt["execution_id"]:
                        attempt["lease_until"] = task["lease_until"]
                    else:
                        attempt.update(status=task["status"] if task["status"] in TERMINAL else ("completed" if task["lease_token"] == attempt["execution_id"] or task.get("finished_execution_id") == attempt["execution_id"] else "superseded"), ended_at=now())
                db.merge(HarnessAttemptRow(execution_id=attempt["execution_id"], run_id=row.run_id, task_id=task["id"], status=attempt["status"], payload=deepcopy(attempt)))
        for tool in run["tools"].values():
            db.merge(HarnessToolRow(node_id=tool["id"], run_id=row.run_id, task_id=tool["task_id"], status=tool["status"], payload=deepcopy(tool)))
        for request in run["inputs"].values():
            db.merge(HarnessInputRow(request_id=request["id"], run_id=row.run_id, status=request["status"], payload=deepcopy(request)))
        row.payload = deepcopy(run)
        for change in changes:
            node_id = change.get("node_id", run["root_task_id"])
            node = run["tasks"].get(node_id) or run["tools"].get(node_id) or {}
            db.add(HarnessEventRow(session_id=row.session_id, run_id=row.run_id, event=change["event"], payload={
                "run_id": row.run_id, "node_id": node_id, "parent_id": node.get("parent_id") or node.get("task_id"),
                "version": row.version, "timestamp": now(), "status": node.get("status", run["status"]),
                "execution_id": node.get("execution_id") or node.get("lease_token"),
            }))

    def get(self, run_id):
        with Session(self.engine) as db:
            row = db.get(HarnessRunRow, run_id)
            if row is None:
                raise HarnessError("HARNESS_RUN_NOT_FOUND", 404)
            return deepcopy(row.payload)

    def runs(self, session_id):
        with Session(self.engine) as db:
            return [deepcopy(row.payload) for row in db.scalars(select(HarnessRunRow).where(HarnessRunRow.session_id == session_id))]

    def active(self, session_id):
        with Session(self.engine) as db:
            row = db.scalar(select(HarnessRunRow).where(HarnessRunRow.active_session == session_id))
            return deepcopy(row.payload) if row else None

    def runnable_runs(self):
        with Session(self.engine) as db:
            return list(db.scalars(select(HarnessRunRow.run_id).where(HarnessRunRow.active_session.is_not(None))))

    def request_run(self, request_id):
        with Session(self.engine) as db:
            row = db.get(HarnessInputRow, request_id)
            if row is None:
                raise HarnessError("INPUT_REQUEST_NOT_FOUND", 404)
            return row.run_id

    def events(self, session_id, after=0, limit=100):
        with Session(self.engine) as db:
            return [{"seq": row.seq, "event": row.event, **row.payload} for row in db.scalars(
                select(HarnessEventRow).where(HarnessEventRow.session_id == session_id, HarnessEventRow.seq > after)
                .order_by(HarnessEventRow.seq).limit(limit))]

    def cursor(self, session_id):
        with Session(self.engine) as db:
            return db.scalar(select(func.max(HarnessEventRow.seq)).where(HarnessEventRow.session_id == session_id)) or 0

    def client_run(self, session_id, user_id, profile_id, client_run_id):
        profile_key = profile_id or ""
        with Session(self.engine) as db:
            row = db.scalar(select(HarnessClientRunRow).where(
                HarnessClientRunRow.session_id == session_id,
                HarnessClientRunRow.user_id == user_id,
                HarnessClientRunRow.profile_id == profile_key,
                HarnessClientRunRow.client_run_id == client_run_id,
            ))
            return None if row is None else {"harness_run_id": row.harness_run_id, "request_hash": row.request_hash}

    def bind_client_run(self, session_id, user_id, profile_id, client_run_id, harness_run_id, request_hash):
        profile_key = profile_id or ""
        with self.transaction() as db:
            existing = db.scalar(select(HarnessClientRunRow).where(
                HarnessClientRunRow.session_id == session_id,
                HarnessClientRunRow.user_id == user_id,
                HarnessClientRunRow.profile_id == profile_key,
                HarnessClientRunRow.client_run_id == client_run_id,
            ).with_for_update())
            if existing:
                if existing.request_hash != request_hash:
                    raise HarnessError("CLIENT_RUN_ID_CONFLICT", 409)
                return existing.harness_run_id
            db.add(HarnessClientRunRow(client_run_id=client_run_id, session_id=session_id, user_id=user_id,
                                       profile_id=profile_key, harness_run_id=harness_run_id, request_hash=request_hash))
            return harness_run_id

    def upsert_portrait(self, user_id, profile_id, patch: dict, *, aliases: dict[str, str] | None = None):
        """Merge trusted portrait leaves and index them as profile facts."""
        if not profile_id or not isinstance(patch, dict):
            return {}
        aliases = aliases or {}
        def leaves(value, prefix=""):
            if isinstance(value, dict):
                for key, child in value.items():
                    if not isinstance(key, str) or not key or "." in key:
                        raise HarnessError("INVALID_PROFILE_DATA")
                    yield from leaves(child, f"{prefix}.{key}" if prefix else key)
            else:
                if value is not None:
                    yield prefix, value
        flattened = dict(leaves(patch))
        run = {"session_id": "portrait", "user_id": user_id, "profile_id": profile_id}
        task = {"id": "portrait"}
        with self.transaction() as db:
            row = db.get(HarnessPortraitRow, {"user_id": user_id, "profile_id": profile_id})
            payload = deepcopy(row.payload) if row else {"data": {}, "history": []}
            data = payload.setdefault("data", {})
            changed = {}
            for path, value in flattened.items():
                if data.get(path) != value:
                    changed[path] = value
                    data[path] = value
            if changed:
                payload.setdefault("history", []).append({"updated_at": now(), "paths": sorted(changed)})
                version = (row.version + 1) if row else 1
                db.merge(HarnessPortraitRow(user_id=user_id, profile_id=profile_id, payload=payload, version=version))
                for path, value in changed.items():
                    keys = [f"profile.{path}"]
                    if path in aliases:
                        keys.append(aliases[path])
                    for key in keys:
                        identity = self.fact_identity(run, task, "profile", key)
                        self.write_fact(db, identity, {"key": key, "value": value, "scope": "profile", "status": "confirmed",
                                                       "source": {"type": "project_backend", "portrait_path": path, "portrait_version": version}})
            return changed

    def ready(self):
        try:
            with Session(self.engine) as db:
                db.execute(select(HarnessRunRow.run_id).limit(1))
            return True
        except Exception:
            return False

    @staticmethod
    def fact_identity(run, task, scope, key):
        owner = {"task": task["id"], "session": run["session_id"], "profile": run["profile_id"], "user": run["user_id"]}[scope]
        if not owner:
            raise HarnessError("FACT_SCOPE_UNAVAILABLE")
        import json
        return json.dumps([run["user_id"], scope, owner, key], ensure_ascii=False)

    @staticmethod
    def write_fact(db, identity, record):
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"), {"identity": identity})
        row = db.scalar(select(HarnessFactRow).where(HarnessFactRow.identity == identity).with_for_update())
        previous = deepcopy(row.payload) if row else None
        version = row.version + 1 if row else 1
        record = {**record, "version": version, "updated_at": now(), "history": (previous or {}).get("history", []) + ([{k: v for k, v in previous.items() if k != "history"}] if previous else [])}
        db.merge(HarnessFactRow(identity=identity, version=version, payload=record))
        return record

    def scoped_facts(self, user_id, scope, owner):
        import json
        prefix = json.dumps([user_id, scope, owner], ensure_ascii=False)[:-1] + ","
        with Session(self.engine) as db:
            return {json.loads(row.identity)[3]: deepcopy(row.payload) for row in db.scalars(select(HarnessFactRow).where(HarnessFactRow.identity.startswith(prefix, autoescape=True)))}

    def sync_legacy_facts(self, context, changes, scope):
        scope = "user" if scope == "shared" else scope
        task = {"id": "legacy"}
        run = {"session_id": context.session_id, "profile_id": context.profile_id, "user_id": context.user_id}
        with self.transaction() as db:
            for change in sorted(changes, key=lambda c: c["key"]):
                identity = self.fact_identity(run, task, scope, change["key"])
                self.write_fact(db, identity, {"key": change["key"], "value": change["after"], "scope": scope,
                    "status": "confirmed" if change["after"] is not None else "expired", "source": change["source"]})
            db.info["harness_changed"] = bool(changes)

    def fact_records(self, run, task, db=None):
        def read(session):
            import json
            result = {}
            prefix = json.dumps([run["user_id"]], ensure_ascii=False)[:-1] + ","
            for row in session.scalars(select(HarnessFactRow).where(HarnessFactRow.identity.startswith(prefix, autoescape=True))):
                user, scope, owner, key = json.loads(row.identity)
                expected = {"task": task["id"], "session": run["session_id"], "profile": run["profile_id"], "user": run["user_id"]}.get(scope)
                ancestors = {task["id"]}
                parent = task.get("parent_id")
                while parent:
                    ancestors.add(parent)
                    parent = run["tasks"][parent].get("parent_id")
                matches = owner in ancestors if scope == "task" else expected and owner == expected
                if user == run["user_id"] and matches:
                    result[row.identity] = deepcopy(row.payload)
            return result
        if db is not None:
            return read(db)
        with Session(self.engine) as session:
            return read(session)
