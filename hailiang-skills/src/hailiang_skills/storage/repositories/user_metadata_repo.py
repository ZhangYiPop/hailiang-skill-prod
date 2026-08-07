from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hailiang_skills.core.session_logging import ensure_user_log_dir, get_user_log_dir


class FileBackedUserMetadataRepository:
    """Small local fallback for debug display names and metadata."""

    def get(self, user_id: str) -> dict[str, Any]:
        path = get_user_log_dir(user_id) / "metadata.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {"user_id": user_id, "display_name": "", "metadata": {}}
        except (OSError, json.JSONDecodeError):
            return {"user_id": user_id, "display_name": "", "metadata": {}}

    def upsert(self, user_id: str, display_name: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        current = self.get(user_id)
        payload = {
            "user_id": user_id,
            "display_name": display_name.strip(),
            "metadata": {**(current.get("metadata") or {}), **(metadata or {})},
        }
        path = ensure_user_log_dir(user_id) / "metadata.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return payload
