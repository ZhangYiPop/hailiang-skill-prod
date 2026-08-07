from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from hailiang_skills.core.session_logging import SESSION_LOG_ROOT

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "session_opening.yml"

_session_opening_config: dict[str, Any] | None = None


def load_session_opening_config(force_reload: bool = False) -> dict[str, Any]:
    global _session_opening_config
    if _session_opening_config is None or force_reload:
        if _CONFIG_PATH.exists():
            with _CONFIG_PATH.open(encoding="utf-8") as fh:
                _session_opening_config = yaml.safe_load(fh) or {}
        else:
            _session_opening_config = {}
    return _session_opening_config


def get_session_opening_meta() -> dict[str, Any]:
    return load_session_opening_config().get("session_opening", {})


def build_session_opening_message(
    profile_name: str | None = None,
    *,
    parent_name: str | None = None,
) -> str:
    config = get_session_opening_meta()
    if not config.get("enabled", True):
        return ""
    template = str(config.get("default_message") or "").strip()
    if not template:
        return ""
    normalized_profile_name = (profile_name or "").strip()
    normalized_parent_name = (parent_name or "").strip()
    profile_name_suffix = f"（{normalized_profile_name}）" if normalized_profile_name else ""
    parent_name_prefix = f"{normalized_parent_name} " if normalized_parent_name else ""
    return template.format(
        parent_name=normalized_parent_name,
        parent_name_prefix=parent_name_prefix,
        profile_name=normalized_profile_name,
        profile_name_prefix=parent_name_prefix,
        profile_name_suffix=profile_name_suffix,
    ).strip()


def build_historical_session_opening_message(
    *,
    user_id: str,
    profile_id: str | None,
    profile_name: str | None,
    parent_name: str | None = None,
    recent_session_payloads: list[dict[str, Any]] | None = None,
) -> str:
    # A profile can belong either to a student or to a parent.  Do not use a
    # name-based greeting or borrow another child's conversation context.
    del profile_name, parent_name
    # PostgreSQL production storage supplies two payloads directly.  The
    # legacy file scan remains a development fallback only.
    recent_sessions = (
        recent_session_payloads
        if recent_session_payloads is not None
        else _find_recent_session_snapshots(user_id=user_id, profile_id=profile_id)
    )
    if not recent_sessions:
        return ""

    # Prefer the most recent completed session.  A just-created or still
    # streaming session has no generated conclusion summary yet, so use the
    # immediately preceding session's summary rather than a title/raw message.
    summary = _session_summary(recent_sessions[0])
    if not summary and len(recent_sessions) > 1:
        summary = _session_summary(recent_sessions[1])
    if not summary:
        return ""
    return f"你好，我们上次聊到了「{summary}」。这次想聊聊什么？"


def _find_recent_session_snapshots(*, user_id: str, profile_id: str | None) -> list[dict[str, Any]]:
    if not SESSION_LOG_ROOT.exists():
        return []
    # Development fallback: inspect newest snapshots first and stop after the
    # two usable histories required by the greeting.  The old implementation
    # YAML-parsed every file in ``logs/sessions`` on every new conversation.
    snapshot_paths = [path / "snapshot.json" for path in SESSION_LOG_ROOT.iterdir()]
    snapshot_paths = [path for path in snapshot_paths if path.is_file()]
    snapshot_paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    candidates: list[tuple[str, dict[str, Any]]] = []
    for snapshot_path in snapshot_paths:
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("user_id") or "") != user_id:
            continue
        if profile_id and str(payload.get("profile_id") or "") != profile_id:
            continue
        if not _has_user_message(payload):
            continue
        candidates.append((_updated_at(snapshot_path, payload), payload))
        if len(candidates) >= 2:
            break
    if not candidates:
        return None
    return [payload for _, payload in sorted(candidates, key=lambda item: item[0], reverse=True)]


def _has_user_message(payload: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict) and item.get("role") == "user" and str(item.get("content") or "").strip()
        for item in payload.get("messages", [])
    )


def _session_summary(payload: dict[str, Any]) -> str:
    for item in reversed(payload.get("messages", [])):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        compression = item.get("context_compression")
        if not isinstance(compression, dict):
            compression = metadata.get("context_compression") if isinstance(metadata, dict) else {}
        candidates = (
            item.get("conclusion_summary"),
            metadata.get("conclusion_summary"),
            compression.get("conversation_summary") if isinstance(compression, dict) else "",
        )
        for value in candidates:
            summary = " ".join(str(value or "").split()).strip()
            if summary:
                return summary[:220]
    return ""


def _updated_at(snapshot_path: Path, payload: dict[str, Any]) -> str:
    messages = payload.get("messages", [])
    if messages and isinstance(messages[-1], dict):
        created_at = str(messages[-1].get("created_at") or messages[-1].get("createdAt") or "")
        if created_at:
            return created_at
    try:
        return str(snapshot_path.stat().st_mtime)
    except OSError:
        return ""
