from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hailiang_skills.core.deployment import release_version


def capability_catalog(orchestrator: Any | None = None) -> list[dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    registry = getattr(orchestrator, "runtime_registry", None)
    bundles = getattr(registry, "bundles", {}) if registry is not None else {}
    for bundle in bundles.values():
        for relative_path, path in getattr(bundle, "scripts", {}).items():
            capability_id = f"{bundle.contract.skill_id}:{relative_path}"
            try:
                digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError:
                digest = "missing"
            entries[capability_id] = {"capability_id": capability_id, "content_hash": digest}
    return [entries[key] for key in sorted(entries)]


def kernel_fingerprint(orchestrator: Any | None = None) -> str:
    payload = {
        "release_version": release_version(),
        "capabilities": capability_catalog(orchestrator),
        "runtime_contract_schema": 1,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
