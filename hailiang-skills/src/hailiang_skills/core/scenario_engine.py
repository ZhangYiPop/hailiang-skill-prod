from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hailiang_skills.core.logging import make_event

_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "scenarios.yml"

_scenario_config: dict[str, Any] | None = None


def load_scenarios_config(force_reload: bool = False) -> dict[str, Any]:
    global _scenario_config
    if _scenario_config is None or force_reload:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                _scenario_config = yaml.safe_load(f) or {}
        else:
            _scenario_config = {}
    return _scenario_config


class ScenarioEngine:
    def __init__(self) -> None:
        self.config = load_scenarios_config()

    @property
    def scenarios(self) -> dict[str, dict[str, Any]]:
        return self.config.get("scenarios", {})

    def ensure_context_initialized(self, context) -> list[dict[str, Any]]:
        if context.interaction_state.get("current_scenario"):
            return []

        default_scenario = self._get_default_scenario_id()
        scenario_meta = self.scenarios.get(default_scenario, {})
        default_phase = scenario_meta.get("default_phase", "collect_info")

        context.interaction_state["current_scenario"] = default_scenario
        context.interaction_state["current_phase"] = default_phase
        context.interaction_state.setdefault("scenario_history", []).append(default_scenario)
        return [
            make_event(
                "scenario_initialized",
                {
                    "scenario_id": default_scenario,
                    "phase_id": default_phase,
                },
            )
        ]

    def apply_skill(self, context, skill_name: str) -> list[dict[str, Any]]:
        current_scenario = context.interaction_state.get("current_scenario") or self._get_default_scenario_id()
        next_phase = self.resolve_phase_for_skill(current_scenario, skill_name)
        if not next_phase:
            return []

        previous_phase = context.interaction_state.get("current_phase")
        if previous_phase == next_phase:
            return []

        context.interaction_state["current_phase"] = next_phase
        return [
            make_event(
                "phase_transition",
                {
                    "scenario_id": current_scenario,
                    "from_phase": previous_phase,
                    "to_phase": next_phase,
                    "trigger_skill": skill_name,
                },
            )
        ]

    def apply_scenario_switch(self, context, target_scenario: str, reason: str) -> list[dict[str, Any]]:
        current_scenario = context.interaction_state.get("current_scenario") or self._get_default_scenario_id()
        if current_scenario == target_scenario or target_scenario not in self.scenarios:
            return []

        previous_phase = context.interaction_state.get("current_phase")
        next_phase = self.scenarios[target_scenario].get("default_phase", previous_phase)
        context.interaction_state["current_scenario"] = target_scenario
        context.interaction_state["current_phase"] = next_phase
        context.interaction_state.setdefault("scenario_history", []).append(target_scenario)
        return [
            make_event(
                "scenario_switch",
                {
                    "from_scenario": current_scenario,
                    "to_scenario": target_scenario,
                    "from_phase": previous_phase,
                    "to_phase": next_phase,
                    "reason": reason,
                },
            )
        ]

    def resolve_phase_for_skill(self, scenario_id: str, skill_name: str) -> str | None:
        scenario = self.scenarios.get(scenario_id, {})
        phases = scenario.get("phases", [])
        for phase in phases:
            if phase.get("entry_skill") == skill_name:
                return phase.get("id")
            if skill_name in (phase.get("skill_candidates") or []):
                return phase.get("id")
        return None

    def get_scenario_meta(self, scenario_id: str) -> dict[str, Any]:
        return self.scenarios.get(scenario_id, {})

    def _get_default_scenario_id(self) -> str:
        for scenario_id, scenario in self.scenarios.items():
            if scenario.get("status") == "active":
                return scenario_id
        return "admission_simulation"
