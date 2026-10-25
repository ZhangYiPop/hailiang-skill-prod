from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


RiskLevel = Literal["none", "low", "medium", "high"]
ModerationMode = Literal["cloud", "local_fallback"]

# This is deliberately shared by every public streaming adapter.  A blocked
# model response must never leave a caller with an empty assistant bubble.
OUTPUT_MODERATION_FALLBACK = "该内容被内容安全策略拦截，请调整后重新输入。"


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
        advice = self._advice_items()
        categories = sorted({str(item.get("hitLabel", item.get("label", ""))) for item in advice if item.get("hitLabel", item.get("label"))})
        categories = sorted(set(categories).union(self.labels))
        safe_answers = self.advice_answers()
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

    def _advice_items(self) -> list[dict[str, Any]]:
        """Return Aliyun advice in response order, including chunked checks."""
        if not isinstance(self.raw, dict):
            return []
        payloads = list(self.raw.get("windows") or [])
        if not payloads:
            payloads = [self.raw]
        items: list[dict[str, Any]] = []
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            data = payload.get("Data", payload.get("data", {})) or {}
            if not isinstance(data, dict):
                continue
            advice = data.get("Advice", data.get("advice", [])) or []
            if isinstance(advice, list):
                items.extend(item for item in advice if isinstance(item, dict))
        return items

    def advice_answers(self) -> list[str]:
        answers: list[str] = []
        for item in self._advice_items():
            value = item.get("Answer", item.get("answer", ""))
            if isinstance(value, str) and value.strip():
                answers.append(value.strip())
        return answers

    def replacement_answer(self) -> str | None:
        """The first safe provider replacement, if the provider supplied one."""
        answers = self.advice_answers()
        return answers[0] if answers else None


class ModerationBlockedError(RuntimeError):
    def __init__(self, result: ModerationResult, *, stage: str, case_id: str | None = None) -> None:
        self.result = result
        self.stage = stage
        self.case_id = case_id
        super().__init__(f"content blocked at {stage}: {result.risk_level}")
