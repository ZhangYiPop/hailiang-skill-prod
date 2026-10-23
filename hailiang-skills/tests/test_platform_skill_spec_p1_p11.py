"""Regression coverage for skill规范 v1.1 §11 platform renovations (P1–P11)."""

from __future__ import annotations

from hailiang_skills.core.skill_duty_registry import clear_skill_duty_registry_cache, registered_school_stages
from hailiang_skills.core.skill_lifecycle import _visible_skill_catalog
from hailiang_skills.runtime_bridge.main_planner import PROJECT_RUNTIME_SKILLS_ROOT
from hailiang_skills.skill_runtime.models import SessionState
from hailiang_skills.skill_runtime.route_router import choose_route
from hailiang_skills.skill_runtime.session import build_prompt_assembly
from hailiang_skills.skill_runtime.skill_contract import field_uses, parse_skill_contract
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry
from hailiang_skills.skill_runtime.tools import (
    REQUEST_DECLARED_FORM_TOOL_NAME,
    RETURN_TO_MAIN_ROUTER_TOOL_NAME,
    build_tool_specs,
)


class _Ctx:
    known_facts = None
    skill_states: dict = {"skill_runtime": {"global_facts": {"grade": "高一"}}}
    messages: list = []


def test_p1_catalog_keeps_full_description() -> None:
    registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
    catalog = _visible_skill_catalog(_Ctx(), runtime_registry=registry)
    subject = next(item for item in catalog if item["skill_id"] == "subject_advisor")
    assert len(subject["description"]) > 120
    assert "【做什么】" in subject["description"] or "做什么" in subject["description"]


def test_p2_route_ignores_accepts_scenes_mismatch() -> None:
    registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
    source = registry.get("career_plan_entity") or registry.get("main_planner")
    assert source is not None
    # Build a route target that would previously fail accepts_scenes checks.
    from dataclasses import replace
    from hailiang_skills.skill_runtime.models import RouteTarget

    fake_route = RouteTarget(scene="虚构场景", target_skill_id="subject_advisor")
    bundle = replace(source, contract=replace(source.contract, routes=(fake_route,)))
    state = SessionState(
        session_id="s_route",
        active_skill_id=bundle.contract.skill_id,
        status_flags={"pending_route_scene": "虚构场景"},
        global_facts={"grade": "高一"},
    )
    decision = choose_route(bundle, state, available_bundles=registry.bundles)
    assert decision.should_route is True
    assert decision.target_skill_id == "subject_advisor"


def test_p3_duty_registry_filters_interest_for_senior() -> None:
    clear_skill_duty_registry_cache()
    stages = registered_school_stages("interest_explore")
    assert stages is not None
    assert "senior" not in stages
    assert "primary" in stages


def test_p4_p5_prompt_defaults() -> None:
    registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
    bundle = registry.get("subject_advisor")
    assert bundle is not None
    assert bundle.runtime_metadata.prompt_loading.strategy == "progressive"
    assert bundle.runtime_metadata.prompt_loading.include_route_targets is False
    state = SessionState(session_id="s_prompt", active_skill_id="subject_advisor")
    assembly = build_prompt_assembly(bundle, state)
    assert "# Skill Route Targets" not in assembly.core_prompt


def test_p7_parses_new_runtime_contract_fields() -> None:
    contract = parse_skill_contract(
        {
            "skill_id": "demo",
            "fields": {"uses": ["grade", "target_major"], "outputs": ["selected_subjects"]},
            "stages": [{"id": "collect", "required_fields": ["grade"]}],
            "routes": [{"target_skill_id": "subject_advisor", "pass_fields": ["grade"]}],
        },
        fallback_name="demo",
    )
    assert contract.facts_schema.uses == ("grade", "target_major")
    assert contract.facts_schema.outputs == ("selected_subjects",)
    assert contract.stages[0].required_fields == ("grade",)
    assert contract.routes[0].pass_fields == ("grade",)
    assert field_uses(contract) == ("grade", "target_major")


def test_p9_unified_fields_include_new_keys() -> None:
    from hailiang_skills.core.facts_config import load_facts_config

    facts = load_facts_config()
    keys = set(facts.keys()) if isinstance(facts, dict) else set(getattr(facts, "keys", lambda: [])())
    if not keys and hasattr(facts, "facts"):
        keys = set(facts.facts.keys())
    # facts_config API varies; fall back to raw yaml via schema file presence.
    from pathlib import Path
    import yaml

    payload = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "facts_schema.yml").read_text(encoding="utf-8")
    )
    fact_keys = set((payload or {}).get("facts", {}).keys())
    assert {"selected_subjects", "target_major", "talent_direction", "user_role", "school_stage"} <= fact_keys


def test_p10_p11_tool_dependency_intersection() -> None:
    registry = load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)
    bundle = registry.get("subject_advisor")
    assert bundle is not None
    assert REQUEST_DECLARED_FORM_TOOL_NAME in bundle.runtime_metadata.tool_dependency
    specs = {item.name: item for item in build_tool_specs(bundle, SessionState(session_id="s_tools"))}
    assert specs[REQUEST_DECLARED_FORM_TOOL_NAME].enabled is True
    assert specs[RETURN_TO_MAIN_ROUTER_TOOL_NAME].enabled is True
    # Not declared → disabled when tool_dependency is present.
    if "web_search" not in bundle.runtime_metadata.tool_dependency:
        assert specs["web_search"].enabled is False
