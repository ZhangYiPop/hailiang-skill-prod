from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_skill_runtime_core.models import CoreTraceStep, LoadedSkillContext, MSAgentRuntimeProbe

ScriptReviewer = Callable[[Path], list[Any]]
DockerChecker = Callable[[], tuple[bool, str]]


class AgentSkillRuntimeCore:
    """Shared ms-agent adapter used by debug workspace and Hailiang runtime.

    This class intentionally delegates Skill loading, turn planning, lazy
    resource loading, and script sandboxing to ms-agent. Platform code owns
    persistence, routing, LLM calls, and user-facing API models.
    """

    def __init__(
        self,
        *,
        runtime_probe: MSAgentRuntimeProbe,
        runtime_dir: Path,
        script_reviewer: ScriptReviewer | None = None,
        docker_checker: DockerChecker | None = None,
    ) -> None:
        self.runtime_probe = runtime_probe
        self.runtime_dir = Path(runtime_dir)
        self.script_reviewer = script_reviewer
        self.docker_checker = docker_checker or default_docker_checker

    def health_step(self) -> CoreTraceStep:
        if self.runtime_probe.available:
            return CoreTraceStep(
                name="runtime_health",
                status="success",
                detail="MS-Agent runtime imports are available",
                payload={"status": self.runtime_probe.status},
            )
        return CoreTraceStep(
            name="runtime_health",
            status="warning" if self.runtime_probe.status == "degraded" else "error",
            detail="MS-Agent runtime is unavailable",
            payload={"status": self.runtime_probe.status, "error": self.runtime_probe.error},
        )

    def check_load(self, skill_dir: Path) -> CoreTraceStep:
        if not self.runtime_probe.available or not self.runtime_probe.imports:
            return CoreTraceStep(
                name="skill_loader",
                status="error",
                detail="MS-Agent SkillLoader is unavailable",
                payload={"error": self.runtime_probe.error},
            )
        try:
            loaded = self._load_skills(skill_dir)
        except Exception as exc:
            return CoreTraceStep(
                name="skill_loader",
                status="warning",
                detail=f"MS-Agent SkillLoader failed: {exc}",
                payload={"error": f"{type(exc).__name__}: {exc}", "source": str(skill_dir)},
            )
        return CoreTraceStep(
            name="skill_loader",
            status="success" if loaded else "warning",
            detail="MS-Agent SkillLoader parsed Skill catalog"
            if loaded
            else "MS-Agent SkillLoader did not parse any Skill",
            payload={"source": str(skill_dir), "loaded_skill_keys": sorted(loaded.keys())},
        )

    def build_planning_query(
        self,
        message: str,
        history: list[Any],
        memory_context: dict[str, Any] | None = None,
    ) -> str:
        recent = history[-8:]
        if not recent:
            query = f"Latest user message:\n{message}"
        else:
            lines = ["Recent conversation:"]
            for item in recent:
                role = getattr(item, "role", "")
                content = getattr(item, "content", "")
                speaker = "User" if role == "user" else "Assistant"
                lines.append(f"{speaker}: {content}")
            lines.extend(["", "Latest user message:", message])
            query = "\n".join(lines)
        memory_text = _memory_context_text(memory_context)
        if memory_text:
            return f"{memory_text}\n\n{query}"
        return query

    def plan_turn(
        self,
        *,
        skill_dir: Path,
        planner_llm: Any,
        planning_query: str,
    ) -> tuple[Any, Any, Any, dict[str, Any], list[CoreTraceStep]]:
        self._assert_available()
        loaded = self._load_skills(skill_dir)
        if not loaded:
            raise RuntimeError(f"MS-Agent SkillLoader could not parse {skill_dir}")

        skill_key, skill_schema = next(iter(loaded.items()))
        steps = [
            self.health_step(),
            CoreTraceStep(
                name="skill_loader",
                status="success",
                detail="MS-Agent SkillLoader loaded active Skill",
                payload={"skill_key": skill_key, "skill_path": str(skill_schema.skill_path)},
            ),
        ]

        analyzer_cls = self.runtime_probe.imports["SkillAnalyzer"]  # type: ignore[index]
        analyzer = analyzer_cls(planner_llm)
        started = time.perf_counter()
        skill_context = analyzer.analyze_skill_plan(skill_schema, planning_query, skill_schema.skill_path)
        plan = skill_context.plan
        if not plan or not plan.can_handle:
            raise RuntimeError("MS-Agent SkillAnalyzer did not produce an executable plan")

        plan_payload = self._plan_payload(plan, planner_llm)
        plan_payload["duration_ms"] = _elapsed_ms(started)
        steps.append(
            CoreTraceStep(
                name="skill_plan",
                status="success",
                detail=plan.plan_summary or "MS-Agent SkillAnalyzer produced a plan",
                payload=plan_payload,
            )
        )
        return skill_key, skill_schema, skill_context, plan_payload, steps

    def load_context_from_plan(
        self,
        *,
        skill_context: Any,
        turn: int,
        previous_lazy_load: dict[str, list[str]],
    ) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]], dict[str, list[str]], CoreTraceStep]:
        started = time.perf_counter()
        skill_context.load_from_plan()
        loaded_refs = self._loaded_items(skill_context.references)
        loaded_scripts = self._loaded_items(skill_context.scripts)
        loaded_resources = self._loaded_items(skill_context.resources)
        current_lazy_load = {
            "references": [item["name"] for item in loaded_refs],
            "scripts": [item["name"] for item in loaded_scripts],
            "resources": [item["name"] for item in loaded_resources],
        }
        plan = skill_context.plan
        step = CoreTraceStep(
            name="lazy_resource_load",
            status="success",
            detail=f"Turn {turn}: loaded only resources requested by the MS-Agent plan",
            payload={
                "turn": turn,
                **current_lazy_load,
                "selection_reason": getattr(plan, "reasoning", ""),
                "previous_diff": lazy_load_diff(previous_lazy_load, current_lazy_load),
                "duration_ms": _elapsed_ms(started),
            },
        )
        return loaded_refs, loaded_scripts, loaded_resources, current_lazy_load, step

    def execute_scripts_in_sandbox(
        self,
        *,
        skill_id: str,
        skill_dir: Path,
        loaded_scripts: list[dict[str, str]],
        execute_scripts: bool,
        script_inputs: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[CoreTraceStep]]:
        if not execute_scripts:
            return [], [
                CoreTraceStep(
                    name="script_execution",
                    status="skipped",
                    detail="script execution disabled by request",
                )
            ]
        if not loaded_scripts:
            return [], [
                CoreTraceStep(
                    name="script_execution",
                    status="skipped",
                    detail="MS-Agent plan did not require scripts",
                )
            ]

        preflight_steps: list[CoreTraceStep] = []
        if self.script_reviewer:
            review_started = time.perf_counter()
            findings = self.script_reviewer(skill_dir)
            review_duration_ms = _elapsed_ms(review_started)
            blocking = [finding for finding in findings if str(getattr(finding, "status", "")) == "error"]
            preflight_steps.append(
                CoreTraceStep(
                    name="script_review",
                    status="warning" if blocking else "success",
                    detail="static script safety review blocked execution"
                    if blocking
                    else "static script safety review passed",
                    payload={
                        "findings": [_dump_model(finding) for finding in findings],
                        "script_review_duration_ms": review_duration_ms,
                    },
                )
            )
            if blocking:
                return [], [
                    *preflight_steps,
                    CoreTraceStep(
                        name="script_execution",
                        status="warning",
                        detail="script execution skipped because static safety review failed",
                        payload={
                            "findings": [_dump_model(finding) for finding in findings],
                            "script_review_duration_ms": review_duration_ms,
                        },
                    )
                ]
        else:
            review_duration_ms = 0
            preflight_steps.append(
                CoreTraceStep(
                    name="script_review",
                    status="skipped",
                    detail="no static script reviewer configured",
                    payload={"script_review_duration_ms": review_duration_ms},
                )
            )

        docker_started = time.perf_counter()
        docker_ok, docker_detail = self.docker_checker()
        docker_duration_ms = _elapsed_ms(docker_started)
        preflight_steps.append(
            CoreTraceStep(
                name="sandbox_startup",
                status="success" if docker_ok else "warning",
                detail="Docker sandbox is available"
                if docker_ok
                else "Docker sandbox is unavailable; script execution will be skipped",
                payload={"docker": docker_detail, "docker_check_duration_ms": docker_duration_ms},
            )
        )
        if not docker_ok:
            return [], [
                *preflight_steps,
                CoreTraceStep(
                    name="script_execution",
                    status="warning",
                    detail="script execution skipped because Docker sandbox is unavailable",
                    payload={"docker": docker_detail, "docker_check_duration_ms": docker_duration_ms},
                )
            ]

        self._assert_available()
        dependency_site_packages, requirement_steps = self._ensure_requirements_cache(
            skill_id=skill_id,
            skill_dir=skill_dir,
        )
        if dependency_site_packages is False:
            return [], [
                *preflight_steps,
                *requirement_steps,
                CoreTraceStep(
                    name="script_execution",
                    status="warning",
                    detail="script execution skipped because requirements installation failed",
                ),
            ]

        steps = [
            *preflight_steps,
            *requirement_steps,
            CoreTraceStep(
                name="execution_command",
                status="success",
                detail="Executing loaded Python scripts with MS-Agent SkillContainer sandbox",
                payload={
                    "scripts": [str(item.get("path") or item.get("name")) for item in loaded_scripts],
                    "script_review_duration_ms": review_duration_ms,
                    "docker_check_duration_ms": docker_duration_ms,
                },
            )
        ]
        try:
            container_cls = self._future_import_safe_container(self.runtime_probe.imports["SkillContainer"])  # type: ignore[index]
            execution_input_cls = self.runtime_probe.imports["ExecutionInput"]  # type: ignore[index]
            workspace_dir = self.runtime_dir / "workspace_runs" / uuid.uuid4().hex[:12]
            container_started = time.perf_counter()
            container = container_cls(workspace_dir=workspace_dir, use_sandbox=True)
            container.mount_skill_directory(skill_id, skill_dir)
            if dependency_site_packages:
                setattr(container, "_dependency_site_packages", dependency_site_packages)
            steps.append(
                CoreTraceStep(
                    name="sandbox_startup",
                    status="success",
                    detail="MS-Agent SkillContainer sandbox initialized",
                    payload={
                        "workspace_dir": str(workspace_dir),
                        "duration_ms": _elapsed_ms(container_started),
                    },
                )
            )

            outputs: list[dict[str, Any]] = []
            for script in loaded_scripts:
                script_path = Path(str(script.get("abs_path") or script.get("path")))
                if not script_path.is_absolute():
                    script_path = skill_dir / script_path
                payload = self._script_input_for(script=script, script_path=script_path, script_inputs=script_inputs)
                args = _script_args_from_payload(payload)
                started = time.perf_counter()
                output = asyncio.run(
                    container.execute_python_script(
                        script_path,
                        skill_id=skill_id,
                        input_spec=execution_input_cls(
                            args=args,
                            stdin=json.dumps(payload, ensure_ascii=False),
                            working_dir=skill_dir,
                            requirements=[],
                        ),
                    )
                )
                output_data = output.to_dict() if hasattr(output, "to_dict") else dict(output)
                outputs.append(
                    {
                        "script": script_path.name,
                        "args": args,
                        "stdin_payload": payload,
                        "duration_ms": _elapsed_ms(started),
                        **output_data,
                    }
                )

            status = "success" if all(_sandbox_result_success(item) for item in outputs) else "warning"
            steps.append(
                CoreTraceStep(
                    name="script_execution",
                    status=status,  # type: ignore[arg-type]
                    detail="MS-Agent SkillContainer completed script execution",
                    payload={"outputs": outputs},
                )
            )
            return outputs, steps
        except Exception as exc:
            steps.append(
                CoreTraceStep(
                    name="script_execution",
                    status="warning",
                    detail=f"script execution failed in MS-Agent SkillContainer: {exc}",
                    payload={"error": f"{type(exc).__name__}: {exc}"},
                )
            )
            return [], steps

    def _script_input_for(
        self,
        *,
        script: dict[str, str],
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
        return {
            "query": "",
            "parameters": {},
            "facts": {},
            "messages": [],
            "script": script_path.name,
        }

    def _ensure_requirements_cache(
        self,
        *,
        skill_id: str,
        skill_dir: Path,
    ) -> tuple[Path | None | bool, list[CoreTraceStep]]:
        requirements, raw_requirements = _read_script_requirements(skill_dir)
        if not requirements:
            return None, [
                CoreTraceStep(
                    name="requirements_cache",
                    status="skipped",
                    detail="scripts/requirements.txt is empty; dependency cache skipped",
                )
            ]

        requirements_hash = hashlib.sha256(raw_requirements.encode("utf-8")).hexdigest()[:16]
        cache_root = self.runtime_dir / "sandbox_deps" / _safe_cache_skill_id(skill_id) / requirements_hash
        site_packages = cache_root / "site-packages"
        marker = cache_root / ".complete"
        payload = {
            "skill_id": skill_id,
            "requirements_hash": requirements_hash,
            "requirements": requirements,
            "site_packages": str(site_packages),
        }
        if marker.exists() and site_packages.exists():
            return site_packages, [
                CoreTraceStep(
                    name="requirements_cache",
                    status="success",
                    detail="requirements cache hit",
                    payload={**payload, "state": "hit"},
                )
            ]

        started = time.perf_counter()
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
            site_packages.mkdir(parents=True, exist_ok=True)
            (cache_root / "requirements.txt").write_text(raw_requirements, encoding="utf-8")
            container_cls = self.runtime_probe.imports["SkillContainer"]  # type: ignore[index]
            container = container_cls(workspace_dir=cache_root, use_sandbox=True)
            result = asyncio.run(
                container._execute_in_sandbox(
                    shell_command=(
                        "python -m pip install --disable-pip-version-check "
                        "-r /sandbox/requirements.txt -t /sandbox/site-packages"
                    ),
                    requirements=[],
                )
            )
            if not _sandbox_result_success(result):
                return False, [
                    CoreTraceStep(
                        name="requirements_cache",
                        status="error",
                        detail="requirements cache install failed",
                        payload={**payload, "state": "error", "result": result, "duration_ms": _elapsed_ms(started)},
                    )
                ]
            marker.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
            return site_packages, [
                CoreTraceStep(
                    name="requirements_cache",
                    status="success",
                    detail="requirements cache installed",
                    payload={**payload, "state": "success", "result": result, "duration_ms": _elapsed_ms(started)},
                )
            ]
        except Exception as exc:
            return False, [
                CoreTraceStep(
                    name="requirements_cache",
                    status="error",
                    detail="requirements cache install failed",
                    payload={
                        **payload,
                        "state": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "duration_ms": _elapsed_ms(started),
                    },
                )
            ]

    def _future_import_safe_container(self, container_cls):
        class FutureImportSafeSkillContainer(container_cls):
            async def execute_python_script(self, script_path, *args, **kwargs):
                path = Path(script_path)
                if not getattr(self, "use_sandbox", False) or not path.exists():
                    return await super().execute_python_script(script_path, *args, **kwargs)

                code = path.read_text(encoding="utf-8")
                lines = code.splitlines(keepends=True)
                stripped = [line for line in lines if not line.lstrip().startswith("from __future__ import ")]
                skill_id = kwargs.get("skill_id") or (args[0] if args else "unknown")
                input_spec = kwargs.get("input_spec")
                if input_spec is None and len(args) > 1:
                    input_spec = args[1]
                sandbox_file = self._sandbox_script_path(skill_id, path)
                prefix_lines = []
                if sandbox_file:
                    prefix_lines.append(f"__file__ = {str(sandbox_file)!r}")
                if getattr(self, "_dependency_site_packages", None):
                    prefix_lines.append("import sys as __ms_sys")
                    prefix_lines.append("__ms_sys.path.insert(0, '/sandbox/deps/site-packages')")
                stdin = getattr(input_spec, "stdin", None)
                if stdin is not None:
                    prefix_lines.append("import io as __ms_io, sys as __ms_stdin_sys")
                    prefix_lines.append(f"__ms_stdin_sys.stdin = __ms_io.StringIO({str(stdin)!r})")
                prefix = "\n".join(prefix_lines)
                if prefix:
                    prefix += "\n"
                if len(stripped) == len(lines):
                    stripped = lines

                temp_path = path.with_name(f".__exec_no_future_{uuid.uuid4().hex}_{path.name}")
                temp_path.write_text(prefix + "".join(stripped), encoding="utf-8")
                try:
                    return await super().execute_python_script(temp_path, *args, **kwargs)
                finally:
                    temp_path.unlink(missing_ok=True)

            def _sandbox_script_path(self, skill_id, path: Path) -> str | None:
                skill_dir = getattr(self, "_skill_dirs", {}).get(str(skill_id))
                if not skill_dir:
                    return None
                try:
                    rel = path.resolve().relative_to(Path(skill_dir).resolve()).as_posix()
                except ValueError:
                    rel = f"scripts/{path.name}"
                safe_id = str(skill_id).replace("@", "_").replace("/", "_")
                return f"{self.SANDBOX_ROOT}/skills/{safe_id}/{rel}"

            def _get_sandbox(self):
                if self._sandbox is None:
                    from ms_agent.sandbox.sandbox import EnclaveSandbox

                    volumes = [(str(self.workspace_dir.resolve()), self.SANDBOX_ROOT, "rw")]
                    for skill_id, skill_dir in self._skill_dirs.items():
                        safe_id = skill_id.replace("@", "_").replace("/", "_")
                        sandbox_path = f"{self.SANDBOX_ROOT}/skills/{safe_id}"
                        volumes.append((str(Path(skill_dir).resolve()), sandbox_path, "ro"))

                    dependency_site_packages = getattr(self, "_dependency_site_packages", None)
                    if dependency_site_packages:
                        volumes.append(
                            (
                                str(Path(dependency_site_packages).resolve()),
                                f"{self.SANDBOX_ROOT}/deps/site-packages",
                                "ro",
                            )
                        )

                    self._sandbox = EnclaveSandbox(
                        image=self.image,
                        memory_limit=self.memory_limit,
                        volumes=volumes,
                    )
                return self._sandbox

        FutureImportSafeSkillContainer.__name__ = container_cls.__name__
        return FutureImportSafeSkillContainer

    def build_skill_runtime_context(
        self,
        *,
        skill_id: str,
        skill_key: str,
        skill_schema: Any,
        skill_dir: Path,
        plan_payload: dict[str, Any],
        loaded_refs: list[dict[str, str]],
        loaded_scripts: list[dict[str, str]],
        loaded_resources: list[dict[str, str]],
        execution_outputs: list[dict[str, Any]],
        turn: int,
        planning_query: str,
        planner_llm: Any,
        previous_lazy_load: dict[str, list[str]],
    ) -> LoadedSkillContext:
        tools_yaml = (skill_dir / "tools.yaml").read_text(encoding="utf-8") if (skill_dir / "tools.yaml").exists() else ""
        current_lazy_load = {
            "references": [item["name"] for item in loaded_refs],
            "scripts": [item["name"] for item in loaded_scripts],
            "resources": [item["name"] for item in loaded_resources],
        }
        raw_trace = {
            "runtime": "ms_agent_single_skill",
            "skill_key": skill_key,
            "turn": turn,
            "planning_query": planning_query,
            "planner": {
                "name": getattr(planner_llm, "planner_name", ""),
                "error": getattr(planner_llm, "last_error", None),
                "raw_response": getattr(planner_llm, "last_raw_response", None),
            },
            "plan": plan_payload,
            "loaded": {
                "references": loaded_refs,
                "scripts": [{"name": item["name"], "path": item["path"]} for item in loaded_scripts],
                "resources": [{"name": item["name"], "path": item["path"]} for item in loaded_resources],
            },
            "previous_lazy_load": previous_lazy_load,
            "lazy_load_diff": lazy_load_diff(previous_lazy_load, current_lazy_load),
            "execution_outputs": execution_outputs,
        }
        return LoadedSkillContext(
            skill_key=skill_key,
            skill_path=skill_schema.skill_path,
            skill_md=skill_schema.content,
            tools_yaml=tools_yaml,
            references=[{"path": item["path"], "content": item["content"][:2500]} for item in loaded_refs],
            scripts=loaded_scripts,
            resources=loaded_resources,
            plan=plan_payload,
            execution_outputs=execution_outputs,
            raw_trace=raw_trace,
            combined_response=str(getattr(planner_llm, "last_combined_response", "") or ""),
        )

    def run_single_skill_turn(
        self,
        *,
        skill_id: str,
        skill_dir: Path,
        planner_llm: Any,
        message: str,
        history: list[Any],
        turn: int,
        previous_lazy_load: dict[str, list[str]],
        execute_scripts: bool,
        memory_context: dict[str, Any] | None = None,
    ) -> tuple[LoadedSkillContext, dict[str, list[str]], list[CoreTraceStep]]:
        planning_query = self.build_planning_query(message, history, memory_context=memory_context)
        skill_key, skill_schema, skill_context, plan_payload, steps = self.plan_turn(
            skill_dir=skill_dir,
            planner_llm=planner_llm,
            planning_query=planning_query,
        )
        loaded_refs, loaded_scripts, loaded_resources, current_lazy_load, lazy_step = self.load_context_from_plan(
            skill_context=skill_context,
            turn=turn,
            previous_lazy_load=previous_lazy_load,
        )
        steps.append(lazy_step)
        execution_outputs, script_steps = self.execute_scripts_in_sandbox(
            skill_id=skill_key,
            skill_dir=skill_schema.skill_path,
            loaded_scripts=loaded_scripts,
            execute_scripts=execute_scripts,
        )
        steps.extend(script_steps)
        context = self.build_skill_runtime_context(
            skill_id=skill_id,
            skill_key=skill_key,
            skill_schema=skill_schema,
            skill_dir=Path(skill_schema.skill_path),
            plan_payload=plan_payload,
            loaded_refs=loaded_refs,
            loaded_scripts=loaded_scripts,
            loaded_resources=loaded_resources,
            execution_outputs=execution_outputs,
            turn=turn,
            planning_query=planning_query,
            planner_llm=planner_llm,
            previous_lazy_load=previous_lazy_load,
        )
        return context, current_lazy_load, steps

    def _assert_available(self) -> None:
        if not self.runtime_probe.available or not self.runtime_probe.imports:
            raise RuntimeError(f"MS-Agent runtime unavailable: {self.runtime_probe.error or self.runtime_probe.status}")

    def _load_skills(self, skill_dir: Path) -> dict[str, Any]:
        self._assert_available()
        loader_cls = self.runtime_probe.imports["SkillLoader"]  # type: ignore[index]
        loader = loader_cls()
        return loader.load_skills(str(skill_dir))

    def _plan_payload(self, plan: Any, planner_llm: Any) -> dict[str, Any]:
        return {
            "planner": getattr(planner_llm, "planner_name", ""),
            "planner_error": getattr(planner_llm, "last_error", None),
            "can_handle": plan.can_handle,
            "plan_summary": plan.plan_summary,
            "steps": plan.steps,
            "required_scripts": plan.required_scripts,
            "required_references": plan.required_references,
            "required_resources": plan.required_resources,
            "required_packages": plan.required_packages,
            "parameters": plan.parameters,
            "reasoning": plan.reasoning,
        }

    def _loaded_items(self, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        loaded: list[dict[str, str]] = []
        for item in items:
            loaded.append(
                {
                    "name": str(item.get("name", "")),
                    "path": str(item.get("path") or item.get("abs_path") or item.get("name", "")),
                    "abs_path": str(item.get("abs_path") or item.get("path") or ""),
                    "content": str(item.get("content", "")),
                }
            )
        return loaded


def lazy_load_diff(previous: dict[str, list[str]], current: dict[str, list[str]]) -> dict[str, dict[str, list[str]]]:
    diff = {}
    for key in ["references", "scripts", "resources"]:
        before = set(previous.get(key, []))
        after = set(current.get(key, []))
        diff[key] = {
            "added": sorted(after - before),
            "removed": sorted(before - after),
            "unchanged": sorted(before & after),
        }
    return diff


def default_docker_checker() -> tuple[bool, str]:
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
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "docker info failed").strip()
    return True, "docker available"


def _dump_model(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": str(value)}


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _memory_context_text(memory_context: dict[str, Any] | None) -> str:
    if not memory_context:
        return ""
    summary = str(memory_context.get("summary") or "").strip()
    facts = memory_context.get("facts") if isinstance(memory_context.get("facts"), dict) else {}
    status = memory_context.get("status") if isinstance(memory_context.get("status"), dict) else {}
    if not summary and not facts and not status:
        return ""
    return (
        "Conversation Memory:\n"
        f"Rolling summary:\n{summary or '(none)'}\n\n"
        "Structured facts:\n"
        f"{json.dumps(facts or {}, ensure_ascii=False)}\n\n"
        "Status:\n"
        f"{json.dumps(status or {}, ensure_ascii=False)}"
    )


def _read_script_requirements(skill_dir: Path) -> tuple[list[str], str]:
    requirements_path = skill_dir / "scripts" / "requirements.txt"
    if not requirements_path.is_file():
        return [], ""
    raw = requirements_path.read_text(encoding="utf-8", errors="replace")
    requirements = [
        line.strip()
        for line in raw.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return requirements, raw


def _safe_cache_skill_id(skill_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", skill_id).strip("._") or "skill"


def _sandbox_result_success(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    status = None
    for key in ("status", "exit_code", "returncode"):
        if key in result and result.get(key) is not None:
            status = result.get(key)
            break
    return status in {0, "0", "success", "completed"} or result.get("success") is True


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
        args.append(f"--{cli_key}")
        args.append(",".join(str(item) for item in value) if isinstance(value, list) else str(value))
    return args
