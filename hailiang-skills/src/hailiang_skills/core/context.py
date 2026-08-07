from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from hailiang_skills.core.fact_scope import (
    FACT_SCOPE_PROFILE,
    FACT_SCOPE_SHARED,
    resolve_fact_scope,
)
from hailiang_skills.schemas.facts import FactRecord, KnownFacts
from hailiang_skills.schemas.provenance import Provenance
from hailiang_skills.core.message_interactions import backfill_interactions, expire_active_interactions
from hailiang_skills.core.logging import make_event


@dataclass
class SessionContext:
    session_id: str = field(default_factory=lambda: f"sess_{uuid4().hex[:12]}")
    user_id: str = "anonymous"
    profile_id: str | None = None
    profile_name: str | None = None
    title: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    known_facts: KnownFacts = field(default_factory=KnownFacts)
    shared_facts: KnownFacts = field(default_factory=KnownFacts)
    profile_facts: KnownFacts = field(default_factory=KnownFacts)
    session_facts: KnownFacts = field(default_factory=KnownFacts)
    skill_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidate_paths: list[dict[str, Any]] = field(default_factory=list)
    interaction_state: dict[str, Any] = field(default_factory=dict)
    risk_signals: list[str] = field(default_factory=list)
    event_trace: list[dict[str, Any]] = field(default_factory=list)
    asset_version: str = "dev"
    session_meta: dict[str, Any] = field(default_factory=dict)
    last_fact_changes: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._ensure_message_ids()
        self.refresh_effective_facts()

    def _ensure_message_ids(self) -> None:
        """Backfill IDs for snapshots created before message feedback existed."""
        for message in self.messages:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("message_id") or "").strip()
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                message["metadata"] = metadata
            if not message_id:
                message_id = f"msg_{uuid4().hex[:16]}"
                message["message_id"] = message_id
            metadata.setdefault("message_id", message_id)
        backfill_interactions(self.messages)

    def add_message(self, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        if role == "user":
            expired = expire_active_interactions(self.messages)
            if expired:
                self.event_trace.append(
                    make_event(
                        "message_interactions_expired",
                        {"reason": "new_user_message", "interactions": expired},
                    )
                )
        message = {
            "message_id": f"msg_{uuid4().hex[:16]}",
            "role": role,
            "content": content,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        message["metadata"] = {"message_id": message["message_id"]}
        if metadata:
            message["metadata"].update(metadata)
            for key in (
                "skill_id",
                "skill_name",
                "agent_label",
                "scene_name",
                "theme_key",
                "message_type",
                "skill_intro",
            ):
                if metadata.get(key):
                    message[key] = metadata[key]
        self.messages.append(message)
        if role == "user" and not self.title:
            self.title = content.strip()[:40] or "新会话"

    def refresh_effective_facts(self) -> None:
        merged = KnownFacts()
        for key, record in self.shared_facts.facts.items():
            merged.facts[key] = record
        for key, record in self.profile_facts.facts.items():
            merged.facts[key] = record
        for key, record in self.session_facts.facts.items():
            merged.facts[key] = record
        self.known_facts = merged

    def update_fact(
        self,
        key: str,
        value: Any,
        source_skill: str,
        confidence: float = 1.0,
        source_type: str = "skill",
        source_id: str | None = None,
        source_label: str | None = None,
        scope: str | None = None,
        source_turn_id: str | None = None,
        provenance: Provenance | None = None,
    ) -> FactRecord:
        fact_scope = resolve_fact_scope(key, scope)
        if fact_scope == FACT_SCOPE_SHARED:
            target = self.shared_facts
        elif fact_scope == FACT_SCOPE_PROFILE:
            target = self.profile_facts
        else:
            target = self.session_facts
        record = target.set_fact(
            key,
            value,
            source_skill=source_skill,
            confidence=confidence,
            source_type=source_type,
            source_id=source_id,
            source_label=source_label,
            scope=fact_scope,
            source_turn_id=source_turn_id,
            provenance=provenance,
        )
        self.refresh_effective_facts()
        return record

    def reset_fact(self, key: str, scope: str | None = None) -> FactRecord | None:
        fact_scope = resolve_fact_scope(key, scope)
        if fact_scope == FACT_SCOPE_SHARED:
            target = self.shared_facts
        elif fact_scope == FACT_SCOPE_PROFILE:
            target = self.profile_facts
        else:
            target = self.session_facts
        removed = target.reset_fact(key)
        self.refresh_effective_facts()
        return removed

    def clear_session_facts(self) -> None:
        self.session_facts = KnownFacts()
        self.refresh_effective_facts()

    def set_shared_facts(self, facts: KnownFacts) -> None:
        self.shared_facts = facts
        self.refresh_effective_facts()

    def set_profile_facts(self, facts: KnownFacts) -> None:
        self.profile_facts = facts
        self.refresh_effective_facts()

    def set_session_facts(self, facts: KnownFacts) -> None:
        self.session_facts = facts
        self.refresh_effective_facts()

    def load_effective_facts(
        self,
        shared_facts: KnownFacts | None = None,
        profile_facts: KnownFacts | None = None,
        session_facts: KnownFacts | None = None,
        user_facts: KnownFacts | None = None,
    ) -> None:
        if user_facts is not None and shared_facts is None:
            shared_facts = user_facts
        if shared_facts is not None:
            self.shared_facts = shared_facts
        if profile_facts is not None:
            self.profile_facts = profile_facts
        if session_facts is not None:
            self.session_facts = session_facts
        self.refresh_effective_facts()

    @property
    def user_facts(self) -> KnownFacts:
        return self.shared_facts

    @user_facts.setter
    def user_facts(self, facts: KnownFacts) -> None:
        self.shared_facts = facts

    def set_user_facts(self, facts: KnownFacts) -> None:
        self.shared_facts = facts
        self.refresh_effective_facts()

    def load_legacy_user_facts(
        self,
        user_facts: KnownFacts | None = None,
        session_facts: KnownFacts | None = None,
    ) -> None:
        if user_facts is not None:
            self.shared_facts = user_facts
        if session_facts is not None:
            self.session_facts = session_facts
        self.refresh_effective_facts()
