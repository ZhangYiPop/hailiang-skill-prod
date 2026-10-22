"""The dedicated, internal-only Skill script execution service."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4
from zipfile import ZipFile
from io import BytesIO

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from hailiang_skills.runtime_bridge.script_review import review_scripts


MAX_ARCHIVE_BYTES = int(os.getenv("HAILIANG_SANDBOX_MAX_ARCHIVE_BYTES", str(32 * 1024 * 1024)))
MAX_EXTRACTED_BYTES = int(os.getenv("HAILIANG_SANDBOX_MAX_EXTRACTED_BYTES", str(64 * 1024 * 1024)))
MAX_OUTPUT_BYTES = int(os.getenv("HAILIANG_SANDBOX_MAX_OUTPUT_BYTES", str(1024 * 1024)))
MAX_TIMEOUT_SECONDS = float(os.getenv("HAILIANG_SANDBOX_MAX_TIMEOUT_SECONDS", "60"))
WORK_ROOT = Path(os.getenv("HAILIANG_SANDBOX_WORK_DIR", "/sandbox/work"))
RUNNER_IMAGE_DIGEST = os.getenv("HAILIANG_SANDBOX_IMAGE_DIGEST", "unknown")
TOKEN = os.getenv("HAILIANG_SANDBOX_SHARED_TOKEN", "")
MAX_CONCURRENT_EXECUTIONS = max(1, int(os.getenv("HAILIANG_SANDBOX_MAX_CONCURRENT_EXECUTIONS", "2")))
QUEUE_TIMEOUT_SECONDS = max(0.1, float(os.getenv("HAILIANG_SANDBOX_QUEUE_TIMEOUT_SECONDS", "5")))
EXECUTION_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_EXECUTIONS)


class ExecutionRequest(BaseModel):
    skill_id: str = Field(min_length=1, max_length=160)
    skill_version: str = Field(default="", max_length=200)
    bundle_archive: str = Field(min_length=1)
    bundle_digest: str = Field(min_length=64, max_length=64)
    script_path: str = Field(min_length=1, max_length=512)
    input: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30.0, gt=0, le=MAX_TIMEOUT_SECONDS)
    trace_id: str = Field(default="", max_length=256)


app = FastAPI(title="Hailiang Skill Sandbox Runner", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/executions")
def execute(request: ExecutionRequest, x_hailiang_sandbox_token: str = Header(default="")) -> dict[str, Any]:
    _require_token(x_hailiang_sandbox_token)
    started = time.perf_counter()
    execution_id = f"sandbox_{uuid4().hex}"
    environment_key = ""
    cache_hit = False
    workspace: Path | None = None
    if not EXECUTION_SEMAPHORE.acquire(timeout=QUEUE_TIMEOUT_SECONDS):
        return {
            "ok": False, "exit_code": None, "stdout": "", "stderr": "", "json_output": None,
            "error_code": "SANDBOX_CAPACITY_EXHAUSTED", "error": "Skill 沙箱当前繁忙，请稍后重试。",
            "execution_id": execution_id, "environment_key": "", "cache_hit": False,
            "duration_ms": int((time.perf_counter() - started) * 1000), "executor_type": "remote_runner",
        }
    try:
        archive = _decode_archive(request.bundle_archive, request.bundle_digest)
        environment_key = _environment_key(request.bundle_digest)
        # Dependencies are immutable contents of the runner image.
        cache_hit = True
        workspace = Path(tempfile.mkdtemp(prefix=f"{execution_id}-", dir=_ensure_dir(WORK_ROOT)))
        package_root = workspace / "package"
        _safe_extract(archive, package_root)
        script = _resolve_script(package_root, request.script_path)
        _validate_package(package_root)
        result = _run_script(Path(sys.executable), script, package_root, request.input, request.timeout_seconds)
        result.update(
            execution_id=execution_id,
            environment_key=environment_key,
            cache_hit=cache_hit,
            duration_ms=int((time.perf_counter() - started) * 1000),
            executor_type="remote_runner",
        )
        return result
    except SandboxFailure as exc:
        return {
            "ok": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "json_output": None,
            "error_code": exc.code,
            "error": exc.detail,
            "execution_id": execution_id,
            "environment_key": environment_key,
            "cache_hit": cache_hit,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "executor_type": "remote_runner",
        }
    finally:
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
        EXECUTION_SEMAPHORE.release()


class SandboxFailure(Exception):
    def __init__(self, code: str, detail: str) -> None:
        self.code, self.detail = code, detail


def _require_token(received: str) -> None:
    if not TOKEN or not hmac.compare_digest(received, TOKEN):
        raise HTTPException(status_code=401, detail="sandbox authentication failed")


def _decode_archive(encoded: str, expected_digest: str) -> bytes:
    if len(encoded) > (MAX_ARCHIVE_BYTES * 4 // 3) + 8:
        raise SandboxFailure("SANDBOX_BUNDLE_TOO_LARGE", "冻结 Skill 包超过沙箱大小限制。")
    try:
        archive = base64.b64decode(encoded, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SandboxFailure("SANDBOX_BUNDLE_INVALID", "冻结 Skill 包不是合法 base64。") from exc
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise SandboxFailure("SANDBOX_BUNDLE_TOO_LARGE", "冻结 Skill 包超过沙箱大小限制。")
    if not hmac.compare_digest(hashlib.sha256(archive).hexdigest(), expected_digest):
        raise SandboxFailure("SANDBOX_BUNDLE_INTEGRITY_FAILED", "冻结 Skill 包摘要校验失败。")
    return archive


def _safe_extract(archive: bytes, destination: Path) -> None:
    try:
        with ZipFile(BytesIO(archive)) as zip_file:
            total_size = 0
            for item in zip_file.infolist():
                relative = PurePosixPath(item.filename)
                if relative.is_absolute() or ".." in relative.parts or item.is_dir():
                    if item.is_dir():
                        continue
                    raise SandboxFailure("SANDBOX_BUNDLE_PATH_INVALID", "冻结 Skill 包包含非法路径。")
                total_size += item.file_size
                if total_size > MAX_EXTRACTED_BYTES:
                    raise SandboxFailure("SANDBOX_BUNDLE_TOO_LARGE", "冻结 Skill 包解压后超过沙箱大小限制。")
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zip_file.read(item))
    except SandboxFailure:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SandboxFailure("SANDBOX_BUNDLE_INVALID", "冻结 Skill 包无法解压。") from exc


def _resolve_script(root: Path, relative_path: str) -> Path:
    relative = PurePosixPath(relative_path)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("scripts",) or relative.suffix != ".py":
        raise SandboxFailure("SANDBOX_SCRIPT_PATH_INVALID", "只允许执行当前 Skill 包 scripts/ 下的 Python 脚本。")
    path = root.joinpath(*relative.parts)
    if not path.is_file():
        raise SandboxFailure("SANDBOX_SCRIPT_NOT_FOUND", "当前冻结 Skill 包中不存在请求的脚本。")
    return path


def _validate_package(package_root: Path) -> None:
    """Repeat the shared publish validation at the execution boundary."""
    findings = [item for item in review_scripts(package_root) if item.status == "error"]
    if not findings:
        return
    first = findings[0]
    code = "SANDBOX_REQUIREMENTS_INVALID" if first.name.startswith("dependency") or first.name == "import_not_supported" else "SANDBOX_SCRIPT_REJECTED"
    raise SandboxFailure(code, first.detail)


def _environment_key(bundle_digest: str) -> str:
    raw = f"{RUNNER_IMAGE_DIGEST}|{sys.version_info.major}.{sys.version_info.minor}|{bundle_digest}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _run_script(python: Path, script: Path, package_root: Path, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    try:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(package_root)
        completed = subprocess.run(
            [str(python), str(script)], input=json.dumps(payload, ensure_ascii=False), text=True,
            capture_output=True, timeout=min(timeout_seconds, MAX_TIMEOUT_SECONDS), cwd=str(script.parent), check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": None, "stdout": "", "stderr": "", "json_output": None, "error_code": "SANDBOX_SCRIPT_TIMEOUT", "error": "Skill 脚本执行超时。"}
    stdout, stderr = completed.stdout[:MAX_OUTPUT_BYTES], completed.stderr[:MAX_OUTPUT_BYTES]
    if len(completed.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES or len(completed.stderr.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return {"ok": False, "exit_code": completed.returncode, "stdout": stdout, "stderr": stderr, "json_output": None, "error_code": "SANDBOX_OUTPUT_TOO_LARGE", "error": "Skill 脚本输出超过限制。"}
    try:
        json_output = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        json_output = None
    if completed.returncode != 0:
        return {"ok": False, "exit_code": completed.returncode, "stdout": stdout, "stderr": stderr, "json_output": json_output, "error_code": "SANDBOX_SCRIPT_FAILED", "error": "Skill 脚本以非零状态退出。"}
    if json_output is None:
        return {"ok": False, "exit_code": completed.returncode, "stdout": stdout, "stderr": stderr, "json_output": None, "error_code": "SANDBOX_OUTPUT_INVALID", "error": "Skill 脚本 stdout 必须是单个 JSON。"}
    return {"ok": True, "exit_code": completed.returncode, "stdout": stdout, "stderr": stderr, "json_output": json_output, "error_code": "", "error": ""}


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
