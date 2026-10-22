"""Shared static validation for untrusted Skill Python scripts."""
from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


BLOCKED_IMPORTS = {"subprocess", "socket", "requests", "httpx", "urllib", "shutil", "http", "docker", "ctypes"}
BLOCKED_CALLS = {"os.system", "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename", "os.popen", "os.execv", "os.execve", "eval", "exec", "__import__"}
PINNED_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9_.,-]+\])?==([^\s;@]+)$")

# This manifest is baked into the isolated Runner image. Adding a package is a
# platform image release, never a user-controlled runtime pip installation.
PLATFORM_DEPENDENCIES: dict[str, dict[str, object]] = json.loads(
    (Path(__file__).with_name("sandbox_dependencies.json")).read_text(encoding="utf-8")
)
MODULE_TO_PACKAGE = {module: package for package, item in PLATFORM_DEPENDENCIES.items() for module in item["modules"]}  # type: ignore[index]
STDLIB_MODULES = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


@dataclass(slots=True)
class ScriptReviewFinding:
    name: str
    status: str
    detail: str
    payload: dict[str, Any] = field(default_factory=dict)

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        del mode
        return {"name": self.name, "status": self.status, "detail": self.detail, "payload": self.payload}


def _error(name: str, detail: str, **payload: Any) -> ScriptReviewFinding:
    return ScriptReviewFinding(name=name, status="error", detail=detail, payload=payload)


def _requirements(path: Path) -> tuple[dict[str, str], list[ScriptReviewFinding]]:
    declared: dict[str, str] = {}
    findings: list[ScriptReviewFinding] = []
    if not path.exists():
        return declared, findings
    for line_no, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PINNED_REQUIREMENT.fullmatch(line)
        if not match:
            findings.append(_error("dependency_declaration_invalid", f"scripts/requirements.txt:{line_no} 依赖必须是平台镜像中的精确版本 package==version。", path="scripts/requirements.txt", line=line_no, declaration=line, support_status="invalid", suggested_action="移除 URL、路径、额外索引或未锁定版本。"))
            continue
        package, version = match.groups()
        package = package.lower().replace("_", "-")
        supported = PLATFORM_DEPENDENCIES.get(package)
        if supported is None:
            findings.append(_error("dependency_not_supported", f"平台镜像不支持依赖 `{package}=={version}`，请使用已支持版本或提交平台依赖申请。", path="scripts/requirements.txt", line=line_no, package=package, declared_version=version, supported_version=None, support_status="unsupported", suggested_action="提交平台依赖申请。"))
        elif version != supported["version"]:
            findings.append(_error("dependency_version_not_supported", f"平台镜像仅支持 `{package}=={supported['version']}`，当前声明为 `{package}=={version}`。", path="scripts/requirements.txt", line=line_no, package=package, declared_version=version, supported_version=supported["version"], support_status="version_mismatch", suggested_action=f"改为 {package}=={supported['version']}。"))
        declared[package] = version
    return declared, findings


def review_scripts(package_root: Path) -> list[ScriptReviewFinding]:
    scripts_dir = Path(package_root) / "scripts"
    if not scripts_dir.exists():
        return [ScriptReviewFinding(name="script_safety_reviewer", status="skipped", detail="no scripts directory found")]
    declared, findings = _requirements(scripts_dir / "requirements.txt")
    for script in sorted(scripts_dir.rglob("*.py")):
        if script.name == "__init__.py":
            continue
        rel = script.relative_to(package_root).as_posix()
        try:
            tree = ast.parse(script.read_text(encoding="utf-8", errors="replace"), filename=rel)
        except SyntaxError as exc:
            findings.append(_error("syntax", f"{rel} 存在语法错误：{exc.msg}", path=rel, line=exc.lineno))
            continue
        for node in ast.walk(tree):
            modules: list[tuple[str, int]] = []
            if isinstance(node, ast.Import):
                modules = [(alias.name, node.lineno) for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [(node.module, node.lineno)]
            for module, line_no in modules:
                root = module.split(".")[0]
                if root in BLOCKED_IMPORTS:
                    findings.append(_error("blocked_import", f"{rel} imports blocked module `{module}`。", path=rel, line=line_no, import_module=module, support_status="blocked", suggested_action="使用平台工具或移除系统、网络与进程控制代码。"))
                elif root not in STDLIB_MODULES:
                    package = MODULE_TO_PACKAGE.get(root)
                    if package is None:
                        findings.append(_error("import_not_supported", f"{rel} 导入 `{root}`，平台镜像无法识别或不支持该依赖。", path=rel, line=line_no, import_module=root, package=None, support_status="unsupported", suggested_action="使用标准库或提交平台依赖申请。"))
                    elif declared.get(package) != PLATFORM_DEPENDENCIES[package]["version"]:
                        findings.append(_error("dependency_not_declared", f"{rel} 使用 `{root}`，请在 scripts/requirements.txt 声明 `{package}=={PLATFORM_DEPENDENCIES[package]['version']}`。", path=rel, line=line_no, import_module=root, package=package, declared_version=declared.get(package), supported_version=PLATFORM_DEPENDENCIES[package]["version"], support_status="declaration_required", suggested_action=f"添加 {package}=={PLATFORM_DEPENDENCIES[package]['version']}。"))
            if isinstance(node, ast.Call):
                call = _call_name(node.func)
                if call in BLOCKED_CALLS:
                    findings.append(_error("blocked_call", f"{rel} 调用了禁止函数 `{call}`。", path=rel, line=node.lineno, call=call, support_status="blocked", suggested_action="移除动态执行或系统操作。"))
    if not findings:
        findings.append(ScriptReviewFinding(name="script_safety_reviewer", status="success", detail="scripts passed static safety and dependency review", payload={"platform_dependencies": PLATFORM_DEPENDENCIES}))
    return findings


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""
