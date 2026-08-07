from __future__ import annotations

from hailiang_skills.core.session_logging import (
    load_user_facts_snapshot,
    write_user_facts_snapshot,
)
from hailiang_skills.schemas.facts import FactRecord, KnownFacts


class FileBackedUserFactRepository:
    def __init__(self) -> None:
        self._items: dict[str, KnownFacts] = {}

    def get(self, user_id: str) -> KnownFacts:
        if user_id not in self._items:
            snapshot = load_user_facts_snapshot(user_id)
            facts = KnownFacts()
            for key, payload in (snapshot or {}).get("facts", {}).items():
                facts.facts[key] = FactRecord.model_validate(payload)
            self._items[user_id] = facts
        return self._items[user_id]

    def save(self, user_id: str, facts: KnownFacts) -> KnownFacts:
        self._items[user_id] = facts
        write_user_facts_snapshot(
            user_id,
            {key: record.model_dump() for key, record in facts.facts.items()},
        )
        return facts

    def reset(self, user_id: str, fact_keys: list[str]) -> KnownFacts:
        facts = self.get(user_id)
        for key in fact_keys:
            facts.reset_fact(key)
        return self.save(user_id, facts)
