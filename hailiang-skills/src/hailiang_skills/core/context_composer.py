"""Bounded, explainable composition of model working context.

This module deliberately uses a conservative multilingual estimator instead
of pretending that every provider exposes the same tokenizer.  Its result is
used as an admission budget, not as provider billing telemetry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class ContextInputTooLongError(ValueError):
    """The current user input cannot fit without truncation."""


def estimate_tokens(value: Any) -> int:
    """Conservative estimate for Chinese and mixed-language prompt content."""
    if value in (None, "", [], {}):
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return max(1, (len(value) * 3 + 1) // 4)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    total_tokens: int = 160_000
    archive_tokens: int = 24_000
    recent_tokens: int = 64_000
    summary_tokens: int = 16_000
    reference_tokens: int = 12_000

    @classmethod
    def from_total(cls, total_tokens: int) -> "ContextBudget":
        total = max(8_000, int(total_tokens))
        return cls(
            total_tokens=total,
            archive_tokens=max(1_200, min(24_000, total * 15 // 100)),
            recent_tokens=max(3_200, min(64_000, total * 40 // 100)),
            summary_tokens=max(1_200, min(16_000, total * 10 // 100)),
            reference_tokens=max(1_200, min(12_000, total * 8 // 100)),
        )


class ContextComposer:
    """Apply a stable priority order to mutable conversation context."""

    def __init__(self, working_context_tokens: int = 160_000) -> None:
        self.budget = ContextBudget.from_total(working_context_tokens)

    def compose_memory(
        self,
        memory: dict[str, Any],
        *,
        confirmed_facts: dict[str, Any] | None = None,
        archive: list[dict[str, Any]] | None = None,
        current_message: str = "",
        activity_state: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return a prompt-safe memory projection plus public budget metadata."""
        result = dict(memory or {})
        trimmed: list[str] = []
        # Do not silently trim a user message. The caller can surface this
        # deterministic status before a provider gets an oversized request.
        fixed_tokens = estimate_tokens(current_message) + estimate_tokens(activity_state or {}) + estimate_tokens(confirmed_facts or {})
        if fixed_tokens > self.budget.total_tokens:
            status = self._status(result, fixed_tokens, trimmed, rejected_current=True)
            return result, status

        result["profile_candidate_archive"] = self._limit_records(
            list(archive or []), self.budget.archive_tokens, "archive", trimmed
        )
        result["recent_messages"] = self._limit_messages(
            list(result.get("recent_messages") or []), self.budget.recent_tokens, "recent", trimmed
        )
        result["reference_messages"] = self._limit_messages(
            list(result.get("reference_messages") or []), self.budget.reference_tokens, "reference", trimmed
        )
        summary = str(result.get("summary") or "")
        result["summary"] = self._tail_text(summary, self.budget.summary_tokens, "summary", trimmed)

        projected = fixed_tokens + estimate_tokens(result.get("profile_candidate_archive")) + estimate_tokens(result.get("recent_messages")) + estimate_tokens(result.get("reference_messages")) + estimate_tokens(result.get("summary")) + estimate_tokens(result.get("facts"))
        # If the structured memory itself is unusually large, it is lower
        # priority than live state/facts. Keep the newest serializable tail.
        available_for_facts = max(0, self.budget.total_tokens - (projected - estimate_tokens(result.get("facts"))))
        if estimate_tokens(result.get("facts")) > available_for_facts:
            result["facts"] = self._tail_json(result.get("facts"), available_for_facts)
            trimmed.append("memory_facts")
            projected = fixed_tokens + estimate_tokens(result.get("profile_candidate_archive")) + estimate_tokens(result.get("recent_messages")) + estimate_tokens(result.get("reference_messages")) + estimate_tokens(result.get("summary")) + estimate_tokens(result.get("facts"))
        return result, self._status(result, projected, trimmed, rejected_current=False)

    def assert_current_message_fits(self, current_message: str) -> None:
        # Reserve half of the working budget for mandatory instructions,
        # current activity state and a minimally useful response context.
        if estimate_tokens(current_message) > self.budget.total_tokens // 2:
            raise ContextInputTooLongError(
                f"当前输入超过单轮上下文可用预算（{self.budget.total_tokens // 2} tokens）；请拆分后重试。"
            )

    def _status(self, result: dict[str, Any], projected: int, trimmed: list[str], *, rejected_current: bool) -> dict[str, Any]:
        return {
            "working_context_tokens": self.budget.total_tokens,
            "estimated_prompt_tokens": projected,
            "usage_ratio": round(projected / self.budget.total_tokens, 4),
            "trimmed_sections": trimmed,
            "current_message_rejected": rejected_current,
            "section_tokens": {
                "summary": estimate_tokens(result.get("summary")),
                "facts": estimate_tokens(result.get("facts")),
                "archive": estimate_tokens(result.get("profile_candidate_archive")),
                "recent": estimate_tokens(result.get("recent_messages")),
                "reference": estimate_tokens(result.get("reference_messages")),
            },
        }

    @staticmethod
    def _tail_text(value: str, budget: int, label: str, trimmed: list[str]) -> str:
        if estimate_tokens(value) <= budget:
            return value
        # JSON/message framing consumes a little extra budget after content
        # is truncated, so reserve a fixed safety margin.
        chars = max(1, max(1, budget - 128) * 4 // 3)
        trimmed.append(label)
        return value[-chars:]

    def _limit_messages(self, messages: list[Any], budget: int, label: str, trimmed: list[str]) -> list[Any]:
        kept: list[Any] = []
        used = 0
        for item in reversed(messages):
            cost = estimate_tokens(item)
            if kept and used + cost > budget:
                trimmed.append(label)
                break
            if not kept and cost > budget:
                content = dict(item) if isinstance(item, dict) else {"content": str(item)}
                content["content"] = self._tail_text(str(content.get("content") or ""), budget, label, trimmed)
                kept.append(content)
                break
            kept.append(item)
            used += cost
        return list(reversed(kept))

    def _limit_records(self, records: list[dict[str, Any]], budget: int, label: str, trimmed: list[str]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        used = 0
        for record in records:
            cost = estimate_tokens(record)
            if kept and used + cost > budget:
                trimmed.append(label)
                break
            if not kept and cost > budget:
                kept.append(self._tail_json(record, budget))
                trimmed.append(label)
                break
            kept.append(record)
            used += cost
        return kept

    @staticmethod
    def _tail_json(value: Any, budget: int) -> Any:
        raw = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        chars = max(1, max(1, budget - 128) * 4 // 3)
        return {"compacted": True, "tail": raw[-chars:]}
