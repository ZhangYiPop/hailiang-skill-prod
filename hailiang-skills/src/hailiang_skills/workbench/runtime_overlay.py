from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any
from dataclasses import replace

import yaml

from hailiang_skills.core.deployment import state_root


def _safe_path(value: str) -> str:
    path = PurePosixPath(str(value or "").replace("\\", "/"))
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("工作台文件路径不合法")
    return path.as_posix()


def skill_markdown_with_metadata(markdown: str, metadata: dict[str, Any] | None) -> str:
    """Merge Workbench metadata back into SKILL.md frontmatter for export/runtime."""
    text = str(markdown or "")
    merged = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
    body = text
    if text.startswith("---\n"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            original = yaml.safe_load(parts[1]) or {}
            if isinstance(original, dict):
                merged = {**original, **merged}
            body = parts[2].lstrip("\n")
    if not merged:
        return text
    return "---\n" + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False) + "---\n" + body


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
        source_metadata = payload.get("source_metadata") or payload.get("configuration") or {}
        (root / "SKILL.md").write_text(
            skill_markdown_with_metadata(prompt, source_metadata),
            encoding="utf-8",
        )
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
    source_metadata = payload.get("source_metadata") or payload.get("configuration") or {}
    if isinstance(source_metadata, dict):
        configured.metadata.update(copy.deepcopy(source_metadata))
    runtime_contract = payload.get("runtime_contract") if isinstance(payload.get("runtime_contract"), dict) else {}
    if "questionnaire" not in configured.metadata and isinstance(runtime_contract.get("questionnaire"), dict):
        # Read compatibility for old Workbench revisions.
        configured.metadata["questionnaire"] = copy.deepcopy(runtime_contract["questionnaire"])
    if prompt:
        configured._skill_markdown = skill_markdown_with_metadata(prompt, source_metadata)
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
        local_assets: dict[str, str] = {} if managed_files else dict(getattr(bundle, "local_assets", {}) or {})
        assets_root = runtime_root / "assets"
        if assets_root.is_dir():
            for path in sorted(item for item in assets_root.rglob("*") if item.is_file()):
                local_assets[path.relative_to(runtime_root).as_posix()] = path.read_text(
                    encoding="utf-8", errors="replace",
                )
        configured._local_assets = local_assets
        configured._local_assets_loader = None
        asset_config = source_metadata.get("assets") if isinstance(source_metadata, dict) else {}
        local_assets_enabled = not isinstance(asset_config, dict) or asset_config.get("local_enabled") is not False
        runtime_metadata = getattr(configured, "runtime_metadata", None)
        if local_assets and local_assets_enabled and runtime_metadata is not None and hasattr(runtime_metadata, "assets"):
            configured.runtime_metadata = replace(
                runtime_metadata,
                assets=replace(runtime_metadata.assets, local_enabled=True, local_dir="assets"),
            )
        scripts_root = runtime_root / "scripts"
        overlay_scripts = {
            path.relative_to(runtime_root).as_posix(): path
            for path in sorted(scripts_root.rglob("*.py"))
            if path.is_file()
        } if scripts_root.is_dir() else {}
        configured._scripts = overlay_scripts if managed_files else {**(getattr(bundle, "scripts", {}) or {}), **overlay_scripts}
    return configured


def skill_bundle_from_entry(entry: dict[str, Any]) -> Any:
    """Load an entire immutable Skill, without inheriting a filesystem template."""
    from hailiang_skills.skill_runtime.skill_loader import load_skill_bundle_from_directory

    prepared = materialize_entry_files(entry)
    payload = entry.get("payload") or {}
    root = Path(prepared["runtime_root"])
    prompt = str(payload.get("prompt_markdown") or "")
    metadata = copy.deepcopy(payload.get("source_metadata") or payload.get("configuration") or {})
    # Preserve prompt frontmatter, with explicitly saved metadata authoritative.
    if prompt.startswith("---\n"):
        parts = prompt.split("---", 2)
        if len(parts) == 3:
            original = yaml.safe_load(parts[1]) or {}
            metadata = {**(original if isinstance(original, dict) else {}), **metadata}
            prompt = parts[2].lstrip("\n")
    metadata["skill_id"] = str(entry["object_key"])
    has_local_assets = (root / "assets").is_dir() and any(path.is_file() for path in (root / "assets").rglob("*"))
    assets_metadata = metadata.get("assets") if isinstance(metadata.get("assets"), dict) else {}
    if has_local_assets and "local_enabled" not in assets_metadata:
        metadata["assets"] = {**assets_metadata, "local_enabled": True, "local_dir": "assets"}
    questionnaire = (payload.get("runtime_contract") or {}).get("questionnaire")
    if "questionnaire" not in metadata and isinstance(questionnaire, dict):
        # Read compatibility for old Workbench revisions.
        metadata["questionnaire"] = copy.deepcopy(questionnaire)
    (root / "SKILL.md").write_text(
        "---\n" + yaml.safe_dump(metadata, allow_unicode=True) + "---\n" + prompt,
        encoding="utf-8",
    )
    bundle = load_skill_bundle_from_directory(root)
    bundle.contract = replace(bundle.contract, skill_id=str(entry["object_key"]))
    bundle._skill_markdown = (root / "SKILL.md").read_text(encoding="utf-8")
    bundle._skill_markdown_loader = None
    bundle._scripts = {path.relative_to(root).as_posix(): path for path in bundle.scripts.values()}
    return bundle
