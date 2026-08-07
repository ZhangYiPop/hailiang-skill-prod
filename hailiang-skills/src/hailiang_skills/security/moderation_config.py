from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_MODERATION_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "runtime.yml"


@dataclass(slots=True, frozen=True)
class ModerationPolicyConfig:
    allowed_labels_by_provider: dict[str, frozenset[str]] = field(default_factory=dict)
    source_by_provider: dict[str, str] = field(default_factory=dict)

    def allowed_labels(self, provider: str) -> frozenset[str]:
        return self.allowed_labels_by_provider.get(str(provider or "").strip().lower(), frozenset())

    def source(self, provider: str) -> str:
        return self.source_by_provider.get(str(provider or "").strip().lower(), "default")


def load_moderation_policy_config(path: Path | None = None) -> ModerationPolicyConfig:
    config_path = path or DEFAULT_MODERATION_CONFIG_PATH
    data: dict[str, Any] = {}
    if config_path.is_file():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            moderation = raw.get("security_moderation")
            data = moderation if isinstance(moderation, dict) else {}

    configured = data.get("allowed_labels")
    providers = configured if isinstance(configured, dict) else {}
    allowed = {
        str(provider).strip().lower(): _label_set(labels)
        for provider, labels in providers.items()
        if str(provider).strip()
    }
    sources = {provider: "runtime.yml" for provider in allowed}
    if "HAILIANG_SECURITY_ALLOWED_LABELS" in os.environ:
        allowed["aliyun"] = _label_set(os.environ.get("HAILIANG_SECURITY_ALLOWED_LABELS", "").split(","))
        sources["aliyun"] = "HAILIANG_SECURITY_ALLOWED_LABELS"
    return ModerationPolicyConfig(allowed_labels_by_provider=allowed, source_by_provider=sources)


def _label_set(value: Any) -> frozenset[str]:
    values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    return frozenset(str(item).strip().lower() for item in values if str(item).strip())
