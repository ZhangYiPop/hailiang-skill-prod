"""Client contract for the isolated Skill script runner.

The API process never executes an untrusted Skill script when the remote
executor is selected.  Instead it sends an immutable, content-addressed
bundle to the runner service.  Keeping the protocol here lets Harness and the
legacy runtime share exactly the same execution boundary.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SandboxExecutorError(RuntimeError):
    """A controlled failure returned by the isolated script runner."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class SandboxExecutionRequest:
    skill_id: str
    skill_version: str
    bundle_archive: str
    bundle_digest: str
    script_path: str
    input_payload: dict[str, Any]
    timeout_seconds: float = 30.0
    trace_id: str = ""


@dataclass(frozen=True, slots=True)
class SandboxExecutionResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    json_output: object | None
    duration_ms: int
    execution_id: str
    environment_key: str
    cache_hit: bool
    error_code: str = ""
    error: str = ""
    executor_type: str = "remote_runner"

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> "SandboxExecutionResult":
        return cls(
            ok=bool(payload.get("ok")),
            exit_code=payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None,
            stdout=str(payload.get("stdout") or ""),
            stderr=str(payload.get("stderr") or ""),
            json_output=payload.get("json_output"),
            duration_ms=int(payload.get("duration_ms") or 0),
            execution_id=str(payload.get("execution_id") or ""),
            environment_key=str(payload.get("environment_key") or ""),
            cache_hit=bool(payload.get("cache_hit")),
            error_code=str(payload.get("error_code") or ""),
            error=str(payload.get("error") or ""),
            executor_type=str(payload.get("executor_type") or "remote_runner"),
        )


def snapshot_skill_bundle(root_dir: Path) -> tuple[str, str]:
    """Build the deterministic archive used by both Harness and legacy paths."""
    # Imported lazily: Harness itself imports this module through tool paths.
    from hailiang_skills.harness.assets import snapshot_assets

    snapshot = snapshot_assets(root_dir)
    return str(snapshot["archive"]), str(snapshot["digest"])


class RemoteSandboxExecutor:
    """Minimal HTTP client for ``skill-sandbox-runner``."""

    def __init__(self, *, endpoint: str, token: str, request_timeout_seconds: float = 45.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.request_timeout_seconds = max(1.0, request_timeout_seconds)

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint and self.token)

    @classmethod
    def from_environment(cls) -> "RemoteSandboxExecutor | None":
        if os.getenv("HAILIANG_SCRIPT_EXECUTOR", "legacy").strip().lower() != "remote":
            return None
        endpoint = os.getenv("HAILIANG_SANDBOX_ENDPOINT", "").strip()
        token = os.getenv("HAILIANG_SANDBOX_SHARED_TOKEN", "").strip()
        if not endpoint or not token:
            raise SandboxExecutorError(
                "SANDBOX_EXECUTOR_UNAVAILABLE",
                "远端 Skill 沙箱未配置 HAILIANG_SANDBOX_ENDPOINT 或 HAILIANG_SANDBOX_SHARED_TOKEN。",
            )
        return cls(
            endpoint=endpoint,
            token=token,
            request_timeout_seconds=float(os.getenv("HAILIANG_SANDBOX_REQUEST_TIMEOUT_SECONDS", "45")),
        )

    def execute(self, request: SandboxExecutionRequest) -> SandboxExecutionResult:
        payload = {
            "skill_id": request.skill_id,
            "skill_version": request.skill_version,
            "bundle_archive": request.bundle_archive,
            "bundle_digest": request.bundle_digest,
            "script_path": request.script_path,
            "input": request.input_payload,
            "timeout_seconds": request.timeout_seconds,
            "trace_id": request.trace_id,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.endpoint}/v1/executions",
            data=encoded,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Hailiang-Sandbox-Token": self.token,
            },
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.request_timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2_000]
            raise SandboxExecutorError("SANDBOX_EXECUTION_REJECTED", detail or str(exc)) from exc
        except urllib.error.URLError as exc:
            raise SandboxExecutorError("SANDBOX_EXECUTOR_UNAVAILABLE", str(exc.reason)) from exc
        except TimeoutError as exc:
            raise SandboxExecutorError("SANDBOX_EXECUTOR_TIMEOUT", "等待远端 Skill 沙箱超时。") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SandboxExecutorError("SANDBOX_PROTOCOL_INVALID", "Skill 沙箱返回的不是合法 JSON。") from exc
        if not isinstance(data, dict):
            raise SandboxExecutorError("SANDBOX_PROTOCOL_INVALID", "Skill 沙箱返回格式错误。")
        return SandboxExecutionResult.from_response(data)


def execute_remote_skill_script(
    *,
    root_dir: Path,
    skill_id: str,
    skill_version: str,
    script_path: Path,
    input_payload: dict[str, Any],
    timeout_seconds: float = 30.0,
    trace_id: str = "",
) -> SandboxExecutionResult | None:
    """Execute through the configured remote runner, or return None in legacy mode."""
    executor = RemoteSandboxExecutor.from_environment()
    if executor is None:
        return None
    try:
        relative_script = script_path.resolve().relative_to(root_dir.resolve()).as_posix()
    except ValueError as exc:
        raise SandboxExecutorError("SANDBOX_SCRIPT_PATH_INVALID", "脚本不属于当前冻结 Skill 包。") from exc
    archive, digest = snapshot_skill_bundle(root_dir)
    return executor.execute(
        SandboxExecutionRequest(
            skill_id=skill_id,
            skill_version=skill_version,
            bundle_archive=archive,
            bundle_digest=digest,
            script_path=relative_script,
            input_payload=input_payload,
            timeout_seconds=timeout_seconds,
            trace_id=trace_id,
        )
    )
