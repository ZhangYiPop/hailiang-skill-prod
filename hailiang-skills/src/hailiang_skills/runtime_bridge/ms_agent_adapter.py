from __future__ import annotations

import inspect
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_skill_runtime_core import AgentSkillRuntimeCore, CoreTraceStep, MSAgentRuntimeProbe, probe_ms_agent_runtime

from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID
from hailiang_skills.runtime_bridge.script_review import review_scripts


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


class MSAgentRuntimeAdapter:
    _LOCAL_FAST_SCRIPT_NAMES = frozenset({"profile_op.py", "output_sanitizer.py"})

    def __init__(
        self,
        *,
        runtime_dir: Path,
        runtime_probe: MSAgentRuntimeProbe | None = None,
        sandbox_prewarm_enabled: bool = True,
        local_fast_path_enabled: bool = True,
        max_prepare_workers: int = 2,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.runtime_probe = runtime_probe or probe_ms_agent_runtime()
        self.sandbox_prewarm_enabled = sandbox_prewarm_enabled
        self.local_fast_path_enabled = local_fast_path_enabled
        self._sandbox_prepare_executor = ThreadPoolExecutor(
            max_workers=max_prepare_workers,
            thread_name_prefix="hailiang-sandbox-prepare",
        )
        self._sandbox_prepare_lock = threading.Lock()
        self._sandbox_prepare_records: dict[str, SandboxPrepareRecord] = {}

    @property
    def available(self) -> bool:
        return self.runtime_probe.available

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
        outputs, core_steps = core.execute_scripts_in_sandbox(**kwargs)
        return outputs, [*prepare_steps, *(self._normalize_step(step) for step in core_steps)]

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
                outputs.append(
                    {
                        "script": script_path.name,
                        "args": args,
                        "stdin_payload": payload,
                        "duration_ms": _elapsed_ms(started),
                        "exit_code": completed.returncode,
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                        "ok": completed.returncode == 0,
                        "execution_mode": "local_fast_path",
                    }
                )
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


def _script_skip_detail(detail: str) -> str:
    if detail.startswith("sandbox prepare failed because "):
        return "script execution skipped because " + detail.split("sandbox prepare failed because ", 1)[1]
    if detail.startswith("sandbox prepare failed during "):
        return "script execution skipped because " + detail.split("sandbox prepare failed during ", 1)[1]
    return detail
