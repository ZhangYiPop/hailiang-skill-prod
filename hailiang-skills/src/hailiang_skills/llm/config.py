from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "llm" / "qwen_dashscope.json"


def _sanitize_base_url(value: str) -> str:
    return value.strip().strip("`").strip().strip('"').strip("'").rstrip("/")


@dataclass
class EmbeddingConfig:
    enabled: bool = False
    base_url: str = ""
    model: str = "text-embedding-v4"
    api_key_env: str = ""
    timeout_s: int = 8
    max_batch_size: int = 10
    cache_enabled: bool = True
    cache_dir: str = ""


@dataclass
class RouteSuggestionConfig:
    monitor_every_turn: bool = False
    general_chat_card_threshold: float = 0.90


@dataclass
class LLMConfig:
    provider: str
    base_url: str
    model: str
    api_key_env: str
    timeout_s: int = 30
    temperature: float = 0.0
    max_tokens: int = 8000
    enable_thinking: bool = False
    return_reasoning: bool = False
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    route_suggestions: RouteSuggestionConfig = field(default_factory=RouteSuggestionConfig)

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


def load_llm_config(path: Path | None = None) -> LLMConfig:
    config_path = path or DEFAULT_CONFIG_PATH
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["base_url"] = _sanitize_base_url(data["base_url"])
    embedding_raw = data.get("embedding", {})
    embedding = embedding_raw if isinstance(embedding_raw, dict) else {}
    data["embedding"] = EmbeddingConfig(
        enabled=bool(embedding.get("enabled", False)),
        base_url=_sanitize_base_url(str(embedding.get("base_url") or data.get("base_url") or "")),
        model=str(embedding.get("model") or "text-embedding-v4").strip() or "text-embedding-v4",
        api_key_env=str(embedding.get("api_key_env") or data.get("api_key_env") or "").strip(),
        timeout_s=max(int(embedding.get("timeout_s", 8) or 8), 1),
        max_batch_size=max(int(embedding.get("max_batch_size", 10) or 10), 1),
        cache_enabled=bool(embedding.get("cache_enabled", True)),
        cache_dir=str(embedding.get("cache_dir") or "").strip(),
    )
    route_suggestions_raw = data.get("route_suggestions", {})
    route_suggestions = route_suggestions_raw if isinstance(route_suggestions_raw, dict) else {}
    data["route_suggestions"] = RouteSuggestionConfig(
        monitor_every_turn=bool(route_suggestions.get("monitor_every_turn", False)),
        general_chat_card_threshold=min(
            max(float(route_suggestions.get("general_chat_card_threshold", 0.90) or 0.90), 0.0),
            1.0,
        ),
    )
    return LLMConfig(**data)
