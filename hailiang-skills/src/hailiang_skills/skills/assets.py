from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def load_json(relative_path: str, default: Any) -> Any:
    path = PROJECT_ROOT / relative_path
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_text(relative_path: str, default: str = "") -> str:
    path = PROJECT_ROOT / relative_path
    if not path.exists():
        return default
    return path.read_text(encoding="utf-8")


def feature_flag_enabled(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}
