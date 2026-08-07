"""Public, stream-safe descriptions for upstream model failures."""

from __future__ import annotations

import re
from typing import Any


_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(cookie\s*[:=]\s*)[^\s,;]+"),
)


def public_model_error(exc: BaseException, *, terminal: bool) -> dict[str, Any]:
    """Build a bounded diagnostic payload that is safe to put on the SSE wire."""
    raw = f"{type(exc).__name__}: {exc}".strip()
    lowered = raw.lower()
    if "timeout" in lowered or "timed out" in lowered:
        code, message, retryable = "MODEL_TIMEOUT", "模型响应超时，请稍后重试。", True
    elif any(token in lowered for token in ("401", "403", "auth", "api key", "unauthorized")):
        code, message, retryable = "MODEL_AUTHENTICATION_FAILED", "模型服务认证失败，请检查服务配置。", False
    elif any(token in lowered for token in ("429", "rate limit", "too many requests", "llmratelimiterror")):
        code, message, retryable = "MODEL_RATE_LIMITED", "模型服务繁忙，请稍后重试。", True
    elif any(token in lowered for token in ("invalid json", "invalid response", "parse", "malformed")):
        code, message, retryable = "MODEL_INVALID_RESPONSE", "模型返回内容无法解析，请重试。", True
    elif any(token in lowered for token in ("stream", "connection reset", "connection aborted", "broken pipe")):
        code, message, retryable = "MODEL_STREAM_INTERRUPTED", "模型流式连接中断，请重试。", True
    elif any(token in lowered for token in ("unavailable", "not configured", "client unavailable", "connection refused")):
        code, message, retryable = "MODEL_UNAVAILABLE", "模型服务暂不可用，请稍后重试。", True
    elif any(token in lowered for token in ("upstream", "502", "503", "504")):
        code, message, retryable = "MODEL_UPSTREAM_ERROR", "模型上游服务异常，请稍后重试。", True
    else:
        code, message, retryable = "MODEL_RUNTIME_ERROR", "模型运行异常，请稍后重试。", True
    detail = raw
    for pattern in _SECRET_PATTERNS:
        detail = pattern.sub(r"\1[REDACTED]", detail)
    return {
        "code": code,
        "message": message,
        "upstream_detail": detail[:2000],
        "retryable": retryable,
        "terminal": terminal,
    }
