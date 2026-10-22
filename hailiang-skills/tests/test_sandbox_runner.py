from __future__ import annotations

import base64
from pathlib import Path

from hailiang_skills.harness.assets import snapshot_assets
from hailiang_skills import sandbox_runner
from hailiang_skills.sandbox_executor import SandboxExecutionResult
from hailiang_skills.runtime_bridge import ms_agent_adapter
from hailiang_skills.runtime_bridge.ms_agent_adapter import MSAgentRuntimeAdapter
from agent_skill_runtime_core import MSAgentRuntimeProbe


def _bundle(tmp_path: Path, *, requirements: str = "") -> tuple[str, str]:
    skill = tmp_path / "skill"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Demo", encoding="utf-8")
    (scripts / "calculate.py").write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "print(json.dumps({'ok': True, 'value': payload['value'] * 2}))\n",
        encoding="utf-8",
    )
    if requirements:
        (scripts / "requirements.txt").write_text(requirements, encoding="utf-8")
    snapshot = snapshot_assets(skill)
    return str(snapshot["archive"]), str(snapshot["digest"])


def _request(archive: str, digest: str) -> sandbox_runner.ExecutionRequest:
    return sandbox_runner.ExecutionRequest(
        skill_id="demo",
        skill_version="rel_demo",
        bundle_archive=archive,
        bundle_digest=digest,
        script_path="scripts/calculate.py",
        input={"value": 21},
    )


def test_runner_executes_frozen_bundle_and_reuses_dependency_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox_runner, "TOKEN", "test-token")
    monkeypatch.setattr(sandbox_runner, "WORK_ROOT", tmp_path / "work")
    archive, digest = _bundle(tmp_path)

    first = sandbox_runner.execute(_request(archive, digest), "test-token")
    second = sandbox_runner.execute(_request(archive, digest), "test-token")

    assert first["ok"] is True
    assert first["json_output"] == {"ok": True, "value": 42}
    assert first["cache_hit"] is True
    assert second["ok"] is True
    assert second["cache_hit"] is True
    assert first["environment_key"] == second["environment_key"]


def test_runner_rejects_modified_archive_and_unsafe_script_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox_runner, "TOKEN", "test-token")
    monkeypatch.setattr(sandbox_runner, "WORK_ROOT", tmp_path / "work")
    archive, digest = _bundle(tmp_path)
    changed = base64.b64encode(base64.b64decode(archive) + b"changed").decode("ascii")

    invalid = sandbox_runner.execute(_request(changed, digest), "test-token")
    unsafe = sandbox_runner.execute(
        _request(archive, digest).model_copy(update={"script_path": "../calculate.py"}), "test-token"
    )

    assert invalid["error_code"] == "SANDBOX_BUNDLE_INTEGRITY_FAILED"
    assert unsafe["error_code"] == "SANDBOX_SCRIPT_PATH_INVALID"


def test_runner_rejects_uncontrolled_requirements(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox_runner, "TOKEN", "test-token")
    monkeypatch.setattr(sandbox_runner, "WORK_ROOT", tmp_path / "work")
    archive, digest = _bundle(tmp_path, requirements="-e git+https://example.com/untrusted.git")

    result = sandbox_runner.execute(_request(archive, digest), "test-token")

    assert result["ok"] is False
    assert result["error_code"] == "SANDBOX_REQUIREMENTS_INVALID"


def test_runner_rejects_import_not_declared_in_platform_baseline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sandbox_runner, "TOKEN", "test-token")
    monkeypatch.setattr(sandbox_runner, "WORK_ROOT", tmp_path / "work")
    archive, digest = _bundle(tmp_path, requirements="pandas==2.2.3\n")
    # The image package may be absent from this unit-test venv; validation is
    # static, so change the frozen source to a definitely unsupported import.
    skill = tmp_path / "unsupported"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Demo", encoding="utf-8")
    (scripts / "calculate.py").write_text("import unsupported_package\n", encoding="utf-8")
    snapshot = __import__("hailiang_skills.harness.assets", fromlist=["snapshot_assets"]).snapshot_assets(skill)
    result = sandbox_runner.execute(_request(str(snapshot["archive"]), str(snapshot["digest"])), "test-token")
    assert result["error_code"] == "SANDBOX_REQUIREMENTS_INVALID"


def test_legacy_runtime_uses_remote_runner_when_enabled(tmp_path: Path, monkeypatch) -> None:
    skill = tmp_path / "skill"
    scripts = skill / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "calculate.py"
    script.write_text("import json\n", encoding="utf-8")
    monkeypatch.setenv("HAILIANG_SCRIPT_EXECUTOR", "remote")
    monkeypatch.setattr(
        ms_agent_adapter,
        "execute_remote_skill_script",
        lambda **_kwargs: SandboxExecutionResult(
            ok=True, exit_code=0, stdout='{"ok":true}', stderr="", json_output={"ok": True},
            duration_ms=3, execution_id="sandbox_test", environment_key="env_test", cache_hit=True,
        ),
    )
    adapter = MSAgentRuntimeAdapter(
        runtime_dir=tmp_path / "runtime",
        runtime_probe=MSAgentRuntimeProbe(status="available", imports={}),
        local_fast_path_enabled=True,
    )
    try:
        outputs, steps = adapter.execute_scripts_in_sandbox(
            skill_id="demo", skill_dir=skill,
            loaded_scripts=[{"path": "scripts/calculate.py"}], execute_scripts=True,
            script_inputs={"scripts/calculate.py": {"value": 21}}, session_id="session-test",
        )
    finally:
        adapter.close()

    assert outputs[0]["execution_mode"] == "remote_runner"
    assert outputs[0]["dependency_cache_hit"] is True
    assert steps[0].payload["execution_mode"] == "remote_runner"
