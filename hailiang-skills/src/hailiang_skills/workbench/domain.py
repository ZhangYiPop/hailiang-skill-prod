from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ObjectType = Literal["skill", "expert", "expert_team"]


@dataclass(frozen=True, slots=True)
class DependencyLock:
    object_id: str
    object_type: ObjectType
    object_key: str
    release_id: str
    release_no: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class BusinessObject:
    object_id: str
    object_type: ObjectType
    object_key: str
    name: str
    description: str = ""
    archived: bool = False


@dataclass(frozen=True, slots=True)
class ObjectRevision:
    revision_id: str
    object_id: str
    revision_no: int
    content_hash: str
    payload: dict[str, Any]
    dependency_locks: tuple[DependencyLock, ...] = ()
    validation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ObjectRelease:
    release_id: str
    object_id: str
    revision_id: str
    release_no: int
    content_hash: str
    dependency_locks: tuple[DependencyLock, ...] = ()
    archived: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedConfigurationSnapshot:
    root: dict[str, Any]
    entries: tuple[dict[str, Any], ...]
    kernel_fingerprint: str
    snapshot_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    run_id: str
    suite_id: str
    revision_id: str
    status: str
    results: tuple[dict[str, Any], ...] = ()
    manual_result: str | None = None
    manual_notes: str = ""


@dataclass(frozen=True, slots=True)
class PackageManifest:
    schema_version: int
    package_id: str
    root: dict[str, Any]
    objects: tuple[dict[str, Any], ...]
    kernel_fingerprint: str
    package_hash: str = ""


@dataclass(frozen=True, slots=True)
class DeploymentSnapshot:
    deployment_id: str
    environment: str
    root_release_id: str
    package_hash: str
    manifest: dict[str, Any]
    status: str
    previous_deployment_id: str | None = None
