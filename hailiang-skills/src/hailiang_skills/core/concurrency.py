"""Bounded SSE turn admission and cross-worker generation cancellation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from time import monotonic
from uuid import uuid4

from hailiang_skills.core.telemetry import span

try:  # pragma: no cover - optional in local file-backend development
    import redis
except ImportError:  # pragma: no cover
    redis = None


class CapacityExceededError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TurnLease:
    session_id: str
    user_id: str
    generation: str
    redis_reserved: bool = False


class TurnCoordinator:
    """Only the current generation may persist/output a session turn.

    Redis is authoritative in production.  The process-local semaphores keep
    the development backend safe and also limit executor work before it can
    exhaust memory.
    """

    def __init__(self, *, redis_url: str | None = None) -> None:
        self._max_streams = int(os.getenv("HAILIANG_MAX_SSE_CONNECTIONS", "100"))
        self._per_user = int(os.getenv("HAILIANG_MAX_SSE_PER_USER", "3"))
        self._queue_timeout = float(os.getenv("HAILIANG_SSE_QUEUE_TIMEOUT_SECONDS", "5"))
        self._global = BoundedSemaphore(self._max_streams)
        self._user_locks: dict[str, BoundedSemaphore] = {}
        self._guard = Lock()
        self._local_generations: dict[str, str] = {}
        # Keep cancellation separate from generation ownership.  A cancelled
        # generation remains the current one until its worker exits, so it can
        # persist the partial reply and emit a terminal SSE event safely.
        self._local_cancelled: dict[str, str] = {}
        self._redis = None
        url = redis_url or os.getenv("HAILIANG_REDIS_URL", "")
        if url and redis:
            self._redis = redis.Redis.from_url(url, socket_connect_timeout=1.5, socket_timeout=1.5, decode_responses=True)

    @property
    def redis_enabled(self) -> bool:
        return self._redis is not None

    def ready(self) -> bool:
        if not self._redis:
            return False
        return bool(self._redis.ping())

    def acquire(self, session_id: str, user_id: str, *, run_id: str | None = None) -> TurnLease:
        started = monotonic()
        with span("turn.acquire", node="turn_lock"):
            if not self._global.acquire(timeout=self._queue_timeout):
                raise CapacityExceededError("SSE capacity is saturated; retry later")
            with self._guard:
                user_sem = self._user_locks.setdefault(user_id, BoundedSemaphore(self._per_user))
            remaining = max(0, self._queue_timeout - (monotonic() - started))
            if not user_sem.acquire(timeout=remaining):
                self._global.release()
                raise CapacityExceededError("user SSE capacity is saturated; retry later")
            # The BFF owns the externally visible run id.  Local callers keep
            # the generated fallback for internal tests and legacy tools.
            generation = str(run_id or f"gen_{uuid4().hex}")
            with self._guard:
                self._local_generations[session_id] = generation
                if self._local_cancelled.get(session_id) == generation:
                    self._local_cancelled.pop(session_id, None)
            redis_reserved = False
            if self._redis:
                try:
                    admitted = self._redis.eval(
                        """
                        local g = redis.call('INCR', KEYS[1])
                        if g == 1 then redis.call('EXPIRE', KEYS[1], ARGV[3]) end
                        local u = redis.call('INCR', KEYS[2])
                        if u == 1 then redis.call('EXPIRE', KEYS[2], ARGV[3]) end
                        if g > tonumber(ARGV[1]) or u > tonumber(ARGV[2]) then
                          redis.call('DECR', KEYS[1]); redis.call('DECR', KEYS[2]); return 0
                        end
                        return 1
                        """,
                        2,
                        "hailiang:sse:active",
                        f"hailiang:sse:user:{user_id}",
                        self._max_streams,
                        self._per_user,
                        900,
                    )
                    if not admitted:
                        raise CapacityExceededError("global SSE capacity is saturated; retry later")
                    redis_reserved = True
                    # Atomic last-writer-wins generation makes an earlier stream
                    # observe cancellation at every existing boundary.
                    self._redis.set(f"hailiang:turn:{session_id}:generation", generation, ex=900)
                except CapacityExceededError:
                    user_sem.release()
                    self._global.release()
                    raise
                except Exception:
                    user_sem.release()
                    self._global.release()
                    if redis_reserved:
                        self._release_redis_capacity(user_id)
                    raise
            return TurnLease(session_id=session_id, user_id=user_id, generation=generation, redis_reserved=redis_reserved)

    def current_generation(self, session_id: str) -> str | None:
        with self._guard:
            value = self._local_generations.get(session_id)
        return value or None

    def supersede(self, session_id: str, *, next_run_id: str) -> str | None:
        """Mark the current local run as replaced before a new turn starts."""
        with self._guard:
            current = self._local_generations.get(session_id)
            if not current or current == next_run_id:
                return None
            self._local_cancelled[session_id] = current
            # Unlike an explicit stop, a new user action must make the old
            # worker non-current immediately.  It then cannot persist a late
            # answer in the gap before the new lease is acquired.
            self._local_generations[session_id] = next_run_id
            return current

    def is_current(self, lease: TurnLease) -> bool:
        local_current = self._local_generations.get(lease.session_id) == lease.generation
        if not local_current:
            return False
        if not self._redis:
            return True
        try:
            return self._redis.get(f"hailiang:turn:{lease.session_id}:generation") == lease.generation
        except Exception:
            # Fail closed: do not let a partitioned worker overwrite state.
            return False

    def cancel(self, session_id: str, run_id: str) -> bool:
        """Mark the current run as cancelled.

        Returns ``False`` for a stale/non-current run.  The operation is
        intentionally idempotent for the current run so a double click on the
        stop button never turns into an error.
        """
        with self._guard:
            if self._local_generations.get(session_id) != run_id:
                return False
            self._local_cancelled[session_id] = run_id
        if not self._redis:
            return True
        try:
            # Do not create a cancellation marker for a run superseded in
            # another worker between the local check and this Redis call.
            result = self._redis.eval(
                """
                if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
                redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
                return 1
                """,
                2,
                f"hailiang:turn:{session_id}:generation",
                f"hailiang:turn:{session_id}:cancelled",
                run_id,
                900,
            )
            if result:
                return True
            with self._guard:
                if self._local_cancelled.get(session_id) == run_id:
                    self._local_cancelled.pop(session_id, None)
            return False
        except Exception:
            # A local development service should still be stoppable when
            # Redis is briefly unavailable; workers will also observe the
            # local marker.  Production readiness keeps Redis mandatory.
            return True

    def is_cancelled(self, lease: TurnLease) -> bool:
        with self._guard:
            local_cancelled = self._local_cancelled.get(lease.session_id) == lease.generation
        if local_cancelled:
            return True
        if not self._redis:
            return False
        try:
            return self._redis.get(f"hailiang:turn:{lease.session_id}:cancelled") == lease.generation
        except Exception:
            return local_cancelled

    def release(self, lease: TurnLease) -> None:
        with self._guard:
            if self._local_cancelled.get(lease.session_id) == lease.generation:
                self._local_cancelled.pop(lease.session_id, None)
        with self._guard:
            user_sem = self._user_locks.get(lease.user_id)
        if user_sem:
            user_sem.release()
        self._global.release()
        if lease.redis_reserved:
            self._release_redis_capacity(lease.user_id)

    def _release_redis_capacity(self, user_id: str) -> None:
        if not self._redis:
            return
        try:
            self._redis.eval(
                """
                for _, key in ipairs(KEYS) do
                  local value = redis.call('GET', key)
                  if value and tonumber(value) > 0 then redis.call('DECR', key) end
                end
                return 1
                """,
                2,
                "hailiang:sse:active",
                f"hailiang:sse:user:{user_id}",
            )
        except Exception:
            # TTL bounds leaked permits if Redis disappears during teardown.
            return
