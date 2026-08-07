from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from hailiang_skills.skill_runtime.errors import LLMConfigError, MissingAPIKeyError
from hailiang_skills.skill_runtime.models import LLMConfig


def load_llm_config(config_path: str | Path, require_api_key: bool = True) -> LLMConfig:
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise LLMConfigError(f"llm_config.json 不存在: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise LLMConfigError(f"llm_config.json 不是合法 JSON: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise LLMConfigError("llm_config.json 顶层必须是 JSON 对象")

    required_fields = [
        "provider",
        "base_url",
        "model",
        "api_key_env",
        "timeout_s",
        "temperature",
        "max_tokens",
    ]
    missing = [field for field in required_fields if field not in data]
    if missing:
        joined = ", ".join(missing)
        raise LLMConfigError(f"llm_config.json 缺少字段: {joined}")

    provider = _read_non_empty_string(data, "provider")
    base_url = _read_base_url(data)
    model = _read_non_empty_string(data, "model")
    api_key_env = _read_non_empty_string(data, "api_key_env")
    timeout_s = _read_positive_int(data, "timeout_s")
    temperature = _read_temperature(data)
    max_tokens = _read_positive_int(data, "max_tokens")

    api_key = os.getenv(api_key_env, "").strip()
    if require_api_key and not api_key:
        raise MissingAPIKeyError(f"缺少环境变量 {api_key_env}，无法调用模型接口")

    return LLMConfig(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        api_key=api_key,
        timeout_s=timeout_s,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _read_non_empty_string(data: dict[str, object], field: str) -> str:
    value = data[field]
    if not isinstance(value, str):
        raise LLMConfigError(f"llm_config.json 字段 {field} 必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise LLMConfigError(f"llm_config.json 字段 {field} 不能为空")
    return normalized


def _read_base_url(data: dict[str, object]) -> str:
    value = _read_non_empty_string(data, "base_url").rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LLMConfigError("llm_config.json 字段 base_url 必须是合法的 http/https URL")
    return value


def _read_positive_int(data: dict[str, object], field: str) -> int:
    value = data[field]
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError(f"llm_config.json 字段 {field} 必须是正整数") from exc
    if parsed <= 0:
        raise LLMConfigError(f"llm_config.json 字段 {field} 必须是正整数")
    return parsed


def _read_temperature(data: dict[str, object]) -> float:
    value = data["temperature"]
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise LLMConfigError("llm_config.json 字段 temperature 必须是数字") from exc
    if not 0 <= parsed <= 2:
        raise LLMConfigError("llm_config.json 字段 temperature 必须在 0 到 2 之间")
    return parsed
