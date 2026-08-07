"""Cross-worker admission control for requests made to the model provider."""

from __future__ import annotations

import os
from contextvars import ContextVar
from threading import Lock

from hailiang_skills.core.deployment import deployment_environment

try:  # pragma: no cover - optional for isolated unit tests
    import redis
except ImportError:  # pragma: no cover
    redis = None


class LLMRateLimitError(RuntimeError):
    pass


_reserved_request: ContextVar[str] = ContextVar("hailiang_llm_reserved_request", default="")
_instance: "LLMRateLimiter | None" = None
_instance_signature: tuple[str, str, str, str] | None = None
_instance_lock = Lock()


class LLMRateLimiter:
    """A strict token bucket. Capacity defaults to one to prevent bursts."""

    _TAKE_SCRIPT = """
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
local values = redis.call('HMGET', KEYS[1], 'tokens', 'updated_ms')
local tokens = tonumber(values[1]) or tonumber(ARGV[2])
local updated = tonumber(values[2]) or now_ms
tokens = math.min(tonumber(ARGV[2]), tokens + ((now_ms - updated) / 1000.0) * tonumber(ARGV[1]))
if tokens < 1 then
  redis.call('HMSET', KEYS[1], 'tokens', tokens, 'updated_ms', now_ms)
  redis.call('PEXPIRE', KEYS[1], 60000)
  return 0
end
redis.call('HMSET', KEYS[1], 'tokens', tokens - 1, 'updated_ms', now_ms)
redis.call('PEXPIRE', KEYS[1], 60000)
return 1
"""

    def __init__(self) -> None:
        url = os.getenv("HAILIANG_REDIS_URL", "")
        configured_qps = os.getenv("HAILIANG_LLM_RATE_LIMIT_QPS")
        # Unit tests and one-off local tooling do not source a deployment file.
        # Real test/prod files always set an explicit non-zero limit.
        self.qps = float(configured_qps if configured_qps is not None else ("0" if not url and deployment_environment() == "test" else "45"))
        self.capacity = float(os.getenv("HAILIANG_LLM_RATE_LIMIT_CAPACITY", "1"))
        prefix = os.getenv("HAILIANG_REDIS_KEY_PREFIX", f"hailiang:{deployment_environment()}:").strip()
        self.key = f"{prefix}llm:token_bucket"
        self.reservation_prefix = f"{prefix}llm:reservation:"
        self._redis = None
        if redis and url:
            self._redis = redis.Redis.from_url(url, socket_connect_timeout=1.5, socket_timeout=1.5, decode_responses=True)

    def ready(self) -> bool:
        if self.qps <= 0:
            return True
        try:
            return bool(self._redis and self._redis.ping())
        except Exception:
            return False

    def reserve_for_request(self, request_id: str) -> None:
        if self.qps <= 0:
            return
        self._take()
        if not self._redis:
            raise LLMRateLimitError("model rate limiter Redis is unavailable")
        self._redis.set(f"{self.reservation_prefix}{request_id}", "1", ex=30)
        _reserved_request.set(request_id)

    def acquire(self) -> None:
        request_id = _reserved_request.get()
        if not request_id:
            try:
                from hailiang_skills.core.telemetry import current_telemetry
                context = current_telemetry()
                request_id = context.request_id if context else ""
            except Exception:
                request_id = ""
        if request_id and self._consume_reservation(request_id):
            _reserved_request.set("")
            return
        self._take()

    def _consume_reservation(self, request_id: str) -> bool:
        if not self._redis:
            return False
        try:
            return bool(self._redis.eval("local value=redis.call('GET', KEYS[1]); if value then redis.call('DEL', KEYS[1]); return 1 end; return 0", 1, f"{self.reservation_prefix}{request_id}"))
        except Exception:
            return False

    def _take(self) -> None:
        if self.qps <= 0:
            return
        if not self._redis:
            raise LLMRateLimitError("model rate limiter Redis is unavailable")
        try:
            admitted = self._redis.eval(self._TAKE_SCRIPT, 1, self.key, self.qps, max(self.capacity, 1.0))
        except Exception as exc:
            raise LLMRateLimitError("model rate limiter Redis is unavailable") from exc
        if not admitted:
            raise LLMRateLimitError("model rate limit exceeded")


def get_llm_rate_limiter() -> LLMRateLimiter:
    global _instance, _instance_signature
    signature = (
        os.getenv("HAILIANG_REDIS_URL", ""),
        os.getenv("HAILIANG_REDIS_KEY_PREFIX", ""),
        os.getenv("HAILIANG_LLM_RATE_LIMIT_QPS", ""),
        os.getenv("HAILIANG_DEPLOY_ENV", "test"),
    )
    with _instance_lock:
        if _instance is None or _instance_signature != signature:
            _instance = LLMRateLimiter()
            _instance_signature = signature
        return _instance
