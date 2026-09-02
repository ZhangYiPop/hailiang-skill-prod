from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class VersionedConfigurationRegistry:
    """Read-only resolver keyed by exact object and release identifiers.

    The registry stores declarative configuration snapshots.  Executable
    capabilities continue to come from the shared platform runtime.
    """

    releases: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def install_snapshot(self, snapshot: dict[str, Any]) -> None:
        for entry in snapshot.get("entries", snapshot.get("objects", [])):
            object_id = str(entry.get("object_id") or "")
            release_id = str(entry.get("release_id") or "")
            if object_id and release_id:
                self.releases[(object_id, release_id)] = dict(entry)

    def get(self, object_id: str, release_id: str) -> dict[str, Any] | None:
        value = self.releases.get((str(object_id), str(release_id)))
        return dict(value) if value is not None else None

    def require(self, object_id: str, release_id: str) -> dict[str, Any]:
        value = self.get(object_id, release_id)
        if value is None:
            raise KeyError(f"configuration release not installed: {object_id}@{release_id}")
        return value
