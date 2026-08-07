from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hailiang_skills.core.session_logging import SESSION_LOG_ROOT


class FileBackedSessionIndexRepository:
    def list_sessions(
        self,
        *,
        user_id: str,
        profile_id: str | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if not SESSION_LOG_ROOT.exists():
            return items
        for session_dir in SESSION_LOG_ROOT.iterdir():
            snapshot_path = session_dir / "snapshot.json"
            if not snapshot_path.exists():
                continue
            payload = self._read_snapshot(snapshot_path)
            if not payload:
                continue
            if payload.get("user_id") != user_id:
                continue
            if profile_id and payload.get("profile_id") != profile_id:
                continue
            items.append(
                {
                    "session_id": payload.get("session_id"),
                    "user_id": payload.get("user_id"),
                    "profile_id": payload.get("profile_id"),
                    "profile_name": payload.get("profile_name"),
                    "title": payload.get("title"),
                    "message_count": payload.get("message_count", 0),
                    "created_at": self._infer_created_at(payload),
                    "updated_at": self._infer_updated_at(snapshot_path, payload),
                    "active_skill": self._infer_active_skill(payload),
                }
            )
        return sorted(
            items,
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )

    def _read_snapshot(self, snapshot_path: Path) -> dict[str, Any]:
        try:
            return json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _infer_created_at(self, payload: dict[str, Any]) -> str | None:
        messages = payload.get("messages", [])
        if messages:
            first_message = messages[0]
            if isinstance(first_message, dict):
                return first_message.get("created_at") or first_message.get("createdAt")
        return None

    def _infer_updated_at(self, snapshot_path: Path, payload: dict[str, Any]) -> str | None:
        messages = payload.get("messages", [])
        if messages:
            last_message = messages[-1]
            if isinstance(last_message, dict):
                return last_message.get("created_at") or last_message.get("createdAt")
        try:
            return str(snapshot_path.stat().st_mtime)
        except OSError:
            return None

    def _infer_active_skill(self, payload: dict[str, Any]) -> str | None:
        skill_states = payload.get("skill_states", {})
        planner_state = skill_states.get("planner", {}) if isinstance(skill_states, dict) else {}
        router_state = skill_states.get("router", {}) if isinstance(skill_states, dict) else {}
        return planner_state.get("target_skill") or router_state.get("target_skill")
