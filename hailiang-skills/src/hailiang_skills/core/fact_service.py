from __future__ import annotations

from copy import deepcopy
from typing import Any

from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.fact_scope import (
    FACT_SCOPE_PROFILE,
    FACT_SCOPE_SESSION,
    FACT_SCOPE_SHARED,
)
from hailiang_skills.core.facts_config import get_enabled_fact_keys, get_fact_meta
from hailiang_skills.schemas.facts import normalize_fact_value
from hailiang_skills.schemas.facts import KnownFacts
from hailiang_skills.storage.repositories.profile_repo import FileBackedProfileRepository
from hailiang_skills.storage.repositories.user_fact_repo import FileBackedUserFactRepository
from hailiang_skills.core.telemetry import span


def serialize_known_facts(facts: KnownFacts) -> dict[str, Any]:
    return {key: record.model_dump() for key, record in facts.facts.items()}


def summarize_fact_sources(facts: KnownFacts) -> list[dict[str, Any]]:
    grouped: dict[tuple[str | None, str | None, str | None], dict[str, Any]] = {}
    for key, record in facts.facts.items():
        group_key = (record.source_type, record.source_id, record.source_label)
        bucket = grouped.setdefault(
            group_key,
            {
                "type": record.source_type,
                "source_id": record.source_id,
                "source_label": record.source_label,
                "fact_count": 0,
                "fact_keys": [],
            },
        )
        bucket["fact_count"] += 1
        bucket["fact_keys"].append(key)
    return sorted(
        grouped.values(),
        key=lambda item: (
            str(item.get("type") or ""),
            str(item.get("source_label") or ""),
            str(item.get("source_id") or ""),
        ),
    )


