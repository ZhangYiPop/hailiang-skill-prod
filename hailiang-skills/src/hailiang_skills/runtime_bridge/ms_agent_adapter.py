from __future__ import annotations

import inspect
import hashlib
import ast
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_skill_runtime_core import (
    AgentSkillRuntimeCore,
    CoreTraceStep,
    MSAgentRuntimeProbe,
    PersistentSandboxWorker,
    parse_script_json_output,
    probe_ms_agent_runtime,
)

from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID
from hailiang_skills.runtime_bridge.script_review import review_scripts
from hailiang_skills.runtime_bridge.script_cache import ScriptResultCache


@dataclass(frozen=True)
class SandboxPrepareResult:
    status: str
    state: str
    detail: str
    payload: dict[str, Any]
    blocks_execution: bool = False


@dataclass
class SandboxPrepareRecord:
    key: str
    skill_id: str
    skill_dir: Path
    requirements_hash: str
    future: Future[SandboxPrepareResult]
    scheduled_at: float


@dataclass
class SandboxWorker:
    key: str
    skill_id: str
    skill_dir: Path
    content_hash: str
    requirements_hash: str
    container: Any
    persistent_worker: PersistentSandboxWorker | None
    lock: threading.Lock
    created_at: float
    last_used_at: float
    capacity_reserved: bool = False


