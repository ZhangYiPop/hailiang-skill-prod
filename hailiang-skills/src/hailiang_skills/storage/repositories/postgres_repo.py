"""PostgreSQL repositories for sessions, profiles and the three Fact scopes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, update

from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.telemetry import span
from hailiang_skills.schemas.facts import FactRecord, KnownFacts
from hailiang_skills.storage.database import (
    AuditPayloadRow,
    ChatRunRow,
    ProfileRow,
    SessionEventRow,
    SessionItemRow,
    SessionProfileRow,
    SessionRow,
    SharedFactsRow,
    UserMetadataRow,
)
from hailiang_skills.storage.repositories.base import BaseSessionRepository


class SessionVersionConflict(RuntimeError):
    """The session changed since it was read; callers must reload and retry."""


def _facts_to_payload(facts: KnownFacts) -> dict[str, Any]:
    return {key: value.model_dump(mode="json") for key, value in facts.facts.items()}


def _facts_from_payload(payload: dict[str, Any] | None) -> KnownFacts:
    facts = KnownFacts()
    for key, value in (payload or {}).items():
        facts.facts[key] = FactRecord.model_validate(value)
    return facts


def _jsonable(value: Any) -> Any:
    if callable(value):
        return None
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items() if not callable(item)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, set):
        return [_jsonable(item) for item in sorted(value, key=str)]
    return value


def _context_payload(context: SessionContext) -> dict[str, Any]:
    context.sync_active_branch()
    return {
        "messages": _jsonable(context.messages),
        "shared_facts": _facts_to_payload(context.shared_facts),
        "profile_facts": _facts_to_payload(context.profile_facts),
        "session_facts": _facts_to_payload(context.session_facts),
        "skill_states": _jsonable(context.skill_states),
        "candidate_paths": _jsonable(context.candidate_paths),
        "interaction_state": _jsonable(context.interaction_state),
        "risk_signals": _jsonable(context.risk_signals),
        "event_trace": _jsonable(context.event_trace[-200:]),
        "asset_version": context.asset_version,
        "session_meta": _jsonable(context.session_meta),
        "last_fact_changes": _jsonable(context.last_fact_changes),
        "profile_branches": _jsonable(context.profile_branches),
        "timeline_items": _jsonable(context.timeline_items),
    }


def _context_from_row(
    row: SessionRow,
    *,
    branches: list[SessionProfileRow] | None = None,
    items: list[SessionItemRow] | None = None,
) -> SessionContext:
    payload = row.payload or {}
    branch_payloads = dict(payload.get("profile_branches") or {})
    for branch in branches or []:
        branch_payloads[branch.profile_id] = {
            **dict(branch.state or {}),
            "profile_id": branch.profile_id,
            "profile_name": branch.profile_name,
            "branch_version": branch.branch_version,
        }
    timeline_items = list(payload.get("timeline_items") or [])
    if items is not None:
        timeline_items = [dict(item.payload or {}) for item in items]
    context = SessionContext(
        session_id=row.session_id,
        user_id=row.user_id,
        profile_id=row.profile_id,
        profile_name=row.profile_name,
        title=row.title,
        messages=list(payload.get("messages") or []),
        skill_states=dict(payload.get("skill_states") or {}),
        candidate_paths=list(payload.get("candidate_paths") or []),
        interaction_state=dict(payload.get("interaction_state") or {}),
        risk_signals=list(payload.get("risk_signals") or []),
        event_trace=list(payload.get("event_trace") or []),
        asset_version=str(payload.get("asset_version") or "dev"),
        session_meta=dict(payload.get("session_meta") or {}),
        last_fact_changes=list(payload.get("last_fact_changes") or []),
        profile_branches=branch_payloads,
        timeline_items=timeline_items,
    )
    context.load_effective_facts(
        shared_facts=_facts_from_payload(payload.get("shared_facts")),
        profile_facts=_facts_from_payload(payload.get("profile_facts")),
        session_facts=_facts_from_payload(payload.get("session_facts")),
    )
    context.session_meta["_storage_version"] = row.version
    return context


class PostgresSessionRepository(BaseSessionRepository):
    """Database true source with an optimistic version guard on every save."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def create(self, context: SessionContext) -> SessionContext:
        with span("postgres.session.create", node="postgres_session_create"):
            with self._session_factory.begin() as db:
                db.add(SessionRow(
                    session_id=context.session_id,
                    user_id=context.user_id,
                    profile_id=context.profile_id,
                    profile_name=context.profile_name,
                    title=context.title,
                    payload=_context_payload(context),
                    version=1,
                ))
                db.flush()
                self._sync_profile_storage(db, context)
            context.session_meta["_storage_version"] = 1
            return context

    def get(self, session_id: str) -> SessionContext:
        with span("postgres.session.read", node="postgres_session_read"):
            with self._session_factory() as db:
                row = db.get(SessionRow, session_id)
                if row is None:
                    raise KeyError(session_id)
                branches = db.scalars(
                    select(SessionProfileRow).where(SessionProfileRow.session_id == session_id)
                ).all()
                items = db.scalars(
                    select(SessionItemRow)
                    .where(SessionItemRow.session_id == session_id)
                    .order_by(SessionItemRow.ordinal)
                ).all()
                return _context_from_row(row, branches=list(branches), items=list(items))

    def save(self, context: SessionContext) -> SessionContext:
        expected = int(context.session_meta.get("_storage_version") or 1)
        with span("postgres.session.save", node="postgres_session_save"):
            with self._session_factory.begin() as db:
                result = db.execute(
                    update(SessionRow)
                    .where(SessionRow.session_id == context.session_id, SessionRow.version == expected)
                    .values(
                        user_id=context.user_id,
                        profile_id=context.profile_id,
                        profile_name=context.profile_name,
                        title=context.title,
                        payload=_context_payload(context),
                        version=expected + 1,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                if result.rowcount != 1:
                    raise SessionVersionConflict(f"session {context.session_id} was updated concurrently")
                self._sync_profile_storage(db, context)
            context.session_meta["_storage_version"] = expected + 1
            return context

    def delete(self, session_id: str, *, user_id: str, profile_id: str | None) -> None:
        """Delete a session and its event index, guarded by its ownership."""
        with span("postgres.session.delete", node="postgres_session_delete"):
            with self._session_factory.begin() as db:
                row = db.get(SessionRow, session_id)
                if row is None:
                    raise KeyError(session_id)
                if row.user_id != user_id or row.profile_id != profile_id:
                    raise PermissionError(session_id)
                db.execute(delete(SessionEventRow).where(SessionEventRow.session_id == session_id))
                db.execute(delete(AuditPayloadRow).where(AuditPayloadRow.session_id == session_id))
                db.delete(row)

    def list(self) -> list[SessionContext]:
        with self._session_factory() as db:
            rows = db.scalars(select(SessionRow)).all()
            return [self.get(row.session_id) for row in rows]

    def list_by_profile(self, user_id: str, profile_id: str) -> list[SessionContext]:
        with self._session_factory() as db:
            query = (
                select(SessionRow)
                .join(SessionProfileRow, SessionProfileRow.session_id == SessionRow.session_id)
                .where(SessionRow.user_id == user_id, SessionProfileRow.profile_id == profile_id)
                .order_by(SessionRow.updated_at.desc())
            )
            return [self.get(row.session_id) for row in db.scalars(query).all()]

    def _sync_profile_storage(self, db, context: SessionContext) -> None:
        context.sync_active_branch()
        for profile_id, raw_state in context.profile_branches.items():
            state = dict(raw_state or {})
            branch = db.get(SessionProfileRow, (context.session_id, profile_id))
            if branch is None:
                branch = SessionProfileRow(
                    session_id=context.session_id,
                    profile_id=profile_id,
                    profile_name=state.get("profile_name"),
                    state=state,
                    branch_version=int(state.get("branch_version") or 1),
                )
                db.add(branch)
            else:
                branch.profile_name = state.get("profile_name")
                branch.state = state
                branch.branch_version = int(state.get("branch_version") or branch.branch_version or 1)
                branch.updated_at = datetime.now(timezone.utc)
        for ordinal, raw_item in enumerate(context.timeline_items, start=1):
            item = dict(raw_item or {})
            item_id = str(item.get("item_id") or f"item_{uuid4().hex[:16]}")
            item["item_id"] = item_id
            row = db.get(SessionItemRow, item_id)
            if row is None:
                db.add(SessionItemRow(
                    item_id=item_id,
                    session_id=context.session_id,
                    ordinal=ordinal,
                    profile_id=str(item.get("profile_id") or "") or None,
                    item_type=str(item.get("item_type") or "message"),
                    payload=item,
                    model_visible=bool(item.get("model_visible", True)),
                ))
            else:
                row.ordinal = ordinal
                row.profile_id = str(item.get("profile_id") or "") or None
                row.item_type = str(item.get("item_type") or "message")
                row.payload = item
                row.model_visible = bool(item.get("model_visible", True))

    def record_run(self, context: SessionContext, run_id: str, action: str) -> None:
        with self._session_factory.begin() as db:
            if db.get(ChatRunRow, run_id) is not None:
                return
            db.add(ChatRunRow(
                run_id=run_id,
                session_id=context.session_id,
                profile_id=str(context.profile_id or ""),
                branch_version=int(context.session_meta.get("_active_branch_version") or 1),
                action=action,
                status="running",
            ))

    def update_run_status(self, run_id: str, status: str) -> None:
        with self._session_factory.begin() as db:
            row = db.get(ChatRunRow, run_id)
            if row is not None:
                row.status = status
                row.updated_at = datetime.now(timezone.utc)

    def recent_session_payloads(self, user_id: str, profile_id: str, *, limit: int = 2) -> list[dict]:
        """Fetch only the data needed to build a new-session greeting.

        Avoiding ``list_by_profile`` here is important: that method hydrates
        every historical session (and its full message list) before a new
        conversation can be shown.
        """
        with span("postgres.session.recent_summary", node="recent_summary_lookup"):
            with self._session_factory() as db:
                query = (
                    select(SessionProfileRow.state)
                    .join(SessionRow, SessionRow.session_id == SessionProfileRow.session_id)
                    .where(SessionRow.user_id == user_id, SessionProfileRow.profile_id == profile_id)
                    .order_by(SessionRow.updated_at.desc())
                    .limit(max(1, limit))
                )
                return [dict(payload or {}) for payload in db.scalars(query).all()]

    def load_from_snapshot(self, session_id: str) -> SessionContext:
        return self.get(session_id)


class PostgresUserFactRepository:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def get(self, user_id: str) -> KnownFacts:
        with span("postgres.shared_facts.read", node="postgres_shared_facts_read"):
            with self._session_factory() as db:
                row = db.get(SharedFactsRow, user_id)
                return _facts_from_payload(row.facts if row else {})

    def save(self, user_id: str, facts: KnownFacts) -> KnownFacts:
        payload = _facts_to_payload(facts)
        with span("postgres.shared_facts.save", node="postgres_shared_facts_save"):
            with self._session_factory.begin() as db:
                row = db.get(SharedFactsRow, user_id)
                if row is None:
                    db.add(SharedFactsRow(user_id=user_id, facts=payload))
                else:
                    row.facts = payload
                    row.version += 1
            return facts

    def reset(self, user_id: str, fact_keys: list[str]) -> KnownFacts:
        facts = self.get(user_id)
        for key in fact_keys:
            facts.reset_fact(key)
        return self.save(user_id, facts)


class PostgresUserMetadataRepository:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def get(self, user_id: str) -> dict[str, Any]:
        with self._session_factory() as db:
            row = db.get(UserMetadataRow, user_id)
            if row is None:
                return {"user_id": user_id, "display_name": "", "metadata": {}}
            return {"user_id": row.user_id, "display_name": row.display_name, "metadata": dict(row.extra_metadata or {})}

    def upsert(self, user_id: str, display_name: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._session_factory.begin() as db:
            row = db.get(UserMetadataRow, user_id)
            if row is None:
                row = UserMetadataRow(user_id=user_id, display_name=display_name.strip(), extra_metadata=metadata or {})
                db.add(row)
            else:
                row.display_name = display_name.strip()
                row.extra_metadata = {**(row.extra_metadata or {}), **(metadata or {})}
            return {"user_id": row.user_id, "display_name": row.display_name, "metadata": dict(row.extra_metadata or {})}


class PostgresProfileRepository:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def list_profiles(self, user_id: str) -> list[dict[str, object]]:
        with self._session_factory() as db:
            rows = db.scalars(select(ProfileRow).where(ProfileRow.user_id == user_id).order_by(ProfileRow.created_at)).all()
            return [dict(row.payload) for row in rows]

    def get_profile(self, user_id: str, profile_id: str) -> dict[str, object]:
        """Return a profile by stable ID.

        ``user_id`` remains in the method signature for API compatibility.
        Guardian/profile authorization is owned by the forwarding backend;
        this service only needs the profile record for an already trusted
        request.
        """
        return self.get_profile_by_id(profile_id)

    def get_profile_by_id(self, profile_id: str) -> dict[str, object]:
        with self._session_factory() as db:
            row = db.get(ProfileRow, profile_id)
            if row is None:
                raise KeyError(profile_id)
            return dict(row.payload)

    def create_profile(
        self,
        user_id: str,
        *,
        name: str,
        shared_facts_initialized: bool = False,
        is_default: bool = False,
        profile_id: str | None = None,
    ) -> dict[str, object]:
        now = datetime.now(timezone.utc).isoformat()
        profile_id = profile_id or f"prof_{uuid4().hex[:12]}"
        with self._session_factory.begin() as db:
            existing = db.scalars(select(ProfileRow).where(ProfileRow.user_id == user_id)).all()
            selected_default = is_default or not existing
            if selected_default:
                for row in existing:
                    updated = dict(row.payload)
                    updated["is_default"] = False
                    updated["updated_at"] = now
                    row.payload = updated
                    row.version += 1
            payload: dict[str, object] = {
                "profile_id": profile_id, "name": name.strip() or "未命名孩子", "created_at": now,
                "updated_at": now, "is_default": selected_default,
                "shared_facts_initialized": shared_facts_initialized,
            }
            db.add(ProfileRow(profile_id=profile_id, user_id=user_id, payload=payload, facts={}))
            return dict(payload)

    def update_profile(self, user_id: str, profile_id: str, *, name: str | None = None, is_default: bool | None = None) -> dict[str, object]:
        now = datetime.now(timezone.utc).isoformat()
        with self._session_factory.begin() as db:
            row = db.get(ProfileRow, profile_id)
            if row is None or row.user_id != user_id:
                raise KeyError(profile_id)
            if is_default:
                for other in db.scalars(select(ProfileRow).where(ProfileRow.user_id == user_id)).all():
                    if other.profile_id != profile_id:
                        payload = dict(other.payload)
                        payload["is_default"] = False
                        payload["updated_at"] = now
                        other.payload = payload
                        other.version += 1
            payload = dict(row.payload)
            if name is not None:
                payload["name"] = name.strip() or str(payload.get("name") or "未命名孩子")
            if is_default is not None:
                payload["is_default"] = is_default
            payload["updated_at"] = now
            row.payload = payload
            row.version += 1
            return dict(payload)

    def get_profile_facts(self, user_id: str, profile_id: str) -> KnownFacts:
        with self._session_factory() as db:
            row = db.get(ProfileRow, profile_id)
            if row is None:
                return KnownFacts()
            return _facts_from_payload(row.facts)

    def save_profile_facts(self, user_id: str, profile_id: str, facts: KnownFacts) -> KnownFacts:
        with self._session_factory.begin() as db:
            row = db.get(ProfileRow, profile_id)
            if row is None:
                raise KeyError(profile_id)
            row.facts = _facts_to_payload(facts)
            row.version += 1
            return facts
