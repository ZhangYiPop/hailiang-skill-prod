from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hailiang_skills.runtime_bridge.imports import PROJECT_ROOT
from hailiang_skills.core.skill_ids import canonical_skill_id


DEFAULT_RUNTIME_CONFIG_PATH = PROJECT_ROOT / "config" / "runtime.yml"
SKILL_MANAGEMENT_ALIASES = {
    "admission": "mock_admission",
    "convergence": "multi_path_planning",
    "main_planner": "career_plan_entity",
}


@dataclass(slots=True, frozen=True)
class RuntimeBridgeConfig:
    runtime_dir: Path
    soul_path: Path
    memory_enabled: bool = True
    memory_async_update: bool = True
    sandbox_prewarm_enabled: bool = True
    local_fast_path_enabled: bool = True
    active_window_messages: int = 8
    legacy_bridge_skill_ids: frozenset[str] = frozenset()
    skill_enabled_by_id: dict[str, bool] = field(default_factory=dict)
    tool_routing_mode: str = "ms_agent"
    tool_routing_fallback_on_invalid: bool = True
    progress_simulation_enabled: bool = True
    progress_simulation_interval_s: float = 0.45
    progress_simulation_jitter_s: float = 0.25
    progress_simulation_min_duration_s: float = 1.2


def load_runtime_bridge_config(path: Path | None = None) -> RuntimeBridgeConfig:
    config_path = path or DEFAULT_RUNTIME_CONFIG_PATH
    data: dict[str, Any] = {}
    if config_path.is_file():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            data = raw

    skill_management = data.get("skill_management")
    configured_enabled = skill_management.get("enabled") if isinstance(skill_management, dict) else {}
    skill_enabled_by_id: dict[str, bool] = {}
    if isinstance(configured_enabled, dict):
        for raw_skill_id, raw_enabled in configured_enabled.items():
            skill_id = SKILL_MANAGEMENT_ALIASES.get(
                str(raw_skill_id).strip(),
                canonical_skill_id(raw_skill_id),
            )
            if not skill_id:
                continue
            if skill_id != str(raw_skill_id).strip():
                logging.getLogger("hailiang.runtime_config").warning(
                    "skill_management uses legacy alias %s; use %s",
                    raw_skill_id,
                    skill_id,
                )
            if not isinstance(raw_enabled, bool):
                logging.getLogger("hailiang.runtime_config").warning(
                    "skill_management.enabled.%s must be boolean; defaulting to enabled",
                    skill_id,
                )
                continue
            skill_enabled_by_id[skill_id] = raw_enabled

    tool_routing = data.get("tool_routing")
    tool_routing = tool_routing if isinstance(tool_routing, dict) else {}
    progress_simulation = data.get("progress_simulation")
    progress_simulation = progress_simulation if isinstance(progress_simulation, dict) else {}
    tool_routing_mode = str(
        os.getenv("HAILIANG_TOOL_ROUTING_MODE") or tool_routing.get("mode") or "ms_agent"
    ).strip().lower()
    if tool_routing_mode not in {"ms_agent", "standalone"}:
        logging.getLogger("hailiang.runtime_config").warning(
            "tool_routing.mode=%s is invalid; defaulting to ms_agent",
            tool_routing_mode,
        )
        tool_routing_mode = "ms_agent"

    runtime_dir = _resolve_path(
        os.getenv("HAILIANG_RUNTIME_DIR") or data.get("runtime_dir") or "runtime"
    )
    soul_path = _resolve_path(os.getenv("HAILIANG_SOUL_PATH") or data.get("soul_path") or "config/soul.md")
    return RuntimeBridgeConfig(
        runtime_dir=runtime_dir,
        soul_path=soul_path,
        memory_enabled=_read_bool(
            os.getenv("HAILIANG_MEMORY_ENABLED"),
            data.get("memory_enabled"),
            default=True,
        ),
        memory_async_update=_read_bool(
            os.getenv("HAILIANG_MEMORY_ASYNC_UPDATE"),
            data.get("memory_async_update"),
            default=True,
        ),
        sandbox_prewarm_enabled=_read_bool(
            os.getenv("HAILIANG_SANDBOX_PREWARM_ENABLED"),
            data.get("sandbox_prewarm_enabled"),
            default=True,
        ),
        local_fast_path_enabled=_read_bool(
            os.getenv("HAILIANG_MS_AGENT_LOCAL_FAST_PATH"),
            data.get("local_fast_path_enabled"),
            default=True,
        ),
        active_window_messages=_clamp_active_window(data.get("active_window_messages")),
        legacy_bridge_skill_ids=frozenset(
            item.strip()
            for item in str(
                os.getenv("HAILIANG_LEGACY_BRIDGE_SKILLS")
                or ",".join(str(item) for item in (data.get("legacy_bridge_skill_ids") or []))
            ).split(",")
            if item.strip()
        ),
        skill_enabled_by_id=skill_enabled_by_id,
        tool_routing_mode=tool_routing_mode,
        tool_routing_fallback_on_invalid=_read_bool(
            os.getenv("HAILIANG_TOOL_ROUTING_FALLBACK_ON_INVALID"),
            tool_routing.get("fallback_on_invalid"),
            default=True,
        ),
        progress_simulation_enabled=_read_bool(
            os.getenv("HAILIANG_PROGRESS_SIMULATION_ENABLED"),
            progress_simulation.get("enabled"),
            default=True,
        ),
        progress_simulation_interval_s=_read_float(
            os.getenv("HAILIANG_PROGRESS_SIMULATION_INTERVAL_S"),
            progress_simulation.get("interval_s"),
            default=0.45,
            minimum=0.1,
            maximum=5.0,
        ),
        progress_simulation_jitter_s=_read_float(
            os.getenv("HAILIANG_PROGRESS_SIMULATION_JITTER_S"),
            progress_simulation.get("jitter_s"),
            default=0.25,
            minimum=0.0,
            maximum=2.0,
        ),
        progress_simulation_min_duration_s=_read_float(
            os.getenv("HAILIANG_PROGRESS_SIMULATION_MIN_DURATION_S"),
            progress_simulation.get("min_duration_s"),
            default=1.2,
            minimum=0.0,
            maximum=10.0,
        ),
    )


def _resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (PROJECT_ROOT / path).resolve()


def _read_bool(env_value: str | None, config_value: Any, *, default: bool) -> bool:
    value = env_value if env_value is not None else config_value
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _read_float(
    env_value: str | None,
    config_value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = env_value if env_value is not None else config_value
    try:
        parsed = float(value if value is not None else default)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _clamp_active_window(value: Any) -> int:
    try:
        parsed = int(value if value is not None else 8)
    except (TypeError, ValueError):
        parsed = 8
    return min(20, max(1, parsed))
