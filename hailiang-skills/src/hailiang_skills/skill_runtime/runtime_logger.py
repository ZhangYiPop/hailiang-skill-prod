from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hailiang_skills.core.telemetry import current_telemetry, enrich_payload, text_fingerprint


MAX_LOG_VALUE_CHARS = 4_000
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(slots=True)
class RuntimeLogger:
    file_path: Path
    session_id: str
    user_id: str = ""
    run_id: str = ""

    def log(self, event: str, **payload: Any) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        context = current_telemetry()
        session_id = self.session_id or (context.session_id if context else "")
        user_id = self.user_id or (context.user_id if context else "")
        run_id = self.run_id or (context.run_id if context else "")
        entry = {
            "timestamp": datetime.now(SHANGHAI_TZ).isoformat(),
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "user_id": user_id,
            "run_id": run_id,
            "event": event,
            "payload": _normalize_payload(enrich_payload(payload)),
        }
        with self.file_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def default_log_file(session_path: str | Path) -> Path:
    path = Path(session_path).expanduser().resolve()
    if path.parent.name == "sessions":
        return path.parent.parent / "logs" / f"{path.stem}.jsonl"
    return path.parent / "logs" / f"{path.stem}.jsonl"


def preview_text(value: Any, *, limit: int = 280) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...(truncated)"


def _normalize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            key_name = str(key)
            # Prompt/model/request/response bodies remain in encrypted audit
            # storage only.  Operational JSONL contains low-risk fingerprints.
            if isinstance(item, str) and any(token in key_name.lower() for token in ("prompt", "payload", "body", "content", "message", "response", "input", "text", "line")):
                normalized[key_name] = text_fingerprint(item, preview_chars=0)
            else:
                normalized[key_name] = _normalize_payload(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_payload(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > MAX_LOG_VALUE_CHARS:
            return f"{value[:MAX_LOG_VALUE_CHARS]}...(truncated)"
        return value
    return preview_text(value, limit=MAX_LOG_VALUE_CHARS)
