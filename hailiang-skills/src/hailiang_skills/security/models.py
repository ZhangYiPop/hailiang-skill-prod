from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


RiskLevel = Literal["none", "low", "medium", "high"]
ModerationMode = Literal["cloud", "local_fallback"]


@dataclass(slots=True)
class ModerationResult:
    matched: bool
    risk_level: RiskLevel
    labels: list[str] = field(default_factory=list)
    provider: str = "local"
    mode: ModerationMode = "local_fallback"
    failure_reason: str | None = None
    request_id: str | None = None
    lexicon_version: str | None = None
    source_files: list[str] = field(default_factory=list)
    matched_text_hashes: list[str] = field(default_factory=list)
    match_positions: list[dict[str, int]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def blocked(self) -> bool:
        return self.matched or self.risk_level in {"low", "medium", "high"}

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "risk_level": self.risk_level,
            "labels": self.labels,
            "provider": self.provider,
            "moderation_mode": self.mode,
            "failure_reason": self.failure_reason,
            "request_id": self.request_id,
            "lexicon_version": self.lexicon_version,
            "source_files": self.source_files,
            "matched_text_hashes": self.matched_text_hashes,
            "match_positions": self.match_positions,
        }


class ModerationBlockedError(RuntimeError):
    def __init__(self, result: ModerationResult, *, stage: str, case_id: str | None = None) -> None:
        self.result = result
        self.stage = stage
        self.case_id = case_id
        super().__init__(f"content blocked at {stage}: {result.risk_level}")

