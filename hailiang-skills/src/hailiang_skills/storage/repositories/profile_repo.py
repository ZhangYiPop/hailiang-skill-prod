from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from hailiang_skills.core.session_logging import (
    load_profile_facts_snapshot,
    load_profiles_snapshot,
    write_profile_facts_snapshot,
    write_profiles_snapshot,
)
from hailiang_skills.schemas.facts import FactRecord, KnownFacts


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FileBackedProfileRepository:
    def __init__(self) -> None:
        self._profiles_cache: dict[str, list[dict[str, object]]] = {}
        self._facts_cache: dict[tuple[str, str], KnownFacts] = {}

    def list_profiles(self, user_id: str) -> list[dict[str, object]]:
        if user_id not in self._profiles_cache:
            snapshot = load_profiles_snapshot(user_id) or {}
            self._profiles_cache[user_id] = list(snapshot.get("profiles", []))
        return [dict(item) for item in self._profiles_cache[user_id]]

    def get_profile(self, user_id: str, profile_id: str) -> dict[str, object]:
        for item in self.list_profiles(user_id):
            if item.get("profile_id") == profile_id:
                return item
        raise KeyError(profile_id)

    def create_profile(
        self,
        user_id: str,
        *,
        name: str,
        shared_facts_initialized: bool = False,
        is_default: bool = False,
        profile_id: str | None = None,
    ) -> dict[str, object]:
        now = utc_now_iso()
        profiles = self.list_profiles(user_id)
        profile = {
            "profile_id": profile_id or f"prof_{uuid4().hex[:12]}",
            "name": name.strip() or "未命名孩子",
            "created_at": now,
            "updated_at": now,
            "is_default": is_default or not profiles,
            "shared_facts_initialized": shared_facts_initialized,
        }
        if profile["is_default"]:
            for item in profiles:
                item["is_default"] = False
        profiles.append(profile)
        self._profiles_cache[user_id] = profiles
        self._persist_profiles(user_id)
        return dict(profile)

    def update_profile(
        self,
        user_id: str,
        profile_id: str,
        *,
        name: str | None = None,
        is_default: bool | None = None,
    ) -> dict[str, object]:
        profiles = self.list_profiles(user_id)
        updated: dict[str, object] | None = None
        for item in profiles:
            if item.get("profile_id") != profile_id:
                if is_default:
                    item["is_default"] = False
                continue
            if name is not None:
                item["name"] = name.strip() or item.get("name") or "未命名孩子"
            if is_default is not None:
                item["is_default"] = is_default
            item["updated_at"] = utc_now_iso()
            updated = dict(item)
        if updated is None:
            raise KeyError(profile_id)
        self._profiles_cache[user_id] = profiles
        self._persist_profiles(user_id)
        return updated

    def get_profile_facts(self, user_id: str, profile_id: str) -> KnownFacts:
        cache_key = (user_id, profile_id)
        if cache_key not in self._facts_cache:
            snapshot = load_profile_facts_snapshot(user_id, profile_id)
            facts = KnownFacts()
            for key, payload in (snapshot or {}).get("facts", {}).items():
                facts.facts[key] = FactRecord.model_validate(payload)
            self._facts_cache[cache_key] = facts
        return self._facts_cache[cache_key]

    def save_profile_facts(self, user_id: str, profile_id: str, facts: KnownFacts) -> KnownFacts:
        cache_key = (user_id, profile_id)
        self._facts_cache[cache_key] = facts
        write_profile_facts_snapshot(
            user_id,
            profile_id,
            {key: record.model_dump() for key, record in facts.facts.items()},
        )
        return facts

    def _persist_profiles(self, user_id: str) -> None:
        write_profiles_snapshot(user_id, self._profiles_cache[user_id])
