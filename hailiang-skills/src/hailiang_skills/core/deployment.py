"""Small, dependency-free deployment policy helpers."""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
VALID_ENVIRONMENTS = {"test", "prod"}
_PARALLEL_ENVIRONMENT = re.compile(r"(?:test|prod)-[a-z0-9][a-z0-9-]*\Z")


def is_valid_deployment_environment(value: str) -> bool:
    """Allow the two primary environments and explicitly named test instances."""
    return value in VALID_ENVIRONMENTS or bool(_PARALLEL_ENVIRONMENT.fullmatch(value))


def deployment_environment() -> str:
    """Return the declared environment; local development defaults to test."""
    value = os.getenv("HAILIANG_DEPLOY_ENV", "test").strip().lower()
    if not is_valid_deployment_environment(value):
        raise RuntimeError("HAILIANG_DEPLOY_ENV must be test, prod, test-<name>, or prod-<name>")
    return value


def release_version() -> str:
    configured = os.getenv("HAILIANG_RELEASE_VERSION", "").strip()
    if configured:
        return configured
    version_file = Path.cwd() / "VERSION"
    if version_file.is_file():
        return version_file.read_text(encoding="utf-8").strip() or "dev"
    return "dev"


def node_name() -> str:
    return os.getenv("HAILIANG_NODE_NAME", socket.gethostname()).strip() or socket.gethostname()


def log_root() -> Path:
    configured = os.getenv("HAILIANG_LOG_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else PROJECT_ROOT / "logs"


def state_root() -> Path:
    configured = os.getenv("HAILIANG_STATE_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else PROJECT_ROOT / "runtime"


def raw_audit_enabled() -> bool:
    return os.getenv("HAILIANG_AUDIT_RAW_CONTENT_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
