"""Shared rollout switch for candidate and deployed conversations."""

import os


def context_contract_version() -> int:
    return 3 if os.getenv("HAILIANG_UNIFIED_CONTEXT_ENABLED", "true").lower() not in {"0", "false", "off"} else 2


GENERATION_FAILURE_REPLY = "本轮回复生成失败，请重试"