class FactService:
    def __init__(
        self,
        user_fact_repo: FileBackedUserFactRepository,
        profile_repo: FileBackedProfileRepository,
    ) -> None:
        self.user_fact_repo = user_fact_repo
        self.profile_repo = profile_repo

    @staticmethod
    def validate_configured_update(
        fact_key: str,
        value: Any,
        *,
        declared_value_type: str | None = None,
    ) -> Any | None:
        """Validate a server-selected Fact write before it reaches storage.

        Native Skills use this for their declarative questionnaire mappings.
        It intentionally accepts only configured, enabled keys; clients never
        get to choose a target Fact through the form payload.
        """
        if fact_key not in set(get_enabled_fact_keys()):
            return None
        meta = get_fact_meta(fact_key)
        expected_type = str(meta.get("value_type") or "string")
        if declared_value_type and str(declared_value_type) != expected_type:
            return None
        normalized = normalize_fact_value(fact_key, value)
        if normalized is None:
            return None
        if expected_type == "number" and (
            not isinstance(normalized, (int, float)) or isinstance(normalized, bool)
        ):
            return None
        if expected_type == "boolean" and not isinstance(normalized, bool):
            return None
        if expected_type == "string_list" and not isinstance(normalized, list):
            return None
        if expected_type == "string" and not isinstance(normalized, str):
            return None
        allowed_values = meta.get("allowed_values") or []
        if allowed_values:
            values = normalized if isinstance(normalized, list) else [normalized]
            if any(item not in allowed_values for item in values):
                return None
        return normalized

    def hydrate_context(self, context: SessionContext) -> SessionContext:
        with span("facts.hydrate", node="facts_hydrate"):
            shared_facts = self.user_fact_repo.get(context.user_id)
            profile_facts = KnownFacts()
            if context.profile_id:
                profile_facts = self.profile_repo.get_profile_facts(context.user_id, context.profile_id)
            context.load_effective_facts(
                shared_facts=deepcopy(shared_facts),
                profile_facts=deepcopy(profile_facts),
                session_facts=context.session_facts,
            )
            return context

    def get_shared_facts(self, user_id: str) -> KnownFacts:
        return self.user_fact_repo.get(user_id)

    def get_shared_facts_payload(self, user_id: str) -> dict[str, Any]:
        facts = self.get_shared_facts(user_id)
        return {
            "facts": serialize_known_facts(facts),
            "sources": summarize_fact_sources(facts),
        }

    def get_profile_facts(self, user_id: str, profile_id: str) -> KnownFacts:
        return self.profile_repo.get_profile_facts(user_id, profile_id)

    def get_profile_facts_payload(self, user_id: str, profile_id: str) -> dict[str, Any]:
        facts = self.get_profile_facts(user_id, profile_id)
        return {
            "facts": serialize_known_facts(facts),
            "sources": summarize_fact_sources(facts),
        }

    def get_user_facts(self, user_id: str) -> KnownFacts:
        return self.get_shared_facts(user_id)

    def get_user_facts_payload(self, user_id: str) -> dict[str, Any]:
        payload = self.get_shared_facts_payload(user_id)
        return payload

    def get_session_facts_payload(self, context: SessionContext) -> dict[str, Any]:
        self.hydrate_context(context)
        return {
            "user_facts": serialize_known_facts(context.user_facts),
            "shared_facts": serialize_known_facts(context.shared_facts),
            "profile_facts": serialize_known_facts(context.profile_facts),
            "session_facts": serialize_known_facts(context.session_facts),
            "effective_facts": serialize_known_facts(context.known_facts),
        }

    def persist_context(self, context: SessionContext) -> None:
        with span("facts.persist", node="facts_persist"):
            self.user_fact_repo.save(context.user_id, context.shared_facts)
            if context.profile_id:
                self.profile_repo.save_profile_facts(
                    context.user_id,
                    context.profile_id,
                    context.profile_facts,
                )

    def batch_upsert(
        self,
        context: SessionContext,
        updates: list[dict[str, Any]],
        *,
        scope: str,
        source: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.hydrate_context(context)
        changes: list[dict[str, Any]] = []
        for item in updates:
            key = item["key"]
            value = item.get("value")
            before = context.known_facts.get_value(key)
            record = context.update_fact(
                key,
                value,
                source_skill=(source or {}).get("type", "manual_update"),
                source_type=(source or {}).get("type", "manual_update"),
                source_id=(source or {}).get("source_id"),
                source_label=(source or {}).get("source_label"),
                source_turn_id=(source or {}).get("turn_id"),
                scope=scope,
            )
            changes.append(
                {
                    "key": key,
                    "before": before,
                    "after": record.value,
                    "source": {
                        "type": record.source_type,
                        "source_id": record.source_id,
                        "source_label": record.source_label,
                    },
                    "scope": record.scope,
                    "updated_at": record.updated_at,
                }
            )
        self._persist_scope(context, scope)
        context.last_fact_changes = changes
        return changes

    def reset(
        self,
        context: SessionContext,
        fact_keys: list[str],
        *,
        scope: str,
        source: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.hydrate_context(context)
        changes: list[dict[str, Any]] = []
        for key in fact_keys:
            before = context.known_facts.get_value(key)
            context.reset_fact(key, scope=scope)
            changes.append(
                {
                    "key": key,
                    "before": before,
                    "after": None,
                    "source": source or {"type": "manual_reset"},
                    "scope": scope,
                    "updated_at": None,
                }
            )
        self._persist_scope(context, scope)
        context.last_fact_changes = changes
        return changes

    def clear_user_facts_by_source(
        self,
        context: SessionContext,
        *,
        selector: dict[str, Any],
    ) -> list[dict[str, Any]]:
        self.hydrate_context(context)
        source_type = selector.get("type")
        source_id = selector.get("source_id")
        source_label = selector.get("source_label")
        if not any([source_type, source_id, source_label]):
            raise ValueError("source selector is required")

        changes: list[dict[str, Any]] = []
        to_remove: list[str] = []
        for key, record in context.shared_facts.facts.items():
            if source_type and record.source_type != source_type:
                continue
            if source_id and record.source_id != source_id:
                continue
            if source_label and record.source_label != source_label:
                continue
            to_remove.append(key)
            changes.append(
                {
                    "key": key,
                    "before": record.value,
                    "after": None,
                    "source": {
                        "type": record.source_type,
                        "source_id": record.source_id,
                        "source_label": record.source_label,
                    },
                    "scope": FACT_SCOPE_SHARED,
                    "updated_at": None,
                }
            )

        for key in to_remove:
            context.shared_facts.reset_fact(key)

        context.refresh_effective_facts()
        self.user_fact_repo.save(context.user_id, context.shared_facts)
        context.last_fact_changes = changes
        return changes

    def _persist_scope(self, context: SessionContext, scope: str) -> None:
        if scope == FACT_SCOPE_SHARED:
            self.user_fact_repo.save(context.user_id, context.shared_facts)
        elif scope == FACT_SCOPE_PROFILE and context.profile_id:
            self.profile_repo.save_profile_facts(
                context.user_id,
                context.profile_id,
                context.profile_facts,
            )
        elif scope == FACT_SCOPE_SESSION:
            # session facts are persisted via session repository snapshot save
            return
