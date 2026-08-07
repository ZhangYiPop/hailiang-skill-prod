from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hailiang_skills.core.logging import make_event
from hailiang_skills.core.routing_config import is_skill_skip_stability_check

_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "loop_prevention.yml"

_loop_defense_config: dict[str, Any] | None = None


def load_loop_prevention_config(force_reload: bool = False) -> dict[str, Any]:
    global _loop_defense_config
    if _loop_defense_config is None or force_reload:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                _loop_defense_config = yaml.safe_load(f) or {}
        else:
            _loop_defense_config = {}
    return _loop_defense_config


class LoopDefense:
    def __init__(self) -> None:
        self.config = load_loop_prevention_config().get("loop_prevention", {})

    def stabilize_target_skill(
        self,
        context,
        requested_skill: str,
        *,
        confidence: float,
    ) -> tuple[str, list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        current_skill = context.interaction_state.get("active_skill")
        if not current_skill or current_skill == requested_skill:
            return requested_skill, events

        if is_skill_skip_stability_check(requested_skill):
            return requested_skill, events

        skill_cfg = self.config.get("skill_stability", {})
        if not skill_cfg.get("enabled", True):
            return requested_skill, events

        min_rounds = skill_cfg.get("min_consecutive_rounds", 2)
        threshold = self._get_confidence_threshold(requested_skill)
        selection_history = context.interaction_state.get("skill_selection_history", [])
        consecutive = self._count_consecutive(selection_history, requested_skill)

        if confidence < threshold and consecutive + 1 < min_rounds:
            events.append(
                make_event(
                    "loop_defense_triggered",
                    {
                        "rule": "skill_stability",
                        "requested_skill": requested_skill,
                        "fallback_skill": current_skill,
                        "confidence": confidence,
                        "threshold": threshold,
                        "consecutive_hits": consecutive + 1,
                    },
                )
            )
            return current_skill, events

        return requested_skill, events

    def record_skill_execution(self, context, skill_name: str) -> None:
        history = context.interaction_state.setdefault("skill_selection_history", [])
        history.append(skill_name)
        if len(history) > 12:
            del history[:-12]

    def record_asked_facts(self, context, missing_facts: list[str]) -> None:
        if not missing_facts:
            return
        fact_cfg = self.config.get("fact_ask_once", {})
        if not fact_cfg.get("enabled", True):
            return
        asked_facts = context.interaction_state.setdefault("asked_facts", [])
        allow_repeat = set(fact_cfg.get("allow_repeat_ask", []))
        for fact in missing_facts:
            if fact in allow_repeat:
                continue
            if fact not in asked_facts:
                asked_facts.append(fact)

    def can_switch_scenario(
        self,
        context,
        current_scenario: str,
        target_scenario: str,
    ) -> tuple[bool, str]:
        switch_cfg = self.config.get("scenario_switch_lock", {})
        if not switch_cfg.get("enabled", True):
            return True, ""

        if self._is_high_priority_exit(current_scenario, target_scenario):
            return True, "high_priority_exit"

        lock_key = f"scenario_switch_lock:{current_scenario}:{target_scenario}"
        switch_count = context.interaction_state.get(lock_key, 0)
        max_switches = switch_cfg.get("max_switches_per_round", 3)
        if switch_count >= max_switches:
            return False, f"scenario switch limit reached: {current_scenario}->{target_scenario}"
        return True, ""

    def record_scenario_switch(self, context, current_scenario: str, target_scenario: str) -> None:
        lock_key = f"scenario_switch_lock:{current_scenario}:{target_scenario}"
        context.interaction_state[lock_key] = context.interaction_state.get(lock_key, 0) + 1

    def _get_confidence_threshold(self, skill_name: str) -> float:
        confidence_cfg = self.config.get("confidence_threshold", {})
        per_skill = confidence_cfg.get("per_skill_threshold", {})
        return per_skill.get(skill_name, confidence_cfg.get("default_threshold", 0.75))

    def _count_consecutive(self, history: list[str], skill_name: str) -> int:
        count = 0
        for item in reversed(history):
            if item != skill_name:
                break
            count += 1
        return count

    def _is_high_priority_exit(self, current_scenario: str, target_scenario: str) -> bool:
        switch_cfg = self.config.get("scenario_switch_lock", {})
        for item in switch_cfg.get("high_priority_exits", []):
            if (
                item.get("from_scenario") == current_scenario
                and item.get("to_scenario") == target_scenario
            ):
                return True
        return False
