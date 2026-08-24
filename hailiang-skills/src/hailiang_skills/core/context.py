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
    # ``messages`` and the runtime fields above always describe the currently
    # active child branch.  Inactive branches are serialized here so the
    # runtime can keep using the existing SessionContext contract without ever
    # receiving another child's prompt history.
    profile_branches: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The user-facing conversation is one append-only timeline.  Message items
    # are tagged with their immutable profile_id while profile_switch items are
    # presentation-only and never copied into ``messages``.
    timeline_items: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._ensure_message_ids()
        self._ensure_timeline_items()
        self.refresh_effective_facts()

    @staticmethod
    def _facts_payload(facts: KnownFacts) -> dict[str, Any]:
        return {key: record.model_dump(mode="json") for key, record in facts.facts.items()}

    @staticmethod
    def _facts_from_payload(payload: dict[str, Any] | None) -> KnownFacts:
        facts = KnownFacts()
        for key, value in (payload or {}).items():
            facts.facts[str(key)] = FactRecord.model_validate(value)
        return facts

    @staticmethod
    def _is_global_meta_key(key: str) -> bool:
        return key.startswith("_storage_") or key in {
            "external_run_ids",
            "run_ledger",
            "sse_v2_runs",
            "active_stream_generation",
            "cancelled_stream_generation",
            "superseded_run_id",
            "stream_generation_by_thread",
            "_session_created",
            "_profile_switched",
            "_profile_branch_created",
            "_sse_profile_context",
        }

    def _global_session_meta(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.session_meta.items()
            if self._is_global_meta_key(str(key)) and not callable(value)
        }

    def _branch_session_meta(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.session_meta.items()
            if not self._is_global_meta_key(str(key)) and not callable(value)
        }

    def _ensure_timeline_items(self) -> None:
        if self.timeline_items:
            return
        for message in self.messages:
            if not isinstance(message, dict):
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            metadata.setdefault("profile_id", self.profile_id)
            metadata.setdefault("profile_name", self.profile_name)
            message["metadata"] = metadata
            self.timeline_items.append({
                "item_id": str(message.get("message_id") or f"item_{uuid4().hex[:16]}"),
                "item_type": "message",
                "profile_id": self.profile_id,
                "profile_name": self.profile_name,
                "message": message,
                "model_visible": True,
                "created_at": message.get("created_at") or datetime.now(timezone.utc).isoformat(),
            })

    def sync_active_branch(self) -> None:
        """Persist the active runtime view into its isolated child branch."""
        profile_id = str(self.profile_id or "").strip()
        if not profile_id:
            return
        previous = self.profile_branches.get(profile_id) or {}
        branch_version = int(
            self.session_meta.get("_active_branch_version")
            or previous.get("branch_version")
            or 1
        )
        self.profile_branches[profile_id] = {
            "profile_id": profile_id,
            "profile_name": self.profile_name,
            "messages": self.messages,
            "session_facts": self._facts_payload(self.session_facts),
            "skill_states": self.skill_states,
            "candidate_paths": self.candidate_paths,
            "interaction_state": self.interaction_state,
            "risk_signals": self.risk_signals,
            "event_trace": self.event_trace[-200:],
            "session_meta": self._branch_session_meta(),
            "last_fact_changes": self.last_fact_changes,
            "branch_version": branch_version,
        }
        self.session_meta["_active_branch_version"] = branch_version
        self._sync_timeline_messages()

    def _sync_timeline_messages(self) -> None:
        by_id = {
            str(item.get("item_id") or ""): item
            for item in self.timeline_items
            if isinstance(item, dict) and item.get("item_type") == "message"
        }
        for message in self.messages:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("message_id") or "")
            item = by_id.get(message_id)
            if item is None:
                self.timeline_items.append({
                    "item_id": message_id or f"item_{uuid4().hex[:16]}",
                    "item_type": "message",
                    "profile_id": self.profile_id,
                    "profile_name": self.profile_name,
                    "message": message,
                    "model_visible": True,
                    "created_at": message.get("created_at") or datetime.now(timezone.utc).isoformat(),
                })
            else:
                item["message"] = message
                item["profile_id"] = self.profile_id
                item["profile_name"] = self.profile_name

    def activate_profile_branch(
        self,
        profile_id: str,
        *,
        profile_name: str | None = None,
        mark_resume: bool = True,
    ) -> bool:
        """Activate one child branch and return whether it had to be created."""
        target = str(profile_id or "").strip()
        if not target:
            raise ValueError("profile_id is required")
        current = str(self.profile_id or "").strip()
        if current == target:
            if profile_name:
                self.profile_name = profile_name
            return False
        if current:
            self.sync_active_branch()
        global_meta = self._global_session_meta()
        branch = self.profile_branches.get(target)
        self.profile_id = target
        self.profile_name = profile_name or (str(branch.get("profile_name") or "") if branch else None)
        if branch is None:
            self.messages = []
            self.session_facts = KnownFacts()
            self.skill_states = {}
            self.candidate_paths = []
            self.interaction_state = {}
            self.risk_signals = []
            self.event_trace = []
            self.last_fact_changes = []
            self.session_meta = global_meta
            self.session_meta["_active_branch_version"] = 1
            return True
        self.messages = list(branch.get("messages") or [])
        self.session_facts = self._facts_from_payload(branch.get("session_facts"))
        self.skill_states = dict(branch.get("skill_states") or {})
        self.candidate_paths = list(branch.get("candidate_paths") or [])
        self.interaction_state = dict(branch.get("interaction_state") or {})
        self.risk_signals = list(branch.get("risk_signals") or [])
        self.event_trace = list(branch.get("event_trace") or [])
        self.last_fact_changes = list(branch.get("last_fact_changes") or [])
        self.session_meta = {**global_meta, **dict(branch.get("session_meta") or {})}
        self.session_meta["_active_branch_version"] = int(branch.get("branch_version") or 1) + 1
        if mark_resume and self.messages:
            self.session_meta["resume_recap_pending"] = True
        self._ensure_message_ids()
        return False

    def append_profile_switch(self, *, from_profile_id: str, from_profile_name: str | None = None) -> None:
        self.timeline_items.append({
            "item_id": f"switch_{uuid4().hex[:16]}",
            "item_type": "profile_switch",
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "from_profile_id": from_profile_id,
            "from_profile_name": from_profile_name,
            "to_profile_id": self.profile_id,
            "to_profile_name": self.profile_name,
            "model_visible": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

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
        message["metadata"]["profile_id"] = self.profile_id
        message["metadata"]["profile_name"] = self.profile_name
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
        self.timeline_items.append({
            "item_id": message["message_id"],
            "item_type": "message",
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "message": message,
            "model_visible": True,
            "created_at": message["created_at"],
        })
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
