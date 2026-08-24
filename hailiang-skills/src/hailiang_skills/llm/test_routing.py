from __future__ import annotations

import os
from dataclasses import dataclass


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(value: str, default: int) -> int:
    try:
        return max(int(value or default), 1)
    except (TypeError, ValueError):
        return default


def _float_or_default(value: str, default: float) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class TestLLMRoutingConfig:
    enabled: bool
    user_ids: frozenset[str]
    base_url: str
    api_key: str
    model: str
    timeout_s: int
    temperature: float
    max_tokens: int

    @classmethod
    def from_environment(cls) -> "TestLLMRoutingConfig":
        raw_ids = os.getenv("HAILIANG_TEST_LLM_USER_IDS", "")
        user_ids = frozenset(
            item.strip()
            for item in raw_ids.replace("\n", ",").split(",")
            if item.strip()
        )
        return cls(
            enabled=_truthy(os.getenv("HAILIANG_TEST_LLM_ENABLED", "false")),
            user_ids=user_ids,
            base_url=os.getenv("HAILIANG_TEST_LLM_BASE_URL", "").strip().rstrip("/"),
            api_key=os.getenv("HAILIANG_TEST_LLM_API_KEY", "").strip(),
            model=os.getenv("HAILIANG_TEST_LLM_MODEL", "qwen3.7-plus").strip() or "qwen3.7-plus",
            timeout_s=_positive_int(os.getenv("HAILIANG_TEST_LLM_TIMEOUT_S", "120"), 120),
            temperature=_float_or_default(os.getenv("HAILIANG_TEST_LLM_TEMPERATURE", "0"), 0.0),
            max_tokens=_positive_int(os.getenv("HAILIANG_TEST_LLM_MAX_TOKENS", "8000"), 8000),
        )

    def matches(self, user_id: str, *, environment: str) -> bool:
        """Fail closed in production and when the alternate endpoint is incomplete."""
        return (
            environment == "test"
            and self.enabled
            and bool(self.base_url and self.api_key)
            and str(user_id or "").strip() in self.user_ids
        )
