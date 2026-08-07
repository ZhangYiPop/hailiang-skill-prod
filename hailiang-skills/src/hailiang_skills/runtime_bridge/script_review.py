from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


BLOCKED_IMPORTS = {
    "subprocess",
    "socket",
    "requests",
    "httpx",
    "urllib",
    "shutil",
}
BLOCKED_CALLS = {
    "os.system",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "os.removedirs",
    "os.rename",
}
ALLOWED_REQUIREMENTS = {"", "pandas"}


@dataclass(slots=True)
class ScriptReviewFinding:
    name: str
    status: str
    detail: str
    payload: dict[str, Any] = field(default_factory=dict)

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        del mode
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "payload": self.payload,
        }


def review_scripts(package_root: Path) -> list[ScriptReviewFinding]:
    scripts_dir = Path(package_root) / "scripts"
    findings: list[ScriptReviewFinding] = []
    if not scripts_dir.exists():
        return [
            ScriptReviewFinding(
                name="script_safety_reviewer",
                status="skipped",
                detail="no scripts directory found",
            )
        ]

    requirements = scripts_dir / "requirements.txt"
    if requirements.exists():
        for line in requirements.read_text(encoding="utf-8", errors="replace").splitlines():
            package = line.strip().split("==")[0].split(">=")[0]
            if package not in ALLOWED_REQUIREMENTS:
                findings.append(
                    ScriptReviewFinding(
                        name="requirements",
                        status="error",
                        detail=f"dependency '{package}' is not in the allowlist",
                        payload={"path": "scripts/requirements.txt"},
                    )
                )

    for script in sorted(scripts_dir.glob("*.py")):
        if script.name == "__init__.py":
            continue
        rel = script.relative_to(package_root).as_posix()
        try:
            tree = ast.parse(script.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            findings.append(
                ScriptReviewFinding(
                    name="syntax",
                    status="error",
                    detail=f"{rel} has syntax error: {exc.msg}",
                    payload={"path": rel, "line": exc.lineno},
                )
            )
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in BLOCKED_IMPORTS:
                        findings.append(
                            ScriptReviewFinding(
                                name="blocked_import",
                                status="error",
                                detail=f"{rel} imports blocked module '{alias.name}'",
                                payload={"path": rel},
                            )
                        )
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in BLOCKED_IMPORTS:
                    findings.append(
                        ScriptReviewFinding(
                            name="blocked_import",
                            status="error",
                            detail=f"{rel} imports blocked module '{node.module}'",
                            payload={"path": rel},
                        )
                    )
            elif isinstance(node, ast.Call):
                call = _call_name(node.func)
                if call in BLOCKED_CALLS:
                    findings.append(
                        ScriptReviewFinding(
                            name="blocked_call",
                            status="error",
                            detail=f"{rel} calls blocked function '{call}'",
                            payload={"path": rel},
                        )
                    )

    if not findings:
        findings.append(
            ScriptReviewFinding(
                name="script_safety_reviewer",
                status="success",
                detail="scripts passed static safety review",
            )
        )
    return findings


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""
