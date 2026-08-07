from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hailiang_skills.core.telemetry import current_telemetry
from hailiang_skills.core.deployment import deployment_environment, log_root, node_name, release_version


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_HTTP_LOG_PATH = log_root() / f"http_requests.{os.getpid()}.jsonl"


@dataclass(slots=True, frozen=True)
class HttpRequestLogConfig:
    file_path: Path = DEFAULT_HTTP_LOG_PATH


def append_http_request_record(
    *,
    method: str,
    route: str,
    status_code: int,
    duration_ms: float,
    request_id: str = "",
    trace_id: str = "",
    span_id: str = "",
    session_id: str = "",
    profile_id: str = "",
    user_id: str = "",
    run_id: str = "",
    error: str = "",
    file_path: Path = DEFAULT_HTTP_LOG_PATH,
    extra: dict[str, Any] | None = None,
) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    context = current_telemetry()
    record = {
        "timestamp": datetime.now(SHANGHAI_TZ).isoformat(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id or (context.request_id if context else ""),
        "trace_id": trace_id or (context.trace_id if context else ""),
        "span_id": span_id or (context.span_id if context else ""),
        "session_id": session_id or (context.session_id if context else ""),
        "profile_id": profile_id or (context.profile_id if context else ""),
        "user_id": user_id or (context.user_id if context else ""),
        "run_id": run_id or (context.run_id if context else ""),
        "method": method,
        "route": route,
        "status_code": int(status_code),
        "duration_ms": round(float(duration_ms), 3),
        "error": error,
        "environment": deployment_environment(),
        "version": release_version(),
        "node": node_name(),
    }
    if extra:
        record.update(_sanitize_jsonable(extra))
    with file_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _sanitize_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_jsonable(item) for item in value]
    if isinstance(value, set):
        return [_sanitize_jsonable(item) for item in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