class MSAgentRuntimeAdapter:
    _LOCAL_FAST_SCRIPT_NAMES = frozenset({"profile_op.py", "output_sanitizer.py"})
    _MAX_SANDBOX_WORKERS = 64

    def __init__(
        self,
        *,
        runtime_dir: Path,
        runtime_probe: MSAgentRuntimeProbe | None = None,
        sandbox_prewarm_enabled: bool = True,
        local_fast_path_enabled: bool = True,
        max_prepare_workers: int = 2,
        sandbox_worker_reuse_enabled: bool = True,
        script_result_cache_enabled: bool = True,
        script_result_cache_ttl_seconds: int = 600,
        script_result_cache_max_entries: int = 1000,
        sandbox_worker_pool_size_per_key: int = 1,
        sandbox_worker_pool_max_total: int = 6,
        sandbox_worker_acquire_timeout_seconds: float = 5.0,
        sandbox_worker_idle_ttl_seconds: int = 120,
        sandbox_worker_max_lifetime_seconds: int = 900,
        event_recorder: Any | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.runtime_probe = runtime_probe or probe_ms_agent_runtime()
        self.sandbox_prewarm_enabled = sandbox_prewarm_enabled
        self.sandbox_worker_reuse_enabled = sandbox_worker_reuse_enabled
        self.sandbox_worker_pool_size_per_key = max(1, int(sandbox_worker_pool_size_per_key))
        self.sandbox_worker_pool_max_total = max(1, int(sandbox_worker_pool_max_total))
        self.sandbox_worker_acquire_timeout_seconds = max(0.1, float(sandbox_worker_acquire_timeout_seconds))
        self.sandbox_worker_idle_ttl_seconds = max(1, int(sandbox_worker_idle_ttl_seconds))
        self.sandbox_worker_max_lifetime_seconds = max(1, int(sandbox_worker_max_lifetime_seconds))
        self.event_recorder = event_recorder
        self.local_fast_path_enabled = local_fast_path_enabled
        self._sandbox_prepare_executor = ThreadPoolExecutor(
            max_workers=max_prepare_workers,
            thread_name_prefix="hailiang-sandbox-prepare",
        )
        self._sandbox_prepare_lock = threading.Lock()
        self._sandbox_prepare_records: dict[str, SandboxPrepareRecord] = {}
        self._sandbox_workers: dict[str, list[SandboxWorker]] = {}
        self._sandbox_worker_lock = threading.Lock()
        self._sandbox_worker_condition = threading.Condition(self._sandbox_worker_lock)
        self._sandbox_capacity = threading.BoundedSemaphore(self.sandbox_worker_pool_max_total)
        atexit.register(self.close)
        self.script_result_cache = ScriptResultCache(
            enabled=script_result_cache_enabled,
            ttl_seconds=script_result_cache_ttl_seconds,
            max_entries=script_result_cache_max_entries,
        )

    @property
    def available(self) -> bool:
        return self.runtime_probe.available

    def close(self) -> None:
        """Close all pooled Docker contexts during process shutdown/reload."""
        with self._sandbox_worker_condition:
            workers = [worker for items in self._sandbox_workers.values() for worker in items]
            self._sandbox_workers.clear()
            self._sandbox_worker_condition.notify_all()
        for worker in workers:
            self._close_worker(worker)
        try:
            self._sandbox_prepare_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    @property
    def status(self) -> str:
        return self.runtime_probe.status

    @property
    def error(self) -> str | None:
        return self.runtime_probe.error

    def health_step(self) -> CoreTraceStep:
        return self._normalize_step(self._runtime_core().health_step())

    def check_load(self, skill_dir: Path) -> CoreTraceStep:
        with self._ms_agent_skill_directory(skill_dir) as compatible_dir:
            return self._normalize_step(self._runtime_core().check_load(compatible_dir))

    def run_single_skill_turn(self, **kwargs: Any):
        skill_dir = kwargs.get("skill_dir")
        if skill_dir is None:
            return self._runtime_core().run_single_skill_turn(**kwargs)
        with self._ms_agent_skill_directory(Path(skill_dir)) as compatible_dir:
            kwargs["skill_dir"] = compatible_dir
            return self._runtime_core().run_single_skill_turn(**kwargs)

    @contextmanager
    def _ms_agent_skill_directory(self, skill_dir: Path):
        """Give the legacy MS-Agent loader an uppercase entrypoint without changing the package."""
        skill_dir = Path(skill_dir)
        entries = {item.name: item for item in skill_dir.iterdir()} if skill_dir.is_dir() else {}
        lowercase_entrypoint = entries.get("skill.md")
        if entries.get("SKILL.md", Path()).is_file() or not lowercase_entrypoint or not lowercase_entrypoint.is_file():
            yield skill_dir
            return

        with tempfile.TemporaryDirectory(prefix="hailiang-ms-agent-skill-") as temporary_root:
            compatible_dir = Path(temporary_root) / skill_dir.name
            shutil.copytree(skill_dir, compatible_dir, symlinks=True)
            # macOS may treat skill.md and SKILL.md as the same path, so rename
            # the copied lowercase entrypoint before creating the compatibility name.
            (compatible_dir / "skill.md").unlink()
            shutil.copy2(lowercase_entrypoint, compatible_dir / "SKILL.md")
            yield compatible_dir

    def execute_scripts_in_sandbox(
        self,
        *,
        skill_id: str,
        skill_dir: Path,
        loaded_scripts: list[dict[str, Any]],
        execute_scripts: bool,
        script_inputs: dict[str, dict[str, Any]] | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        revision_id: str | None = None,
        event_recorder: Any | None = None,
    ) -> tuple[list[dict[str, Any]], list[CoreTraceStep]]:
        local_outputs = self._execute_local_fast_path(
            skill_id=skill_id,
            skill_dir=skill_dir,
            loaded_scripts=loaded_scripts,
            execute_scripts=execute_scripts,
            script_inputs=script_inputs,
        )
        if local_outputs is not None:
            return local_outputs

        prepare_steps: list[CoreTraceStep] = []
        if execute_scripts and loaded_scripts and not self._script_review_blocks_execution(skill_dir):
            prepare_result, prepare_steps = self.await_sandbox_prepare(skill_id, skill_dir)
            if prepare_result.blocks_execution:
                return [], [
                    *prepare_steps,
                    CoreTraceStep(
                        name="script_execution",
                        status="warning",
                        detail=_script_skip_detail(prepare_result.detail),
                        payload=prepare_result.payload,
                    ),
                ]

        content_hash = self._skill_content_hash(skill_dir)
        assets_hash = self._directory_hash(Path(skill_dir) / "assets")
        requirements_hash = self._requirements_hash(skill_dir)
        cache_keys: list[tuple[dict[str, Any], str]] = []
        cacheable = self._scripts_cacheable(skill_dir, loaded_scripts)
        cache_lookup_duration_ms = 0
        if cacheable and self.script_result_cache.enabled:
            lookup_started = time.perf_counter()
            for script in loaded_scripts:
                script_path = Path(str(script.get("abs_path") or script.get("path") or script.get("name")))
                if not script_path.is_absolute():
                    script_path = skill_dir / script_path
                payload = self._local_script_input(script, script_path, script_inputs)
                key = self.script_result_cache.key_hash(
                    skill_id=skill_id,
                    skill_content_hash=content_hash,
                    requirements_hash=requirements_hash,
                    script_name=script_path.name,
                    assets_hash=assets_hash,
                    payload=payload,
                )
                cache_keys.append((payload, key))
            cached_outputs = []
            for script, (payload, key) in zip(loaded_scripts, cache_keys):
                cached = self.script_result_cache.get(key)
                if cached is None:
                    cached_outputs = []
                    break
                cached_outputs.append({
                    **cached,
                    "script": str(script.get("name") or script.get("path") or ""),
                    "stdin_payload": payload,
                    "duration_ms": 0,
                    "execution_mode": "result_cache",
                    "cache_hit": True,
                    "cache_key_hash": key,
                })
            cache_lookup_duration_ms = _elapsed_ms(lookup_started)
            if cached_outputs:
                return cached_outputs, [
                    *prepare_steps,
                    CoreTraceStep(
                        name="script_execution",
                        status="success",
                        detail="deterministic script result cache hit",
                        payload={
                            "outputs": cached_outputs,
                            "execution_mode": "result_cache",
                            "cache_hit": True,
                            "cache_lookup_duration_ms": _elapsed_ms(lookup_started),
                        },
                    ),
                ]

        core = self._runtime_core()
        kwargs: dict[str, Any] = {
            "skill_id": skill_id,
            "skill_dir": skill_dir,
            "loaded_scripts": loaded_scripts,
            "execute_scripts": execute_scripts,
        }
        try:
            if "script_inputs" in inspect.signature(core.execute_scripts_in_sandbox).parameters:
                kwargs["script_inputs"] = script_inputs
        except (TypeError, ValueError):
            kwargs["script_inputs"] = script_inputs

        recorder = event_recorder or self.event_recorder
        worker = None
        worker_lease_acquired = False
        cold_slot_acquired = False
        worker_acquire_ms = 0
        supports_worker = False
        try:
            supports_worker = "sandbox_container" in inspect.signature(core.execute_scripts_in_sandbox).parameters
        except (TypeError, ValueError):
            supports_worker = False
        if supports_worker and self.sandbox_worker_reuse_enabled and execute_scripts and loaded_scripts:
            worker, worker_acquire_ms = self._acquire_worker(
                core=core,
                skill_id=skill_id,
                skill_dir=skill_dir,
                content_hash=content_hash,
                requirements_hash=requirements_hash,
                session_id=session_id,
                turn_id=turn_id,
                revision_id=revision_id,
                event_recorder=recorder,
            )
            worker_lease_acquired = worker is not None
        elif execute_scripts and loaded_scripts and recorder:
            self._emit_pool_event(
                "sandbox_cold_fallback", skill_id=skill_id, session_id=session_id,
                turn_id=turn_id, revision_id=revision_id,
                error_code="worker_reuse_disabled", error=RuntimeError("worker reuse disabled"),
                recorder=recorder,
            )
        if worker is None and execute_scripts and loaded_scripts:
            cold_slot_acquired = self._sandbox_capacity.acquire(
                timeout=min(self.sandbox_worker_acquire_timeout_seconds, 0.5)
            )
            if cold_slot_acquired:
                self._emit_pool_event(
                    "sandbox_cold_fallback", skill_id=skill_id, session_id=session_id,
                    turn_id=turn_id, revision_id=revision_id,
                    error_code="worker_pool_timeout", recorder=recorder,
                )
            if not cold_slot_acquired:
                self._emit_pool_event(
                    "sandbox_cold_fallback", skill_id=skill_id, session_id=session_id,
                    turn_id=turn_id, revision_id=revision_id,
                    error_code="global_capacity_exhausted",
                    error=TimeoutError("sandbox global capacity exhausted"),
                    recorder=recorder,
                )
                return [], [
                    *prepare_steps,
                    CoreTraceStep(
                        name="script_execution",
                        status="warning",
                        detail="script execution deferred because sandbox capacity is exhausted",
                        payload={
                            "execution_mode": "sandbox_cold_fallback",
                            "worker_reused": False,
                            "error_code": "global_capacity_exhausted",
                            "retriable": True,
                        },
                    ),
                ]
        if worker is not None:
            kwargs["sandbox_container"] = worker.container
            kwargs["sandbox_worker"] = worker.persistent_worker
            kwargs["worker_reused"] = True
            self._emit_worker_event(
                "sandbox_worker_execution_started", worker,
                session_id=session_id, turn_id=turn_id, revision_id=revision_id,
                recorder=recorder,
            )
        worker_exception: Exception | None = None
        try:
            outputs, core_steps = core.execute_scripts_in_sandbox(**kwargs)
        except Exception as exc:
            worker_exception = exc
            if worker is not None:
                self._emit_worker_event(
                    "sandbox_worker_execution_failed", worker,
                    session_id=session_id, turn_id=turn_id, revision_id=revision_id,
                    error_code="worker_execution_exception", recorder=recorder,
                )
                kwargs.pop("sandbox_worker", None)
                kwargs.pop("sandbox_container", None)
                kwargs["worker_reused"] = False
                outputs, core_steps = core.execute_scripts_in_sandbox(**kwargs)
            else:
                raise
        finally:
            if worker is not None and worker_lease_acquired:
                worker.last_used_at = time.time()
                self._release_worker(worker)
            if cold_slot_acquired:
                self._sandbox_capacity.release()
        if worker is not None and (
            worker_exception is not None
            or not outputs
            or any(not _script_output_ok(item) for item in outputs if isinstance(item, dict))
        ):
            self._emit_worker_event(
                "sandbox_worker_execution_failed", worker,
                session_id=session_id, turn_id=turn_id, revision_id=revision_id,
                error_code="script_execution_failed", recorder=recorder,
            )
            self._discard_worker(
                worker.key, worker, session_id=session_id, turn_id=turn_id,
                revision_id=revision_id, recorder=recorder,
            )
        elif worker is not None:
            self._emit_worker_event(
                "sandbox_worker_execution_completed", worker,
                session_id=session_id, turn_id=turn_id, revision_id=revision_id,
                recorder=recorder,
            )
        if cacheable:
            for output, (_, key) in zip(outputs, cache_keys):
                output.setdefault("cache_hit", False)
                output.setdefault("cache_key_hash", key)
        if cacheable and outputs and all(_script_output_ok(item) for item in outputs if isinstance(item, dict)):
            for output, (_, key) in zip(outputs, cache_keys):
                structured = output.get("return_value") or output.get("json_output")
                if isinstance(structured, dict):
                    self.script_result_cache.put(key, {
                        "args": output.get("args") if isinstance(output.get("args"), list) else [],
                        "return_value": structured,
                        "json_output": structured,
                        "exit_code": output.get("exit_code"),
                        "stdout": output.get("stdout", ""),
                        "stderr": output.get("stderr", ""),
                        "ok": True,
                    })
        normalized_steps = [self._normalize_step(step) for step in core_steps]
        for step in normalized_steps:
            if step.name == "script_execution" and isinstance(step.payload, dict):
                step.payload.setdefault("cache_hit", False)
                step.payload.setdefault("cache_lookup_duration_ms", cache_lookup_duration_ms)
                step.payload.setdefault("worker_acquire_ms", worker_acquire_ms)
                step.payload.setdefault("worker_reused", worker is not None)
        return outputs, [*prepare_steps, *normalized_steps]

    def _get_or_create_worker(self, **kwargs: Any) -> SandboxWorker | None:
        """Compatibility hook used by background prewarming."""
        worker, _wait_ms = self._acquire_worker(**kwargs)
        if worker is not None:
            self._release_worker(worker)
        return worker

    def _execute_local_fast_path(
        self,
        *,
        skill_id: str,
        skill_dir: Path,
        loaded_scripts: list[dict[str, Any]],
        execute_scripts: bool,
        script_inputs: dict[str, dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[CoreTraceStep]] | None:
        """Run audited, side-effect-free main-planner helpers without Docker."""
        if not execute_scripts or not self.local_fast_path_enabled:
            return None
        if not self._local_fast_path_allowed(skill_id, skill_dir, loaded_scripts):
            return None

        outputs: list[dict[str, Any]] = []
        for script in loaded_scripts:
            script_path = Path(str(script.get("abs_path") or script.get("path") or script.get("name")))
            if not script_path.is_absolute():
                script_path = skill_dir / script_path
            payload = self._local_script_input(script, script_path, script_inputs)
            args = _script_args_from_payload(payload)
            started = time.perf_counter()
            try:
                completed = subprocess.run(
                    [sys.executable, str(script_path), *args],
                    input=json.dumps(payload, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                script_compute_ms = _elapsed_ms(started)
                parse_started = time.perf_counter()
                structured_output = parse_script_json_output(completed.stdout)
                stdout_parse_ms = _elapsed_ms(parse_started)
                output_data = {
                    "script": script_path.name,
                    "args": args,
                    "stdin_payload": payload,
                    "duration_ms": script_compute_ms,
                    "script_compute_ms": script_compute_ms,
                    "stdout_parse_ms": stdout_parse_ms,
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                    "ok": completed.returncode == 0,
                    "execution_mode": "local_fast_path",
                }
                if structured_output is not None:
                    output_data["return_value"] = structured_output
                    output_data["json_output"] = structured_output
                outputs.append(output_data)
            except (OSError, subprocess.SubprocessError) as exc:
                outputs.append(
                    {
                        "script": script_path.name,
                        "args": args,
                        "stdin_payload": payload,
                        "duration_ms": _elapsed_ms(started),
                        "exit_code": None,
                        "stdout": "",
                        "stderr": str(exc),
                        "ok": False,
                        "execution_mode": "local_fast_path",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        success = all(bool(item.get("ok")) for item in outputs)
        return outputs, [
            CoreTraceStep(
                name="script_execution",
                status="success" if success else "warning",
                detail="allowlisted scripts executed with local fast path",
                payload={
                    "execution_mode": "local_fast_path",
                    "scripts": [item.get("script") for item in outputs],
                    "outputs": outputs,
                },
            )
        ]

    def _acquire_worker(
        self,
        *,
        core: AgentSkillRuntimeCore,
        skill_id: str,
        skill_dir: Path,
        content_hash: str,
        requirements_hash: str,
        session_id: str | None = None,
        turn_id: str | None = None,
        revision_id: str | None = None,
        event_recorder: Any | None = None,
    ) -> tuple[SandboxWorker | None, float]:
        runtime_image = os.getenv("HAILIANG_SANDBOX_IMAGE", "python:3.11-slim")
        policy = os.getenv("HAILIANG_SANDBOX_POLICY", "default")
        seed = "\0".join((str(skill_id), str(revision_id or content_hash), requirements_hash, runtime_image, policy))
        key = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
        started = time.perf_counter()
        deadline = time.monotonic() + self.sandbox_worker_acquire_timeout_seconds
        queued = False
        while True:
            with self._sandbox_worker_condition:
                workers = self._sandbox_workers.setdefault(key, [])
                now = time.time()
                stale = [item for item in workers if (
                    now - item.last_used_at > self.sandbox_worker_idle_ttl_seconds
                    or now - item.created_at > self.sandbox_worker_max_lifetime_seconds
                ) and not item.lock.locked()]
                for item in stale:
                    workers.remove(item)
                    self._close_worker(item)
                    self._emit_worker_event("sandbox_worker_destroyed", item, session_id=session_id, turn_id=turn_id,
                                            revision_id=revision_id, error_code="worker_expired", recorder=event_recorder)
                for item in workers:
                    if item.lock.acquire(blocking=False):
                        self._emit_worker_event(
                            "sandbox_worker_reused", item, session_id=session_id, turn_id=turn_id,
                            revision_id=revision_id, queue_depth=0, acquire_wait_ms=_elapsed_ms(started),
                            recorder=event_recorder,
                        )
                        return item, _elapsed_ms(started)
                total = sum(len(items) for items in self._sandbox_workers.values())
                max_total = min(self.sandbox_worker_pool_max_total, self._MAX_SANDBOX_WORKERS)
                if total >= max_total:
                    idle_candidates = [
                        item
                        for items in self._sandbox_workers.values()
                        for item in items
                        if not item.lock.locked()
                    ]
                    if idle_candidates:
                        evicted = min(idle_candidates, key=lambda item: item.last_used_at)
                        self._sandbox_workers.get(evicted.key, []).remove(evicted)
                        self._close_worker(evicted)
                        total -= 1
                        self._emit_worker_event(
                            "sandbox_worker_destroyed", evicted,
                            session_id=session_id, turn_id=turn_id, revision_id=revision_id,
                            error_code="global_pool_eviction", recorder=event_recorder,
                        )
                if len(workers) < self.sandbox_worker_pool_size_per_key and total < max_total:
                    if not self._sandbox_capacity.acquire(blocking=False):
                        queue_depth = sum(
                            1 for items in self._sandbox_workers.values()
                            for item in items if item.lock.locked()
                        )
                        if not queued:
                            queued = True
                            self._emit_pool_event(
                                "sandbox_worker_pool_saturated", skill_id=skill_id,
                                session_id=session_id, turn_id=turn_id, revision_id=revision_id,
                                queue_depth=queue_depth, error_code="global_capacity_exhausted",
                                recorder=event_recorder,
                            )
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._emit_pool_event(
                                "sandbox_worker_acquire_timeout", skill_id=skill_id,
                                session_id=session_id, turn_id=turn_id, revision_id=revision_id,
                                queue_depth=queue_depth, error_code="global_capacity_exhausted",
                                recorder=event_recorder,
                            )
                            return None, _elapsed_ms(started)
                        self._sandbox_worker_condition.wait(timeout=min(remaining, 0.1))
                        continue
                    try:
                        workspace = self.runtime_dir / "workspace_workers" / key / uuid4().hex
                        container = core.create_sandbox_container(
                            skill_id=skill_id,
                            skill_dir=skill_dir,
                            workspace_dir=workspace,
                        )
                        persistent = None
                        if _supports_persistent_sandbox(container):
                            persistent = PersistentSandboxWorker(container)
                            persistent.start(timeout=30)
                        worker = SandboxWorker(
                            key=key, skill_id=skill_id, skill_dir=Path(skill_dir),
                            content_hash=content_hash, requirements_hash=requirements_hash,
                            container=container, persistent_worker=persistent,
                            lock=threading.Lock(), created_at=now, last_used_at=now,
                            capacity_reserved=True,
                        )
                        worker.lock.acquire()
                        workers.append(worker)
                        self._emit_worker_event("sandbox_worker_created", worker, session_id=session_id,
                                                turn_id=turn_id, revision_id=revision_id,
                                                acquire_wait_ms=_elapsed_ms(started), recorder=event_recorder)
                        return worker, _elapsed_ms(started)
                    except Exception as exc:
                        self._sandbox_capacity.release()
                        self._emit_pool_event("sandbox_cold_fallback", skill_id=skill_id, session_id=session_id,
                                              turn_id=turn_id, revision_id=revision_id,
                                              error_code="worker_startup_failed", error=exc, recorder=event_recorder)
                        return None, _elapsed_ms(started)
                queue_depth = sum(1 for items in self._sandbox_workers.values() for item in items if item.lock.locked())
                if not queued:
                    queued = True
                    self._emit_pool_event("sandbox_worker_acquire_queued", skill_id=skill_id, session_id=session_id,
                                          turn_id=turn_id, revision_id=revision_id, queue_depth=queue_depth, recorder=event_recorder)
                    self._emit_pool_event("sandbox_worker_pool_saturated", skill_id=skill_id, session_id=session_id,
                                          turn_id=turn_id, revision_id=revision_id, queue_depth=queue_depth, recorder=event_recorder)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._emit_pool_event("sandbox_worker_acquire_timeout", skill_id=skill_id, session_id=session_id,
                                          turn_id=turn_id, revision_id=revision_id, queue_depth=queue_depth,
                                          error_code="worker_acquire_timeout", recorder=event_recorder)
                    return None, _elapsed_ms(started)
                self._sandbox_worker_condition.wait(timeout=min(remaining, 0.1))

    def _release_worker(self, worker: SandboxWorker) -> None:
        if worker.lock.locked():
            worker.lock.release()
        with self._sandbox_worker_condition:
            worker.last_used_at = time.time()
            self._sandbox_worker_condition.notify_all()

    def _discard_worker(self, key: str, target: SandboxWorker | None = None, *,
                        session_id: str | None = None, turn_id: str | None = None,
                        revision_id: str | None = None, recorder: Any | None = None) -> None:
        with self._sandbox_worker_condition:
            workers = self._sandbox_workers.get(key, [])
            removed = [item for item in workers if target is None or item is target]
            self._sandbox_workers[key] = [item for item in workers if item not in removed]
            self._sandbox_worker_condition.notify_all()
        for worker in removed:
            self._close_worker(worker)
            self._emit_worker_event(
                "sandbox_worker_destroyed", worker, session_id=session_id, turn_id=turn_id,
                revision_id=revision_id, error_code="execution_failed", recorder=recorder,
            )

    def _close_worker(self, worker: SandboxWorker) -> None:
        if worker.persistent_worker is not None:
            try:
                worker.persistent_worker.close()
            except Exception:
                pass
        for method_name in ("close", "shutdown", "cleanup"):
            method = getattr(worker.container, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
                break
        if worker.capacity_reserved:
            try:
                worker.capacity_reserved = False
                self._sandbox_capacity.release()
            except ValueError:
                pass

    def _emit_pool_event(self, event_type: str, *, skill_id: str, session_id: str | None = None,
                         turn_id: str | None = None, revision_id: str | None = None,
                         queue_depth: int = 0, error_code: str | None = None,
                         error: Exception | None = None, recorder: Any | None = None) -> None:
        recorder = recorder or self.event_recorder
        if not recorder:
            return
        pool_size = sum(len(items) for items in self._sandbox_workers.values())
        busy_workers = sum(1 for items in self._sandbox_workers.values() for item in items if item.lock.locked())
        payload = {
            "execution_id": uuid4().hex,
            "skill_id": skill_id,
            "revision_id": revision_id,
            "execution_mode": "sandbox_cold_fallback" if event_type == "sandbox_cold_fallback" else "sandbox_persistent_worker",
            "worker_pool_key_hash": hashlib.sha256(
                f"{skill_id}\0{revision_id or ''}".encode("utf-8")
            ).hexdigest()[:32],
            "queue_depth": queue_depth,
            "pool_size": pool_size,
            "busy_workers": busy_workers,
            "idle_workers": max(0, pool_size - busy_workers),
            "script_compute_ms": 0,
            "total_ms": 0,
            "error_code": error_code,
            "error_type": type(error).__name__ if error else None,
            "error_message": _safe_worker_error(error) if error else None,
            "retriable": bool(error_code in {"worker_acquire_timeout", "worker_startup_failed"}),
        }
        try:
            recorder(event_type, payload, session_id=session_id, turn_id=turn_id)
        except TypeError:
            try:
                recorder(event_type, payload)
            except Exception:
                pass
        except Exception:
            pass

    def _emit_worker_event(self, event_type: str, worker: SandboxWorker, *, session_id: str | None = None,
                           turn_id: str | None = None, revision_id: str | None = None,
                           queue_depth: int = 0, acquire_wait_ms: float = 0,
                           error_code: str | None = None, recorder: Any | None = None) -> None:
        recorder = recorder or self.event_recorder
        if not recorder:
            return
        pool_size = len(self._sandbox_workers.get(worker.key, []))
        busy = sum(1 for item in self._sandbox_workers.get(worker.key, []) if item.lock.locked())
        payload = {
            "execution_id": uuid4().hex,
            "skill_id": worker.skill_id,
            "revision_id": revision_id,
            "worker_pool_key_hash": worker.key,
            "execution_mode": "sandbox_persistent_worker",
            "worker_reused": event_type == "sandbox_worker_reused",
            "pool_size": pool_size,
            "busy_workers": busy,
            "idle_workers": max(0, pool_size - busy),
            "queue_depth": queue_depth,
            "acquire_wait_ms": acquire_wait_ms,
            "worker_startup_ms": 0,
            "script_compute_ms": 0,
            "total_ms": 0,
            "error_code": error_code,
            "retriable": bool(error_code),
        }
        try:
            recorder(event_type, payload, session_id=session_id, turn_id=turn_id)
        except TypeError:
            try:
                recorder(event_type, payload)
            except Exception:
                pass
        except Exception:
            pass

    @staticmethod
    def _requirements_hash(skill_dir: Path) -> str:
        requirements = Path(skill_dir) / "scripts" / "requirements.txt"
        if not requirements.exists():
            return "missing"
        raw = requirements.read_text(encoding="utf-8", errors="replace")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] if raw.strip() else "empty"

    @staticmethod
    def _skill_content_hash(skill_dir: Path) -> str:
        return MSAgentRuntimeAdapter._directory_hash(Path(skill_dir))

    @staticmethod
    def _directory_hash(directory: Path) -> str:
        digest = hashlib.sha256()
        root = Path(directory)
        if not root.exists():
            return "missing"
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or ".git" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()[:32]

    @staticmethod
    def _scripts_cacheable(skill_dir: Path, loaded_scripts: list[dict[str, Any]]) -> bool:
        if not loaded_scripts:
            return False
        if any(str(item.get("path") or item.get("name") or "").endswith("requirements.txt") for item in loaded_scripts):
            return False
        nondeterministic = {"time", "random", "secrets", "uuid", "datetime", "os", "sys", "subprocess", "socket", "requests", "httpx", "urllib", "shutil"}
        for script in loaded_scripts:
            path = Path(str(script.get("abs_path") or script.get("path") or script.get("name")))
            if not path.is_absolute():
                path = Path(skill_dir) / path
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                return False
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name.split(".")[0] in nondeterministic for alias in node.names):
                        return False
                elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in nondeterministic:
                    return False
        return True

    def _local_fast_path_allowed(
        self,
        skill_id: str,
        skill_dir: Path,
        loaded_scripts: list[dict[str, Any]],
    ) -> bool:
        if str(skill_id) != CAREER_PLAN_SKILL_ID or not loaded_scripts:
            return False
        root = skill_dir.resolve()
        for script in loaded_scripts:
            path = Path(str(script.get("abs_path") or script.get("path") or script.get("name")))
            if not path.is_absolute():
                path = skill_dir / path
            try:
                relative = path.resolve().relative_to(root).as_posix()
            except ValueError:
                return False
            if not path.is_file():
                return False
            if path.name not in self._LOCAL_FAST_SCRIPT_NAMES:
                return False
            if relative not in {"scripts/profile_op.py", "scripts/__pycache__/output_sanitizer.py"}:
                return False
        return True

    @staticmethod
    def _local_script_input(
        script: dict[str, Any],
        script_path: Path,
        script_inputs: dict[str, dict[str, Any]] | None,
    ) -> dict[str, Any]:
        inputs = script_inputs or {}
        candidates = [
            script_path.name,
            str(script.get("name") or ""),
            str(script.get("path") or ""),
            script_path.as_posix(),
        ]
        for key in candidates:
            value = inputs.get(key)
            if isinstance(value, dict):
                return dict(value)
        for key in ("*", "__default__"):
            value = inputs.get(key)
            if isinstance(value, dict):
                return dict(value)
        return {"query": "", "parameters": {}, "facts": {}, "messages": [], "script": script_path.name}

    def prewarm_runtime_skills(self, bundles: dict[str, Any]) -> list[CoreTraceStep]:
        if not self.sandbox_prewarm_enabled:
            return [
                CoreTraceStep(
                    name="sandbox_prepare",
                    status="skipped",
                    detail="sandbox prewarm disabled by runtime config",
                )
            ]
        steps: list[CoreTraceStep] = []
        for skill_id, bundle in sorted(bundles.items()):
            runtime_metadata = getattr(bundle, "runtime_metadata", None)
            if getattr(runtime_metadata, "skill_type", "") != "native":
                continue
            skill_dir = Path(getattr(bundle, "root_dir"))
            steps.append(self.schedule_sandbox_prepare(skill_id, skill_dir))
        return steps

    def schedule_sandbox_prepare(self, skill_id: str, skill_dir: Path) -> CoreTraceStep:
        if not self.sandbox_prewarm_enabled:
            return self._sandbox_prepare_trace(
                SandboxPrepareResult(
                    status="skipped",
                    state="skipped",
                    detail="sandbox prepare skipped because prewarm is disabled",
                    payload={"skill_id": skill_id, "skill_dir": str(skill_dir)},
                ),
                from_background=True,
            )
        if not self._has_executable_scripts(skill_dir):
            return self._sandbox_prepare_trace(
                SandboxPrepareResult(
                    status="skipped",
                    state="skipped",
                    detail="sandbox prepare skipped because the Skill has no executable Python scripts",
                    payload={"skill_id": skill_id, "skill_dir": str(skill_dir)},
                ),
                from_background=True,
            )

        key, requirements_hash = self._sandbox_prepare_key(skill_id, skill_dir)
        with self._sandbox_prepare_lock:
            record = self._sandbox_prepare_records.get(key)
            if record:
                if record.future.done():
                    return self._sandbox_prepare_trace(
                        self._sandbox_record_result(record, requirements_hash),
                        from_background=True,
                    )
                return self._sandbox_prepare_trace(
                    SandboxPrepareResult(
                        status="success",
                        state="running",
                        detail="sandbox prepare is already running in background",
                        payload={
                            "skill_id": skill_id,
                            "skill_dir": str(skill_dir),
                            "prepare_key": key,
                            "requirements_hash": requirements_hash,
                        },
                    ),
                    from_background=True,
                )

            future = self._sandbox_prepare_executor.submit(
                self._prepare_sandbox_sync,
                skill_id,
                skill_dir,
                key,
                requirements_hash,
            )
            self._sandbox_prepare_records[key] = SandboxPrepareRecord(
                key=key,
                skill_id=skill_id,
                skill_dir=skill_dir,
                requirements_hash=requirements_hash,
                future=future,
                scheduled_at=time.perf_counter(),
            )
            if self.sandbox_worker_reuse_enabled:
                future.add_done_callback(
                    lambda _future, sid=skill_id, sdir=Path(skill_dir): self._prewarm_worker_after_prepare(sid, sdir)
                )

        return self._sandbox_prepare_trace(
            SandboxPrepareResult(
                status="success",
                state="scheduled",
                detail="sandbox prepare scheduled in background",
                payload={
                    "skill_id": skill_id,
                    "skill_dir": str(skill_dir),
                    "prepare_key": key,
                    "requirements_hash": requirements_hash,
                },
            ),
            from_background=True,
        )

    def _prewarm_worker_after_prepare(self, skill_id: str, skill_dir: Path) -> None:
        try:
            key, requirements_hash = self._sandbox_prepare_key(skill_id, skill_dir)
            record = self._sandbox_prepare_records.get(key)
            if record is not None and record.future.exception() is not None:
                return
            self._get_or_create_worker(
                core=self._runtime_core(),
                skill_id=skill_id,
                skill_dir=skill_dir,
                content_hash=self._skill_content_hash(skill_dir),
                requirements_hash=requirements_hash,
            )
        except Exception:
            # Preparation is best-effort; the request path retains the cold
            # sandbox fallback and records its own failure details.
            return

    def await_sandbox_prepare(
        self,
        skill_id: str,
        skill_dir: Path,
    ) -> tuple[SandboxPrepareResult, list[CoreTraceStep]]:
        if not self._has_executable_scripts(skill_dir):
            result = SandboxPrepareResult(
                status="skipped",
                state="skipped",
                detail="sandbox prepare skipped because the Skill has no executable Python scripts",
                payload={"skill_id": skill_id, "skill_dir": str(skill_dir)},
            )
            return result, [self._sandbox_prepare_trace(result, from_background=False)]

        key, requirements_hash = self._sandbox_prepare_key(skill_id, skill_dir)
        with self._sandbox_prepare_lock:
            record = self._sandbox_prepare_records.get(key)

        if not record:
            started = time.perf_counter()
            result = self._prepare_sandbox_sync(skill_id, skill_dir, key, requirements_hash)
            return result, [
                self._sandbox_prepare_trace(
                    result,
                    from_background=False,
                    wait_duration_ms=_elapsed_ms(started),
                )
            ]

        waited_for_running_task = not record.future.done()
        started_wait = time.perf_counter()
        steps: list[CoreTraceStep] = []
        if waited_for_running_task:
            steps.append(
                self._sandbox_prepare_trace(
                    SandboxPrepareResult(
                        status="success",
                        state="running",
                        detail="waiting for background sandbox prepare to finish",
                        payload={
                            "skill_id": skill_id,
                            "skill_dir": str(skill_dir),
                            "prepare_key": key,
                            "requirements_hash": requirements_hash,
                        },
                    ),
                    from_background=True,
                    waited_for_running_task=True,
                )
            )

        result = self._sandbox_record_result(record, requirements_hash)
        steps.append(
            self._sandbox_prepare_trace(
                result,
                from_background=True,
                waited_for_running_task=waited_for_running_task,
                wait_duration_ms=_elapsed_ms(started_wait),
            )
        )
        return result, steps

    def _runtime_core(self) -> AgentSkillRuntimeCore:
        return AgentSkillRuntimeCore(
            runtime_probe=self.runtime_probe,
            runtime_dir=self.runtime_dir,
            script_reviewer=self._shared_core_script_reviewer,
            docker_checker=self._docker_available,
        )

    def _prepare_sandbox_sync(
        self,
        skill_id: str,
        skill_dir: Path,
        prepare_key: str,
        requirements_hash: str,
    ) -> SandboxPrepareResult:
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "skill_id": skill_id,
            "skill_dir": str(skill_dir),
            "prepare_key": prepare_key,
            "requirements_hash": requirements_hash,
        }
        if not self.available or not self.runtime_probe.imports:
            return SandboxPrepareResult(
                status="error",
                state="error",
                detail="sandbox prepare failed because MS-Agent runtime is unavailable",
                payload={**payload, "error": self.error, "duration_ms": _elapsed_ms(started)},
                blocks_execution=True,
            )

        docker_started = time.perf_counter()
        docker_ok, docker_detail = self._docker_available()
        payload["docker"] = docker_detail
        payload["docker_check_duration_ms"] = _elapsed_ms(docker_started)
        if not docker_ok:
            return SandboxPrepareResult(
                status="warning",
                state="error",
                detail="sandbox prepare failed because Docker sandbox is unavailable",
                payload={**payload, "duration_ms": _elapsed_ms(started)},
                blocks_execution=True,
            )

        requirements_state = "skipped"
        core = self._runtime_core()
        ensure_requirements = getattr(core, "_ensure_requirements_cache", None)
        if callable(ensure_requirements):
            dependency_site_packages, requirement_steps = ensure_requirements(
                skill_id=skill_id,
                skill_dir=skill_dir,
            )
            normalized_requirement_steps = [self._dump_core_step(step) for step in requirement_steps]
            payload["requirements_steps"] = normalized_requirement_steps
            requirements_step = next(
                (step for step in normalized_requirement_steps if step.get("name") == "requirements_cache"),
                None,
            )
            requirements_payload = requirements_step.get("payload", {}) if requirements_step else {}
            requirements_state = str(
                requirements_payload.get("state")
                or (requirements_step or {}).get("status")
                or "skipped"
            )
            if dependency_site_packages is False:
                return SandboxPrepareResult(
                    status="warning",
                    state="error",
                    detail="sandbox prepare failed because requirements installation failed",
                    payload={**payload, "duration_ms": _elapsed_ms(started)},
                    blocks_execution=True,
                )
            if dependency_site_packages:
                payload["dependency_site_packages"] = str(dependency_site_packages)
        else:
            payload["requirements_steps"] = [
                {
                    "name": "requirements_cache",
                    "status": "skipped",
                    "detail": "runtime core does not expose requirements cache preinstall",
                    "payload": {},
                }
            ]

        warmup_started = time.perf_counter()
        warmup_result = self._warm_sandbox(skill_id, skill_dir, prepare_key)
        payload["warmup"] = warmup_result
        payload["warmup_duration_ms"] = _elapsed_ms(warmup_started)
        payload["duration_ms"] = _elapsed_ms(started)
        if not warmup_result.get("ok"):
            return SandboxPrepareResult(
                status="warning",
                state="error",
                detail="sandbox prepare failed during lightweight sandbox warmup",
                payload=payload,
                blocks_execution=True,
            )

        state = "cache_hit" if requirements_state == "hit" else "success"
        return SandboxPrepareResult(
            status="success",
            state=state,
            detail="sandbox prepare completed",
            payload={**payload, "requirements_state": requirements_state},
        )

    def _warm_sandbox(self, skill_id: str, skill_dir: Path, prepare_key: str) -> dict[str, Any]:
        try:
            if not self.runtime_probe.imports:
                return {"ok": False, "error": "MS-Agent imports unavailable"}
            container_cls = self.runtime_probe.imports["SkillContainer"]  # type: ignore[index]
            workspace_dir = self.runtime_dir / "sandbox_warmups" / prepare_key
            workspace_dir.mkdir(parents=True, exist_ok=True)
            container = container_cls(workspace_dir=workspace_dir, use_sandbox=True)
            if hasattr(container, "mount_skill_directory"):
                container.mount_skill_directory(skill_id, skill_dir)
            if not hasattr(container, "image"):
                return {"ok": True, "state": "container_config_initialized"}
            image = str(getattr(container, "image", "") or "python:3.11-slim")
            inspect_result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if inspect_result.returncode == 0:
                return {"ok": True, "image": image, "state": "image_present"}
            pull_result = subprocess.run(
                ["docker", "pull", image],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            return {
                "ok": pull_result.returncode == 0,
                "image": image,
                "state": "image_pulled" if pull_result.returncode == 0 else "image_pull_failed",
                "stdout": pull_result.stdout[-1000:],
                "stderr": pull_result.stderr[-1000:],
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _sandbox_prepare_trace(
        self,
        result: SandboxPrepareResult,
        *,
        from_background: bool,
        waited_for_running_task: bool = False,
        wait_duration_ms: int = 0,
    ) -> CoreTraceStep:
        payload = {
            **result.payload,
            "state": result.state,
            "from_background": from_background,
            "waited_for_running_task": waited_for_running_task,
        }
        if wait_duration_ms:
            payload["wait_duration_ms"] = wait_duration_ms
        return CoreTraceStep(
            name="sandbox_prepare",
            status=result.status,  # type: ignore[arg-type]
            detail=result.detail,
            payload=payload,
        )

    def _sandbox_prepare_key(self, skill_id: str, skill_dir: Path) -> tuple[str, str]:
        requirements = skill_dir / "scripts" / "requirements.txt"
        if not requirements.exists():
            requirements_hash = "missing"
        else:
            raw = requirements.read_text(encoding="utf-8", errors="replace")
            requirements_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] if raw.strip() else "empty"
        key_seed = f"{skill_id}\0{skill_dir.resolve()}\0{requirements_hash}"
        return hashlib.sha256(key_seed.encode("utf-8")).hexdigest()[:24], requirements_hash

    def _has_executable_scripts(self, skill_dir: Path) -> bool:
        scripts_dir = skill_dir / "scripts"
        if not scripts_dir.exists():
            return False
        return any(path.is_file() and path.name != "__init__.py" for path in scripts_dir.rglob("*.py"))

    def _script_review_blocks_execution(self, skill_dir: Path) -> bool:
        return any(str(getattr(finding, "status", "")) == "error" for finding in review_scripts(skill_dir))

    def _shared_core_script_reviewer(self, package_root: Path) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                name=finding.name,
                status=getattr(finding.status, "value", finding.status),
                detail=finding.detail,
                payload=finding.payload,
            )
            for finding in review_scripts(package_root)
        ]

    def _docker_available(self) -> tuple[bool, str]:
        if not shutil.which("docker"):
            return False, "docker executable not found"
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or "docker info failed").strip()
        return True, "docker available"

    def _sandbox_record_result(
        self,
        record: SandboxPrepareRecord,
        requirements_hash: str,
    ) -> SandboxPrepareResult:
        try:
            return record.future.result()
        except Exception as exc:  # pragma: no cover - defensive; worker catches normal failures
            return SandboxPrepareResult(
                status="error",
                state="error",
                detail=f"sandbox prepare failed: {exc}",
                payload={
                    "skill_id": record.skill_id,
                    "skill_dir": str(record.skill_dir),
                    "prepare_key": record.key,
                    "requirements_hash": requirements_hash,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                blocks_execution=True,
            )

    def _dump_core_step(self, step: Any) -> dict[str, Any]:
        return {
            "name": "runtime_health" if getattr(step, "name", "") == "ms_agent_health" else getattr(step, "name", ""),
            "status": getattr(step, "status", ""),
            "detail": getattr(step, "detail", ""),
            "payload": getattr(step, "payload", {}) or {},
        }

    def _normalize_step(self, step: CoreTraceStep) -> CoreTraceStep:
        if step.name == "ms_agent_health":
            return CoreTraceStep(
                name="runtime_health",
                status=step.status,
                detail=step.detail,
                payload=step.payload,
            )
        return step


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _safe_worker_error(error: Exception) -> str:
    text = " ".join(str(error).split())
    lowered = text.lower()
    if any(marker in lowered for marker in ("password", "secret", "api_key", "authorization", "token=")):
        return f"{type(error).__name__}"
    return text[:240]


def _supports_persistent_sandbox(container: Any) -> bool:
    """Only use the direct ms-enclave bridge for the real MS-Agent class."""
    return any(
        str(getattr(base, "__module__", "")).startswith("ms_agent")
        for base in type(container).__mro__
    ) and hasattr(container, "_get_sandbox")


def _script_args_from_payload(payload: dict[str, Any]) -> list[str]:
    args: list[str] = []
    cli_keys = {
        "query": "query",
        "major": "major",
        "career": "career",
        "subjects": "selected-subjects",
        "limit": "limit",
        "user_id": "user-id",
        "session_id": "session-id",
    }
    for key, cli_key in cli_keys.items():
        value = payload.get(key)
        if value in (None, "", []):
            continue
        args.extend([f"--{cli_key}", ",".join(str(item) for item in value) if isinstance(value, list) else str(value)])
    return args


def _script_output_ok(output: dict[str, Any]) -> bool:
    if output.get("ok") is not None:
        return bool(output.get("ok"))
    return output.get("exit_code") in (0, "0", "success")


def _script_skip_detail(detail: str) -> str:
    if detail.startswith("sandbox prepare failed because "):
        return "script execution skipped because " + detail.split("sandbox prepare failed because ", 1)[1]
    if detail.startswith("sandbox prepare failed during "):
        return "script execution skipped because " + detail.split("sandbox prepare failed during ", 1)[1]
    return detail
