from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


RiskLevel = Literal["none", "low", "medium", "high"]
ModerationMode = Literal["cloud", "local_fallback"]

# This is intentionally a platform-owned safe replacement, not a model retry:
# a blocked final answer must never trigger another model/tool execution.
MODERATION_SAFE_REPLY = "抱歉，这个问题涉及不适合直接展开的内容。我可以继续帮助您换成安全、合规的方式梳理相关情况。"


def moderation_safe_reply(result: "ModerationResult") -> str:
    """Return the provider's approved substitute when available.

    This is deliberately kept at the moderation boundary rather than asking a
    model to rewrite blocked material. Aliyun's advice is already intended as
    a safe substitute; malformed or empty advice falls back to the stable
    platform response.
    """
    raw = result.raw if isinstance(result.raw, dict) else {}
    candidates = [raw]
    windows = raw.get("windows") if isinstance(raw, dict) else None
    if isinstance(windows, list):
        candidates.extend(item for item in windows if isinstance(item, dict))
    for candidate in candidates:
        data = candidate.get("Data", candidate.get("data", {}))
        data = data if isinstance(data, dict) else {}
        advice = data.get("advice", data.get("Advice", [])) or []
        if not isinstance(advice, list):
            continue
        for item in advice:
            if not isinstance(item, dict):
                continue
            answer = item.get("answer", item.get("Answer", ""))
            if str(answer or "").strip():
                return str(answer).strip()
    return MODERATION_SAFE_REPLY


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
        raw = self.raw if isinstance(self.raw, dict) else {}
        raws = [raw]
        if isinstance(raw.get("windows"), list):
            raws.extend(item for item in raw["windows"] if isinstance(item, dict))
        advice = []
        for item in raws:
            data = item.get("Data", item.get("data", {}))
            data = data if isinstance(data, dict) else {}
            values = data.get("advice", data.get("Advice", [])) or []
            if isinstance(values, list):
                advice.extend(value for value in values if isinstance(value, dict))
        categories = sorted({str(item.get("hitLabel", item.get("label", ""))) for item in advice if item.get("hitLabel", item.get("label"))})
        categories = sorted(set(categories).union(self.labels))
        safe_answers = [str(item.get("answer", item.get("Answer", ""))) for item in advice if item.get("answer", item.get("Answer"))]
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
            "categories": categories,
            "advice": safe_answers,
        }


class ModerationBlockedError(RuntimeError):
    def __init__(self, result: ModerationResult, *, stage: str, case_id: str | None = None) -> None:
        self.result = result
        self.stage = stage
        self.case_id = case_id
        super().__init__(f"content blocked at {stage}: {result.risk_level}")
