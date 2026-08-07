from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from hailiang_skills.core.loop_defense import LoopDefense
from hailiang_skills.core.logging import make_event
from hailiang_skills.core.registry import SkillRegistry
from hailiang_skills.core.scenario_engine import ScenarioEngine
from hailiang_skills.core.session_logging import append_session_events
from hailiang_skills.core.status_labels import normalize_status_label
from hailiang_skills.skills.base import SkillResult


class Orchestrator:
    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry
        self.scenario_engine = ScenarioEngine()
        self.loop_defense = LoopDefense()

    def _record_events(self, context, events: list[dict]) -> None:
        if not events:
            return
        context.event_trace.extend(events)
        append_session_events(context.session_id, events)

    def _record_prompt_assembly_from_dict(self, context, record: dict) -> None:
        record["timestamp"] = datetime.now(timezone.utc).isoformat()
        event = make_event("prompt_assembly", record)
        self._record_events(context, [event])

    def _emit_runtime_status(self, context, stage: str, label: str) -> None:
        callback = (context.session_meta or {}).get("status_callback")
        display_label = normalize_status_label(label)
        if callable(callback) and display_label:
            callback({"stage": stage, "label": display_label})

    def handle_message(self, user_message: str, context) -> SkillResult:
        turn_id = f"turn_{uuid4().hex[:12]}"
        context.session_meta["active_turn_id"] = turn_id
        user_metadata = {}
        if context.session_meta.pop("hide_next_user_message", False):
            user_metadata = {"hidden": True, "message_type": "skill_transition_command"}
        context.add_message("user", user_message, metadata=user_metadata)
        self._record_events(context, self.scenario_engine.ensure_context_initialized(context))

        self._emit_runtime_status(context, "intent", "意图判断")
        router = self.registry.get("router")
        route_result = router.run(user_message, context)
        if hasattr(router, "get_prompt_for_llm") and router.get_prompt_for_llm():
            self._record_prompt_assembly_from_dict(context, router.get_prompt_for_llm())
        self._record_events(context, route_result.events)
        if route_result.state_patch:
            context.skill_states.setdefault("router", {}).update(route_result.state_patch)
            for key, value in route_result.state_patch.get("extracted_facts", {}).items():
                if value not in (None, "", [], {}):
                    context.update_fact(
                        key,
                        value,
                        source_skill="router",
                        confidence=0.7,
                        source_turn_id=turn_id,
                    )
            requested_scenario = route_result.state_patch.get("target_scenario")
            current_scenario = context.interaction_state.get("current_scenario")
            if requested_scenario and requested_scenario != current_scenario:
                scenario_meta = self.scenario_engine.get_scenario_meta(requested_scenario)
                if scenario_meta.get("status") != "active":
                    self._record_events(
                        context,
                        [
                            make_event(
                                "scenario_switch_skipped",
                                {
                                    "from_scenario": current_scenario,
                                    "to_scenario": requested_scenario,
                                    "reason": "target scenario is not active",
                                },
                            )
                        ],
                    )
                else:
                    can_switch, block_reason = self.loop_defense.can_switch_scenario(
                        context,
                        current_scenario,
                        requested_scenario,
                    )
                    if can_switch:
                        self.loop_defense.record_scenario_switch(
                            context,
                            current_scenario,
                            requested_scenario,
                        )
                        self._record_events(
                            context,
                            self.scenario_engine.apply_scenario_switch(
                                context,
                                requested_scenario,
                                route_result.state_patch.get("reason", "router_requested"),
                            ),
                        )
                    else:
                        self._record_events(
                            context,
                            [
                                make_event(
                                    "loop_defense_triggered",
                                    {
                                        "rule": "scenario_switch_lock",
                                        "from_scenario": current_scenario,
                                        "to_scenario": requested_scenario,
                                        "reason": block_reason,
                                    },
                                )
                            ],
                        )

        facts_extractor = self.registry.get("facts_extractor")
        facts_result = facts_extractor.run(user_message, context)
        if hasattr(facts_extractor, "get_prompt_for_llm") and facts_extractor.get_prompt_for_llm():
            self._record_prompt_assembly_from_dict(context, facts_extractor.get_prompt_for_llm())
        self._record_events(context, facts_result.events)
        if facts_result.state_patch:
            context.skill_states.setdefault("facts_extractor", {}).update(
                facts_result.state_patch
            )
            for key, value in facts_result.state_patch.get("fact_updates", {}).items():
                if value not in (None, "", [], {}):
                    context.update_fact(
                        key,
                        value,
                        source_skill="facts_extractor",
                        confidence=facts_result.state_patch.get("confidence", 0.8),
                        source_turn_id=turn_id,
                    )

        self._emit_runtime_status(context, "planner", "推理规划")
        planner = self.registry.get("planner")
        planner_result = planner.run(user_message, context)
        if hasattr(planner, "get_prompt_for_llm") and planner.get_prompt_for_llm():
            self._record_prompt_assembly_from_dict(context, planner.get_prompt_for_llm())
        self._record_events(context, planner_result.events)
        if planner_result.state_patch:
            context.skill_states["planner"] = dict(planner_result.state_patch)
            if planner_result.state_patch.get("response_mode") == "ask_followup":
                self.loop_defense.record_asked_facts(
                    context,
                    planner_result.state_patch.get("missing_facts", []) or [],
                )

        target_skill = planner_result.next_skill or route_result.next_skill or route_result.state_patch.get(
            "target_skill", "chat"
        )
        target_skill, defense_events = self.loop_defense.stabilize_target_skill(
            context,
            target_skill,
            confidence=route_result.state_patch.get("confidence", 0.0),
        )
        self._record_events(context, defense_events)
        self._record_events(context, self.scenario_engine.apply_skill(context, target_skill))
        self._emit_runtime_status(context, "response", "正在生成回复")
        skill = self.registry.get(target_skill)
        result = skill.run(user_message, context)
        if hasattr(skill, "get_prompt_for_llm") and skill.get_prompt_for_llm():
            self._record_prompt_assembly_from_dict(context, skill.get_prompt_for_llm())

        context.add_message("assistant", result.assistant_message)
        context.interaction_state["active_skill"] = skill.skill_name
        self.loop_defense.record_skill_execution(context, skill.skill_name)
        if skill.skill_name != "terminate_or_recommend":
            context.interaction_state["last_non_terminal_skill"] = skill.skill_name
        self._record_events(context, result.events)
        self._record_events(
            context,
            [
                make_event(
                    "assistant_response",
                    {
                        "active_skill": skill.skill_name,
                        "current_scenario": context.interaction_state.get("current_scenario"),
                        "current_phase": context.interaction_state.get("current_phase"),
                        "message_preview": result.assistant_message[:200],
                    },
                )
            ],
        )

        for key, record in result.updated_facts.items():
            context.known_facts.facts[key] = record

        if result.state_patch:
            context.skill_states.setdefault(skill.skill_name, {}).update(
                result.state_patch
            )

        if result.candidate_paths is not None:
            context.candidate_paths = result.candidate_paths

        if result.risk_alerts:
            context.risk_signals = result.risk_alerts

        return result
