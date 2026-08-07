from __future__ import annotations

import json
from pathlib import Path

from hailiang_skills.skill_runtime.models import GeneratedAssetDomain, GeneratedAssetFile

ASSET_REGISTRY_FILE = "asset_registry.json"
TOOL_REGISTRY_FILE = "tool_registry.json"


def default_generated_data_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "assets" / "generated"


def load_generated_asset_domains(data_root: str | Path | None = None) -> dict[str, GeneratedAssetDomain]:
    root = Path(data_root).expanduser().resolve() if data_root else default_generated_data_dir()
    if not root.exists():
        return {}

    domains: dict[str, GeneratedAssetDomain] = {}
    for domain_root in sorted(path for path in root.iterdir() if path.is_dir()):
        files: list[GeneratedAssetFile] = []
        manifest: dict[str, object] = {}
        for file_path in sorted(domain_root.glob("*.json")):
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            relative_path = file_path.relative_to(root).as_posix()
            asset_file = GeneratedAssetFile(
                domain=domain_root.name,
                file_name=file_path.name,
                relative_path=relative_path,
                file_path=file_path,
                payload=payload,
            )
            files.append(asset_file)
            if file_path.name == "asset_manifest.json" and isinstance(payload, dict):
                manifest = payload

        domains[domain_root.name] = GeneratedAssetDomain(
            name=domain_root.name,
            root_dir=domain_root,
            files=tuple(files),
            manifest=manifest,
        )
    return domains


def load_generated_asset_registry(data_root: str | Path | None = None) -> dict[str, object]:
    root = Path(data_root).expanduser().resolve() if data_root else default_generated_data_dir()
    registry_path = root / ASSET_REGISTRY_FILE
    if not registry_path.is_file():
        return {}

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def load_generated_tool_registry(data_root: str | Path | None = None) -> dict[str, object]:
    root = Path(data_root).expanduser().resolve() if data_root else default_generated_data_dir()
    registry_path = root / TOOL_REGISTRY_FILE
    if not registry_path.is_file():
        return {}

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}
