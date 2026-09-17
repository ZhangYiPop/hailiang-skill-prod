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


# This is a storage-only branch key.  It is never exposed as a child profile
# identifier through the public API, SSE protocol, logs, or UI.
UNBOUND_CONTEXT_BRANCH_ID = "__unbound__"


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
    # active context branch.  A branch can be a child profile or the internal
    # unbound branch; inactive branches are serialized so the runtime never
    # receives another child's prompt history.
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
            "configuration_snapshot",
            # The chosen Team/member belongs to the whole user-visible
            # session. Child branches still own their histories, facts,
            # forms and active Skill, but a later child switch must not make
            # the product forget which Agent the user was speaking with.
            "session_agent_selection",
        }

    def session_agent_selection(self) -> dict[str, Any]:
        """Return the session-wide Agent selection without exposing branches.

        Older sessions do not have this record.  They intentionally remain
        ordinary chat until an explicit selection happens; copying legacy
        branch state here would accidentally turn general chat into an Expert
        conversation during a child switch.
        """
        raw = self.session_meta.get("session_agent_selection")
        if not isinstance(raw, dict):
            return {
                "expert_team_id": None,
                "expert_id": None,
                "selection_source": "",
                "selection_version": 0,
            }
        return {
            "expert_team_id": str(raw.get("expert_team_id") or "").strip() or None,
            "expert_id": str(raw.get("expert_id") or "").strip() or None,
            "selection_source": str(raw.get("selection_source") or "").strip(),
            "selection_version": int(raw.get("selection_version") or 0),
        }

    def set_session_agent_selection(
        self,
        *,
        expert_team_id: str | None,
        expert_id: str | None,
        selection_source: str,
    ) -> dict[str, Any]:
        """Persist the last actually selected Agent for every child branch."""
        previous = self.session_agent_selection()
        next_selection = {
            "expert_team_id": str(expert_team_id or "").strip() or None,
            "expert_id": str(expert_id or "").strip() or None,
            "selection_source": str(selection_source or "").strip(),
        }
        changed = any(
            previous[key] != next_selection[key]
            for key in ("expert_team_id", "expert_id", "selection_source")
        )
        next_selection["selection_version"] = previous["selection_version"] + (1 if changed else 0)
        self.session_meta["session_agent_selection"] = next_selection
        return dict(next_selection)

    def abandon_active_interactions_for_expert_change(
        self,
        *,
        reason: str,
        from_expert_id: str | None,
        target_expert_id: str | None,
        preserve_team_handoff: bool = False,
    ) -> list[dict[str, str]]:
        """End branch-local work that cannot safely move to another expert.

        A native fact form is tied to the Skill/Expert that produced it.  An
        explicit expert change must therefore preserve it as history but make
        it read-only, rather than rejecting the user's navigation request.
        Route and handoff cards use the same interaction lifecycle and are
        expired alongside the form, matching the existing switch semantics.
        """
        changes = expire_active_interactions(
            self.messages,
            preserve_kinds={"team_handoff", "route_suggestions"} if preserve_team_handoff else None,
        )
        abandoned_forms = [
            change
            for change in changes
            if str(change.get("interaction_id") or "").startswith("fact_form:")
        ]

        def append_runtime_form(form_id: object) -> None:
            normalized_form_id = str(form_id or "").strip()
            if not normalized_form_id:
                return
            interaction_id = f"fact_form:{normalized_form_id}"
            if any(change["interaction_id"] == interaction_id for change in abandoned_forms):
                return
            source_message_id = ""
            for message in reversed(self.messages):
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                blocks = message.get("blocks") if isinstance(message.get("blocks"), list) else []
                if any(
                    isinstance(block, dict)
                    and block.get("type") == "fact_form"
                    and isinstance(block.get("payload"), dict)
                    and str(block["payload"].get("form_id") or "") == normalized_form_id
                    for block in blocks
                ):
                    source_message_id = str(message.get("message_id") or "")
                    break
            abandoned_forms.append({"message_id": source_message_id, "interaction_id": interaction_id})

        runtime_state = self.skill_states.get("skill_runtime")
        if isinstance(runtime_state, dict):
            runtime_state.pop("pending_form", None)
            runtime_state["active_skill_id"] = ""
            status_flags = runtime_state.get("status_flags")
            if isinstance(status_flags, dict):
                status_flags.pop("native_questionnaire_form", None)
            for facts in (runtime_state.get("skill_facts") or {}).values():
                if isinstance(facts, dict):
                    pending_questionnaire = facts.get("_pending_questionnaire")
                    if isinstance(pending_questionnaire, dict):
                        append_runtime_form(pending_questionnaire.get("form_id"))
                    facts.pop("_pending_questionnaire", None)
        agent_state = self.skill_states.get("agent_runtime")
        if isinstance(agent_state, dict):
            pending_form = agent_state.get("pending_form")
            if isinstance(pending_form, dict):
                append_runtime_form(pending_form.get("form_id"))
            agent_state.pop("pending_form", None)
        self.session_meta.pop("pending_form", None)
        self.interaction_state["active_skill"] = ""

        if abandoned_forms:
            self.event_trace.append(
                make_event(
                    "form_abandoned",
                    {
                        "reason": reason,
                        "from_expert_id": str(from_expert_id or "") or None,
                        "target_expert_id": str(target_expert_id or "") or None,
                        "forms": [
                            {
                                "source_message_id": change["message_id"],
                                "interaction_id": change["interaction_id"],
                                "form_id": change["interaction_id"].removeprefix("fact_form:"),
                            }
                            for change in abandoned_forms
                        ],
                    },
                )
            )
        return changes

    def apply_session_agent_selection(self) -> bool:
        """Restore the session Agent into the active child/unbound branch.

        This deliberately resets only branch-local *live* interactions when
        the Agent changes.  Historical messages and Facts survive, while a
        form, handoff card or active Skill created under another Agent can no
        longer be submitted against the new execution owner.
        """
        # A cross-child handoff is authorized by a card produced in another
        # branch, but its selected Expert must belong only to the execution
        # branch.  Prefer that branch-local override for ordinary
        # ``null/null/continue`` follow-ups; otherwise a later chat would
        # incorrectly restore the session-wide Agent and undo the handoff.
        override = self.session_meta.get("branch_expert_override")
        if isinstance(override, dict):
            team_id = str(override.get("expert_team_id") or "").strip() or None
            expert_id = str(override.get("expert_id") or "").strip() or None
            if team_id or expert_id:
                selection_present = True
                selection_source = str(override.get("source") or "cross_profile_handoff").strip()
            else:
                selection_present = False
                selection_source = ""
        else:
            selection_present = isinstance(self.session_meta.get("session_agent_selection"), dict)
            selection = self.session_agent_selection()
            team_id = selection["expert_team_id"]
            expert_id = selection["expert_id"]
            selection_source = selection["selection_source"]
        if not selection_present:
            return False
        current_team_id = str(self.session_meta.get("expert_team_id") or "").strip() or None
        current_expert_id = str(
            self.session_meta.get("active_expert_id") or self.session_meta.get("expert_id") or ""
        ).strip() or None
        changed = (current_team_id, current_expert_id) != (team_id, expert_id)
        if team_id or expert_id:
            self.session_meta["expert_team_id"] = team_id
            self.session_meta["expert_id"] = expert_id
            self.session_meta["active_expert_id"] = expert_id
            self.session_meta["expert_selection_source"] = selection_source
        else:
            for key in ("expert_team_id", "expert_id", "active_expert_id", "expert_requested_skill_id", "pending_team_handoff"):
                self.session_meta.pop(key, None)
            self.session_meta["expert_selection_source"] = selection_source
        if not changed:
            return False
        self.abandon_active_interactions_for_expert_change(
            reason="context_agent_inheritance",
            from_expert_id=current_expert_id,
            target_expert_id=expert_id,
        )
        self.session_meta.pop("pending_team_handoff", None)
        self.session_meta.pop("expert_requested_skill_id", None)
        self.interaction_state["active_skill"] = "career_plan_entity" if (team_id or expert_id) else "general_chat"
        runtime_state = self.skill_states.get("skill_runtime")
        if isinstance(runtime_state, dict):
            runtime_state["active_skill_id"] = "career_plan_entity" if (team_id or expert_id) else "general_chat"
        agent_state = self.skill_states.get("agent_runtime")
        if isinstance(agent_state, dict):
            agent_state["active_expert_id"] = expert_id
        return True

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

    @property
    def context_scope(self) -> str:
        """Return the public scope of the active branch."""
        return "profile" if str(self.profile_id or "").strip() else "unbound"

    @property
    def context_label(self) -> str:
        if self.context_scope == "profile":
            return self.profile_name or "未命名孩子"
        return "未绑定孩子"

    def _active_branch_key(self) -> str:
        return str(self.profile_id or "").strip() or UNBOUND_CONTEXT_BRANCH_ID

    def sync_active_branch(self) -> None:
        """Persist the active runtime view into its isolated context branch."""
        branch_key = self._active_branch_key()
        previous = self.profile_branches.get(branch_key) or {}
        branch_version = int(
            self.session_meta.get("_active_branch_version")
            or previous.get("branch_version")
            or 1
        )
        self.profile_branches[branch_key] = {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "context_scope": self.context_scope,
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
        # A form belongs to the currently visible child. Preserve confirmed
        # facts/state, but expire live UI work before it is serialized so a
        # return to this child rebuilds the smallest valid next question.
        if current:
            self.abandon_active_interactions_for_expert_change(
                reason="profile_switch",
                from_expert_id=str(self.session_meta.get("active_expert_id") or "") or None,
                target_expert_id=None,
                # Unlike a form, a team-handoff card is a structured
                # authorization record and can be confirmed for the target
                # child without exposing this branch's context.
                preserve_team_handoff=True,
            )
        # The active branch may be the unbound branch (whose public profile
        # id is ``None``), so it must be persisted before entering a child as
        # well.
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

    def activate_unbound_branch(self, *, mark_resume: bool = True) -> bool:
        """Activate the session-local branch that has no child profile."""
        if self.context_scope == "unbound":
            return False
        if self.profile_id:
            self.abandon_active_interactions_for_expert_change(
                reason="profile_switch",
                from_expert_id=str(self.session_meta.get("active_expert_id") or "") or None,
                target_expert_id=None,
                preserve_team_handoff=True,
            )
            self.sync_active_branch()
        global_meta = self._global_session_meta()
        branch = self.profile_branches.get(UNBOUND_CONTEXT_BRANCH_ID)
        self.profile_id = None
        self.profile_name = None
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

    def append_context_switch(
        self,
        *,
        from_profile_id: str | None,
        from_profile_name: str | None = None,
    ) -> None:
        """Append a display-only scope transition without exposing branch keys."""
        self.timeline_items.append({
            "item_id": f"switch_{uuid4().hex[:16]}",
            "item_type": "profile_switch",
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "context_scope": self.context_scope,
            "from_profile_id": from_profile_id,
            "from_profile_name": from_profile_name,
            "from_context_scope": "profile" if from_profile_id else "unbound",
            "to_profile_id": self.profile_id,
            "to_profile_name": self.profile_name,
            "to_context_scope": self.context_scope,
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
            if record.status != "candidate":
                merged.facts[key] = record
        for key, record in self.profile_facts.facts.items():
            if record.status != "candidate":
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
        status: str = "confirmed",
        evidence_summary: str | None = None,
    ) -> FactRecord:
        fact_scope = resolve_fact_scope(key, scope)
        # Unbound conversations may collect information, but it must stay in
        # this session branch even when a reusable Skill declares a profile or
        # shared Fact field.
        if self.context_scope == "unbound":
            fact_scope = "session"
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
            status=status,
            evidence_summary=evidence_summary,
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
