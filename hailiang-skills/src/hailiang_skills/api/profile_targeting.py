"""Resolve the child targeted by an SSE action independently from profile data hydration."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException
import yaml


Authority = Literal["input", "context_data", "strict_match"]
_RUNTIME_CONFIG = Path(__file__).resolve().parents[3] / "config" / "runtime.yml"


@dataclass(frozen=True, slots=True)
class ProfileResolution:
    target_profile_id: str
    input_profile_id: str
    context_profile_id: str
    status: Literal["matched", "mismatched"]
    allow_context_seed: bool
    authority: Authority


@lru_cache(maxsize=1)
def configured_profile_authority() -> Authority:
    env_value = os.getenv("HAILIANG_SESSION_PROFILE_AUTHORITY", "").strip().lower()
    configured = ""
    if _RUNTIME_CONFIG.is_file():
        raw = yaml.safe_load(_RUNTIME_CONFIG.read_text(encoding="utf-8")) or {}
        section = raw.get("session_profile_resolution") if isinstance(raw, dict) else None
        if isinstance(section, dict):
            configured = str(section.get("authority") or "").strip().lower()
    value = env_value or configured or "input"
    if value not in {"input", "context_data", "strict_match"}:
        raise RuntimeError("session_profile_resolution.authority must be input, context_data or strict_match")
    return value  # type: ignore[return-value]


class ProfileTargetResolver:
    def __init__(self, authority: Authority | None = None) -> None:
        self.authority = authority or configured_profile_authority()

    def resolve(self, *, input_profile_id: str, context_data: Any) -> ProfileResolution:
        input_id = str(input_profile_id or "").strip()
        context_id = str(getattr(context_data, "profile_id", "") or "").strip()
        if not input_id:
            raise HTTPException(status_code=422, detail="INPUT_PROFILE_ID_REQUIRED")
        matched = input_id == context_id
        if self.authority == "strict_match" and not matched:
            raise HTTPException(status_code=409, detail="PROFILE_CONTEXT_MISMATCH")
        target = context_id if self.authority == "context_data" else input_id
        if not target:
            raise HTTPException(status_code=422, detail="PROFILE_ID_REQUIRED")
        return ProfileResolution(
            target_profile_id=target,
            input_profile_id=input_id,
            context_profile_id=context_id,
            status="matched" if matched else "mismatched",
            # A mismatched input-authority request must never attach one
            # child's forwarded facts to another child's branch.
            allow_context_seed=matched or self.authority == "context_data",
            authority=self.authority,
        )


def basic_profile_context(data: Any, *, profile_id: str, allow_seed: bool) -> dict[str, Any]:
    """Whitelist projection fields; arbitrary ContextData extras never control routing."""
    if not allow_seed:
        return {"profile_id": profile_id}
    return {
        "profile_id": profile_id,
        "student_name": str(getattr(data, "student_name", "") or ""),
        "school_year": getattr(data, "school_year", None),
        "grade": getattr(data, "grade", None),
        "facts": dict(getattr(data, "facts", {}) or {}),
    }
