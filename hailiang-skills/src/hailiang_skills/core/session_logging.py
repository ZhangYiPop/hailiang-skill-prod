from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import yaml

from hailiang_skills.core.telemetry import current_telemetry
from hailiang_skills.core.deployment import log_root
from hailiang_skills.core.deployment import deployment_environment


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SESSION_LOG_ROOT = log_root() / "sessions"
USER_LOG_ROOT = log_root() / "users"
DEFAULT_RUNTIME_CONFIG_PATH = PROJECT_ROOT / "config" / "runtime.yml"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(slots=True, frozen=True)
class SseRecordingConfig:
    enabled: bool = False
    root_dir: Path = SESSION_LOG_ROOT
    format: str = "jsonl"


def get_session_log_dir(session_id: str) -> Path:
    return SESSION_LOG_ROOT / session_id


def ensure_session_log_dir(session_id: str) -> Path:
    log_dir = get_session_log_dir(session_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_session_sse_log_dir(session_id: str) -> Path:
    return load_sse_recording_config().root_dir / session_id / "sse"


def ensure_session_sse_log_dir(session_id: str) -> Path:
    log_dir = get_session_sse_log_dir(session_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_session_run_sse_log_path(session_id: str, run_id: str) -> Path:
    return get_session_sse_log_dir(session_id) / f"{run_id}.jsonl"


def get_session_sse_aggregate_log_path(session_id: str) -> Path:
    return get_session_sse_log_dir(session_id) / "session_stream.jsonl"


def get_user_log_dir(user_id: str) -> Path:
    return USER_LOG_ROOT / user_id


def ensure_user_log_dir(user_id: str) -> Path:
    log_dir = get_user_log_dir(user_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_profile_log_dir(user_id: str, profile_id: str) -> Path:
    return get_user_log_dir(user_id) / "profiles" / profile_id


def ensure_profile_log_dir(user_id: str, profile_id: str) -> Path:
    log_dir = get_profile_log_dir(user_id, profile_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def append_session_events(session_id: str, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    # The production factory installs a PostgreSQL event sink.  JSONL is kept
    # solely for the explicitly selected local file backend.
    from hailiang_skills.storage.event_store import append_events
    if append_events(session_id, events):
        return
    log_dir = ensure_session_log_dir(session_id)
    event_log_path = log_dir / "events.jsonl"
    with event_log_path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


@lru_cache(maxsize=1)
def load_sse_recording_config(path: Path | None = None) -> SseRecordingConfig:
    config_path = path or DEFAULT_RUNTIME_CONFIG_PATH
    data: dict[str, Any] = {}
    if config_path.is_file():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            data = raw
    sse_data = data.get("sse_recording") if isinstance(data.get("sse_recording"), dict) else {}
    root_value = os.getenv("HAILIANG_SSE_RECORDING_ROOT_DIR") or sse_data.get("root_dir") or str(SESSION_LOG_ROOT)
    configured_enabled = os.getenv("HAILIANG_SSE_RECORDING_ENABLED")
    # Production is opt-in even though the checked-in development YAML records
    # SSE by default. A protected prod env file may explicitly enable it.
    enabled = False if configured_enabled is None and deployment_environment() == "prod" else _read_bool(
        configured_enabled, sse_data.get("enabled"), default=deployment_environment() == "test"
    )
    return SseRecordingConfig(
        enabled=enabled,
        root_dir=_resolve_path(root_value),
        format=str(os.getenv("HAILIANG_SSE_RECORDING_FORMAT") or sse_data.get("format") or "jsonl").strip().lower(),
    )


def append_sse_record(session_id: str, run_id: str, record: dict[str, Any], *, user_id: str = "") -> bool:
    config = load_sse_recording_config()
    if not config.enabled or config.format != "jsonl":
        return False
    log_dir = ensure_session_sse_log_dir(session_id)
    telemetry = current_telemetry()
    base_record = {
        "timestamp": datetime.now(SHANGHAI_TZ).isoformat(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "run_id": run_id,
        "user_id": user_id or (telemetry.user_id if telemetry else ""),
        **_sanitize_jsonable(record),
    }
    run_log_path = log_dir / f"{run_id}.jsonl"
    session_log_path = log_dir / "session_stream.jsonl"
    _append_jsonl_record(run_log_path, {**base_record, "stream_scope": "run"})
    _append_jsonl_record(session_log_path, {**base_record, "stream_scope": "session"})
    return True


def _append_jsonl_record(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _sanitize_jsonable(value: Any) -> Any:
    if callable(value):
        return None
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            cleaned = _sanitize_jsonable(item)
            if cleaned is not None:
                sanitized[str(key)] = cleaned
        return sanitized
    if isinstance(value, list):
        return [_sanitize_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_jsonable(item) for item in value]
    if isinstance(value, set):
        return [_sanitize_jsonable(item) for item in sorted(value, key=str)]
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)


def _resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def _read_bool(env_value: str | None, config_value: Any, *, default: bool) -> bool:
    value = env_value if env_value is not None else config_value
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def write_session_snapshot(context) -> None:
    default_cache = "true" if deployment_environment() == "test" else "false"
    if os.getenv("HAILIANG_LOCAL_SESSION_CACHE_ENABLED", default_cache).strip().lower() not in {"1", "true", "yes", "on"}:
        return
    log_dir = ensure_session_log_dir(context.session_id)
    snapshot = {
        "session_id": context.session_id,
        "user_id": context.user_id,
        "profile_id": context.profile_id,
        "profile_name": context.profile_name,
        "title": context.title,
        "message_count": len(context.messages),
        "messages": context.messages,
        "facts": {key: record.model_dump() for key, record in context.known_facts.facts.items()},
        "user_facts": {key: record.model_dump() for key, record in context.user_facts.facts.items()},
        "shared_facts": {key: record.model_dump() for key, record in context.shared_facts.facts.items()},
        "profile_facts": {key: record.model_dump() for key, record in context.profile_facts.facts.items()},
        "session_facts": {
            key: record.model_dump() for key, record in context.session_facts.facts.items()
        },
        "skill_states": context.skill_states,
        "candidate_paths": context.candidate_paths,
        "risk_signals": context.risk_signals,
        "interaction_state": context.interaction_state,
        "event_count": len(context.event_trace),
        "session_meta": _sanitize_jsonable(context.session_meta),
        "last_fact_changes": context.last_fact_changes,
    }
    (log_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_session_snapshot(session_id: str) -> dict[str, Any] | None:
    snapshot_path = get_session_log_dir(session_id) / "snapshot.json"
    if not snapshot_path.exists():
        return None
    return json.loads(snapshot_path.read_text(encoding="utf-8"))


def write_user_facts_snapshot(user_id: str, facts_payload: dict[str, Any]) -> None:
    log_dir = ensure_user_log_dir(user_id)
    snapshot = {
        "user_id": user_id,
        "facts": facts_payload,
    }
    (log_dir / "facts.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_user_facts_snapshot(user_id: str) -> dict[str, Any] | None:
    snapshot_path = get_user_log_dir(user_id) / "facts.json"
    if not snapshot_path.exists():
        return None
    return json.loads(snapshot_path.read_text(encoding="utf-8"))


def write_profile_facts_snapshot(
    user_id: str,
    profile_id: str,
    facts_payload: dict[str, Any],
) -> None:
    log_dir = ensure_profile_log_dir(user_id, profile_id)
    snapshot = {
        "user_id": user_id,
        "profile_id": profile_id,
        "facts": facts_payload,
    }
    (log_dir / "facts.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_profile_facts_snapshot(user_id: str, profile_id: str) -> dict[str, Any] | None:
    snapshot_path = get_profile_log_dir(user_id, profile_id) / "facts.json"
    if not snapshot_path.exists():
        return None
    return json.loads(snapshot_path.read_text(encoding="utf-8"))


def write_profiles_snapshot(user_id: str, profiles: list[dict[str, Any]]) -> None:
    log_dir = ensure_user_log_dir(user_id)
    snapshot = {
        "user_id": user_id,
        "profiles": profiles,
    }
    (log_dir / "profiles.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_profiles_snapshot(user_id: str) -> dict[str, Any] | None:
    snapshot_path = get_user_log_dir(user_id) / "profiles.json"
    if not snapshot_path.exists():
        return None
    return json.loads(snapshot_path.read_text(encoding="utf-8"))
