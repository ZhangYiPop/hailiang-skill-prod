"""Profile archive persistence and deterministic retrieval.

The public contract intentionally does not expose the ranking implementation,
so a later pgvector-backed implementation can replace this repository without
changing callers.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from hailiang_skills.core.context_composer import estimate_tokens
from hailiang_skills.storage.database import ConversationMemoryCheckpointRow, ProfileMemoryRow


def _tokens(text: str) -> set[str]:
    normalized = str(text or "").lower()
    latin = re.findall(r"[a-z0-9_]{2,}", normalized)
    cjk = [normalized[index : index + 2] for index in range(max(0, len(normalized) - 1)) if "\u4e00" <= normalized[index] <= "\u9fff"]
    return set(latin + cjk)


class InMemoryProfileMemoryRepository:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def record_candidate(self, **item: Any) -> dict[str, Any]:
        memory_id = str(item.get("memory_id") or f"pm_{uuid4().hex}")
        previous = next((record for record in self.items.values() if record["profile_id"] == item["profile_id"] and record["fact_key"] == item["fact_key"] and record.get("status") == "active"), None)
        history = list((previous or {}).get("history", {}).get("recent", []))
        if previous:
            history.append(_observation(previous))
            previous["status"] = "superseded"
            previous["superseded_by"] = memory_id
        record = _record(memory_id=memory_id, history=history, **item)
        self.items[memory_id] = record
        return dict(record)

    def retrieve(self, profile_id: str, query_text: str, *, skill_id: str = "", domain: str = "", limit: int = 12, token_budget: int = 24_000) -> list[dict[str, Any]]:
        candidates = [dict(item) for item in self.items.values() if item["profile_id"] == profile_id and item.get("status") == "active"]
        return _rank(candidates, query_text, skill_id=skill_id, domain=domain, limit=limit, token_budget=token_budget)

    def compact_history(self, memory_id: str) -> dict[str, Any] | None:
        item = self.items.get(memory_id)
        if item is None:
            return None
        item["history"] = _compact_history(item.get("history") or {})
        return dict(item)

    def confirm(self, memory_id: str) -> dict[str, Any] | None:
        item = self.items.get(memory_id)
        if item is None or item.get("status") != "active":
            return None
        item["kind"] = "confirmed"
        return dict(item)

    def supersede(self, memory_id: str, replacement_id: str | None = None) -> dict[str, Any] | None:
        item = self.items.get(memory_id)
        if item is None:
            return None
        item["status"] = "superseded"
        item["superseded_by"] = replacement_id
        return dict(item)


class InMemoryConversationMemoryRepository:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def load(self, session_id: str, profile_id: str) -> dict[str, Any] | None:
        payload = self.items.get((session_id, profile_id))
        return dict(payload) if isinstance(payload, dict) else None

    def save(self, session_id: str, profile_id: str, payload: dict[str, Any]) -> None:
        self.items[(session_id, profile_id)] = dict(payload)


class PostgresProfileMemoryRepository:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def record_candidate(self, **item: Any) -> dict[str, Any]:
        memory_id = str(item.get("memory_id") or f"pm_{uuid4().hex}")
        with self._session_factory.begin() as db:
            previous = db.scalars(
                select(ProfileMemoryRow)
                .where(ProfileMemoryRow.profile_id == str(item["profile_id"]), ProfileMemoryRow.fact_key == str(item["fact_key"]), ProfileMemoryRow.status == "active")
                .order_by(ProfileMemoryRow.observed_at.desc())
            ).first()
            recent = list((previous.history or {}).get("recent", [])) if previous else []
            if previous is not None:
                recent.append(_observation(_row_payload(previous)))
                previous.status = "superseded"
                previous.superseded_by = memory_id
            record = _record(memory_id=memory_id, history=recent, **item)
            row = ProfileMemoryRow(**{**record, "value": {"value": record["value"]}})
            db.add(row)
            db.flush()
            return _row_payload(row)

    def retrieve(self, profile_id: str, query_text: str, *, skill_id: str = "", domain: str = "", limit: int = 12, token_budget: int = 24_000) -> list[dict[str, Any]]:
        with self._session_factory() as db:
            rows = db.scalars(
                select(ProfileMemoryRow)
                .where(ProfileMemoryRow.profile_id == str(profile_id), ProfileMemoryRow.status == "active")
                .order_by(ProfileMemoryRow.observed_at.desc())
                .limit(200)
            ).all()
            return _rank([_row_payload(row) for row in rows], query_text, skill_id=skill_id, domain=domain, limit=limit, token_budget=token_budget)

    def compact_history(self, memory_id: str) -> dict[str, Any] | None:
        with self._session_factory.begin() as db:
            row = db.get(ProfileMemoryRow, memory_id)
            if row is None:
                return None
            row.history = _compact_history(row.history or {})
            return _row_payload(row)

    def confirm(self, memory_id: str) -> dict[str, Any] | None:
        with self._session_factory.begin() as db:
            row = db.get(ProfileMemoryRow, memory_id)
            if row is None or row.status != "active":
                return None
            row.kind = "confirmed"
            return _row_payload(row)

    def supersede(self, memory_id: str, replacement_id: str | None = None) -> dict[str, Any] | None:
        with self._session_factory.begin() as db:
            row = db.get(ProfileMemoryRow, memory_id)
            if row is None:
                return None
            row.status = "superseded"
            row.superseded_by = replacement_id
            return _row_payload(row)


class PostgresConversationMemoryRepository:
    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def load(self, session_id: str, profile_id: str) -> dict[str, Any] | None:
        with self._session_factory() as db:
            row = db.get(ConversationMemoryCheckpointRow, {"session_id": session_id, "profile_id": profile_id})
            return dict(row.payload) if row is not None and isinstance(row.payload, dict) else None

    def save(self, session_id: str, profile_id: str, payload: dict[str, Any]) -> None:
        with self._session_factory.begin() as db:
            row = db.get(ConversationMemoryCheckpointRow, {"session_id": session_id, "profile_id": profile_id})
            if row is None:
                row = ConversationMemoryCheckpointRow(session_id=session_id, profile_id=profile_id, payload=dict(payload))
                db.add(row)
            else:
                row.payload = dict(payload)


def _record(*, memory_id: str, history: list[dict[str, Any]], **item: Any) -> dict[str, Any]:
    recent = history[-3:]
    compacted = history[:-3]
    value = item.get("value")
    return {
        "memory_id": memory_id,
        "profile_id": str(item["profile_id"]),
        "kind": str(item.get("kind") or "candidate"),
        "status": "active",
        "fact_key": str(item["fact_key"]),
        "value": value,
        "domain": str(item.get("domain") or "") or None,
        "skill_id": str(item.get("skill_id") or "") or None,
        "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0.5))),
        "observed_at": datetime.now(UTC),
        "source_session_id": str(item.get("source_session_id") or "") or None,
        "source_turn_id": str(item.get("source_turn_id") or "") or None,
        "evidence_summary": str(item.get("evidence_summary") or "")[:500] or None,
        "history": {"recent": recent, "summary": _history_summary(compacted)},
        "superseded_by": None,
    }


def _row_payload(row: ProfileMemoryRow) -> dict[str, Any]:
    return {
        "memory_id": row.memory_id, "profile_id": row.profile_id, "kind": row.kind, "status": row.status,
        "fact_key": row.fact_key, "value": (row.value or {}).get("value"), "domain": row.domain,
        "skill_id": row.skill_id, "confidence": row.confidence, "observed_at": row.observed_at.isoformat(),
        "source_session_id": row.source_session_id, "source_turn_id": row.source_turn_id,
        "evidence_summary": row.evidence_summary, "history": row.history or {}, "superseded_by": row.superseded_by,
    }


def _observation(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in ("value", "confidence", "observed_at", "source_session_id", "source_turn_id", "evidence_summary")}


def _history_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    return "；".join(f"{str(item.get('observed_at') or '')[:10]}：{str(item.get('value') or '')[:80]}" for item in items[-8:])[:800]


def _compact_history(history: dict[str, Any]) -> dict[str, Any]:
    recent = list(history.get("recent") or [])[-3:]
    return {"recent": recent, "summary": str(history.get("summary") or "")[:800]}


def _rank(records: list[dict[str, Any]], query_text: str, *, skill_id: str, domain: str, limit: int, token_budget: int) -> list[dict[str, Any]]:
    query = _tokens(query_text)
    now = datetime.now(UTC)
    def score(item: dict[str, Any]) -> float:
        corpus = " ".join(str(item.get(key) or "") for key in ("fact_key", "value", "domain", "skill_id", "evidence_summary"))
        overlap = len(query & _tokens(corpus)) * 2.0
        skill_bonus = 3.0 if skill_id and item.get("skill_id") == skill_id else 0.0
        domain_bonus = 2.0 if domain and item.get("domain") == domain else 0.0
        observed = item.get("observed_at")
        try:
            observed_at = datetime.fromisoformat(str(observed))
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=UTC)
            age_days = max(0.0, (now - observed_at).total_seconds() / 86400)
        except (TypeError, ValueError):
            age_days = 365.0
        return overlap + skill_bonus + domain_bonus + float(item.get("confidence") or 0) * 2 + max(0.0, 1 - age_days / 365)
    used = 0
    result: list[dict[str, Any]] = []
    for item in sorted(records, key=score, reverse=True):
        public = {**item, "retrieval_score": round(score(item), 4)}
        cost = estimate_tokens(public)
        if result and used + cost > token_budget:
            break
        if not result and cost > token_budget:
            public["history"] = {"summary": str((public.get("history") or {}).get("summary") or "")[:300]}
        result.append(public)
        used += estimate_tokens(public)
        if len(result) >= max(1, limit):
            break
    return result
