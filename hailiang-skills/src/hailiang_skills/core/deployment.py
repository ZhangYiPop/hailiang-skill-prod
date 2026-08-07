"""Small, dependency-free deployment policy helpers."""

from __future__ import annotations

import os
import socket
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
VALID_ENVIRONMENTS = {"test", "prod"}


def deployment_environment() -> str:
    """Return the declared environment; local development defaults to test."""
    value = os.getenv("HAILIANG_DEPLOY_ENV", "test").strip().lower()
    if value not in VALID_ENVIRONMENTS:
        raise RuntimeError("HAILIANG_DEPLOY_ENV must be exactly test or prod")
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
