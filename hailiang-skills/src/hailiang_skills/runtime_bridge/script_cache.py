from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScriptCacheEntry:
    value: dict[str, Any]
    expires_at: float
    size: int


class ScriptResultCache:
    """Bounded in-process cache for deterministic, successful script results."""

    def __init__(self, *, enabled: bool = True, ttl_seconds: int = 600, max_entries: int = 1000, max_bytes: int = 32_000_000) -> None:
        self.enabled = bool(enabled)
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024, int(max_bytes))
        self._items: OrderedDict[str, ScriptCacheEntry] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def key_hash(*, skill_id: str, skill_content_hash: str, requirements_hash: str, script_name: str, assets_hash: str, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        seed = "\0".join((skill_id, skill_content_hash, requirements_hash, script_name, assets_hash, canonical))
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            if item.expires_at <= now:
                self._remove(key, item)
                return None
            self._items.move_to_end(key)
            return dict(item.value)

    def put(self, key: str, value: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        size = len(encoded)
        if size > self.max_bytes:
            return False
        entry = ScriptCacheEntry(dict(value), time.monotonic() + self.ttl_seconds, size)
        with self._lock:
            old = self._items.pop(key, None)
            if old:
                self._bytes -= old.size
            while self._items and (len(self._items) >= self.max_entries or self._bytes + size > self.max_bytes):
                _, evicted = self._items.popitem(last=False)
                self._bytes -= evicted.size
            self._items[key] = entry
            self._bytes += size
        return True

    def _remove(self, key: str, item: ScriptCacheEntry) -> None:
        self._items.pop(key, None)
        self._bytes -= item.size

