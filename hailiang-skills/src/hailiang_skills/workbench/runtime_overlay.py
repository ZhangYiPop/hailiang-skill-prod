from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from hailiang_skills.core.deployment import state_root


def _safe_path(value: str) -> str:
    path = PurePosixPath(str(value or "").replace("\\", "/"))
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("工作台文件路径不合法")
    return path.as_posix()


def materialize_entry_files(entry: dict[str, Any]) -> dict[str, Any]:
    """Materialize immutable revision files under their content hash."""
    prepared = copy.deepcopy(entry)
    files = prepared.get("files") if isinstance(prepared.get("files"), list) else []
    payload = prepared.get("payload") if isinstance(prepared.get("payload"), dict) else {}
    prompt = str(payload.get("prompt_markdown") or "")
    runtime_contract = payload.get("runtime_contract")
    if not files and not prompt and not isinstance(runtime_contract, dict):
        return prepared
    object_key = str(prepared.get("object_key") or "object")
    content_hash = str(prepared.get("content_hash") or "") or hashlib.sha256(
        repr({"files": files, "prompt": prompt, "runtime_contract": runtime_contract}).encode(),
    ).hexdigest()
    root = state_root() / "workbench_runtime" / content_hash / object_key
    root.mkdir(parents=True, exist_ok=True)
    metadata: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        relative_path = _safe_path(str(item.get("relative_path") or ""))
        encoded = item.get("content_base64")
        target = root.joinpath(*PurePosixPath(relative_path).parts)
        if isinstance(encoded, str):
            content = base64.b64decode(encoded, validate=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != hashlib.sha256(content).hexdigest():
                target.write_bytes(content)
        metadata.append({
            "relative_path": relative_path,
            "media_type": str(item.get("media_type") or "application/octet-stream"),
            "size_bytes": int(item.get("size_bytes") or (target.stat().st_size if target.exists() else 0)),
            "content_hash": str(item.get("content_hash") or ""),
        })
    if prompt:
        (root / "SKILL.md").write_text(prompt, encoding="utf-8")
    if isinstance(runtime_contract, dict):
        # Revision assets intentionally exclude this reserved file, so write
        # the immutable payload contract beside SKILL.md. Native Runtime
        # components (questionnaires, memory and prompt assembly) then read
        # the exact candidate contract rather than the deployed one.
        (root / "runtime_contract.json").write_text(
            json.dumps(runtime_contract, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    prepared["runtime_root"] = str(root)
    prepared["files"] = metadata
    return prepared


def configured_skill_bundle(bundle: Any, entry: dict[str, Any] | None) -> Any:
    if bundle is None or not entry:
        return bundle
    prepared = materialize_entry_files(entry)
    payload = prepared.get("payload") if isinstance(prepared.get("payload"), dict) else {}
    prompt = str(payload.get("prompt_markdown") or "").strip()
    runtime_root_value = str(prepared.get("runtime_root") or "")
    runtime_root = Path(runtime_root_value) if runtime_root_value else None
    if not prompt and not (runtime_root and runtime_root.is_dir()):
        return bundle
    configured = copy.copy(bundle)
    configured.metadata = copy.deepcopy(getattr(bundle, "metadata", {}) or {})
    runtime_contract = payload.get("runtime_contract") if isinstance(payload.get("runtime_contract"), dict) else {}
    if isinstance(runtime_contract.get("questionnaire"), dict):
        configured.metadata["questionnaire"] = copy.deepcopy(runtime_contract["questionnaire"])
    if prompt:
        configured._skill_markdown = prompt
        configured._skill_markdown_loader = None
    if runtime_root and runtime_root.is_dir():
        configured.root_dir = runtime_root
        configured.skill_file = runtime_root / "SKILL.md"
        managed_files = bool(payload.get("_workbench_managed_files"))
        references: dict[str, str] = {} if managed_files else dict(getattr(bundle, "references", {}) or {})
        references_root = runtime_root / "references"
        if references_root.is_dir():
            for path in sorted(item for item in references_root.rglob("*") if item.is_file()):
                try:
                    references[path.relative_to(runtime_root).as_posix()] = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
        configured._references = references
        configured._references_loader = None
        scripts_root = runtime_root / "scripts"
        overlay_scripts = {
            path.relative_to(runtime_root).as_posix(): path
            for path in sorted(scripts_root.rglob("*.py"))
            if path.is_file()
        } if scripts_root.is_dir() else {}
        configured._scripts = overlay_scripts if managed_files else {**(getattr(bundle, "scripts", {}) or {}), **overlay_scripts}
    return configured
