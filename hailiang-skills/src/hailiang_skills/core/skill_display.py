from __future__ import annotations

import re
from typing import Any

from hailiang_skills.core.skill_ids import (
    CAREER_PLAN_SKILL_ID,
    EXPERT_DIRECT_EXECUTION_ID,
    GENERAL_CHAT_SKILL_ID,
    LEGACY_MAIN_PLANNER_SKILL_ID,
    canonical_skill_id,
)
from hailiang_skills.skill_runtime.models import SkillCatalogEntry


LEGACY_SKILL_SCENES = {
    "admission": "模拟升学",
    "mock_admission": "模拟升学",
    "convergence": "多元路径规划",
    "multi_path_planning": "多元路径规划",
    "junior_multi_path_planning": "初中多元路径规划",
    "chat": "自由问答",
    CAREER_PLAN_SKILL_ID: "升学规划顾问",
    GENERAL_CHAT_SKILL_ID: "自由问答",
    LEGACY_MAIN_PLANNER_SKILL_ID: "升学规划顾问",
}

NON_SELECTABLE_SKILL_IDS = {GENERAL_CHAT_SKILL_ID, LEGACY_MAIN_PLANNER_SKILL_ID}

def resolve_active_skill_id(context, fallback: str | None = None) -> str:
    skill_states = context.skill_states or {}
    runtime_state = skill_states.get("skill_runtime", {}) if isinstance(skill_states, dict) else {}
    planner_state = skill_states.get("planner", {}) if isinstance(skill_states, dict) else {}
    router_state = skill_states.get("router", {}) if isinstance(skill_states, dict) else {}
    main_state = skill_states.get(CAREER_PLAN_SKILL_ID, {}) if isinstance(skill_states, dict) else {}
    if not main_state and isinstance(skill_states, dict):
        main_state = skill_states.get(LEGACY_MAIN_PLANNER_SKILL_ID, {})
    active_skill = (
        (runtime_state or {}).get("active_skill_id")
        or (planner_state or {}).get("target_skill")
        or (router_state or {}).get("target_skill")
        or (main_state or {}).get("target_skill")
        or (context.interaction_state or {}).get("active_skill")
        or fallback
        or ""
    )
    return canonical_skill_id(active_skill)


def build_skill_display(context, active_skill: str | None = None, runtime_registry=None) -> dict[str, str]:
    skill_id = canonical_skill_id(active_skill or resolve_active_skill_id(context))
    if skill_id == EXPERT_DIRECT_EXECUTION_ID:
        return _build_expert_direct_display(context)

    main_state = (context.skill_states or {}).get(CAREER_PLAN_SKILL_ID, {})
    if not main_state:
        main_state = (context.skill_states or {}).get(LEGACY_MAIN_PLANNER_SKILL_ID, {})
    intent_route = main_state.get("intent_route", {}) if isinstance(main_state, dict) else {}
    route_target = str(intent_route.get("target_skill_id") or "") if isinstance(intent_route, dict) else ""
    route_scene = str(intent_route.get("scene_name") or "") if isinstance(intent_route, dict) else ""

    bundle = runtime_registry.get(skill_id) if runtime_registry is not None and skill_id else None
    runtime_meta = getattr(bundle, "runtime_metadata", None)
    routing = getattr(runtime_meta, "routing", None)

    scene_name = ""
    if route_scene and (not route_target or route_target == skill_id or _legacy_target_matches(route_target, skill_id)):
        scene_name = route_scene
    if not scene_name and routing is not None:
        scene_name = str(getattr(routing, "scene_name", "") or "")
    if not scene_name and runtime_meta is not None:
        accepts_scenes = tuple(getattr(runtime_meta, "accepts_scenes", ()) or ())
        scene_name = accepts_scenes[0] if accepts_scenes else ""
    if not scene_name:
        scene_name = LEGACY_SKILL_SCENES.get(skill_id, "")

    skill_name = ""
    if runtime_meta is not None:
        skill_name = str(getattr(runtime_meta, "name", "") or "").strip()
    if not skill_name:
        skill_name = skill_id

    # Metadata is user-facing runtime data. Keep all three descriptive fields
    # populated even when an imported Skill only declares one of them.
    metadata_description = (
        str(getattr(runtime_meta, "description", "") or "").strip()
        if runtime_meta
        else ""
    )
    metadata_brief = str(getattr(runtime_meta, "brief", "") or "").strip() if runtime_meta else ""
    metadata_info = str(getattr(runtime_meta, "info", "") or "").strip() if runtime_meta else ""
    fallback = f"进入{skill_name}后，我会结合你的情况提供针对性的咨询与规划建议。"
    brief = metadata_brief or metadata_info or metadata_description or fallback
    info = metadata_info or metadata_description or brief
    description = metadata_description or info

    agent_label = _agent_label_for_skill(
        skill_id=skill_id,
        skill_name=skill_name,
        scene_name=scene_name,
        runtime_registry=runtime_registry,
    )
    theme_key = _theme_key(skill_id or scene_name or CAREER_PLAN_SKILL_ID)
    return {
        "skill_id": skill_id,
        "skill_name": skill_name,
        "description": description,
        "brief": brief,
        "info": info,
        "active_skill_label": skill_name,
        "agent_label": agent_label,
        "scene_name": scene_name,
        "skill_theme": theme_key,
        "theme_key": theme_key,
    }


def _build_expert_direct_display(context) -> dict[str, str]:
    """Build user-facing state for a direct reply from an Expert Agent.

    ``expert_direct`` is intentionally not a registered Skill, so it has no
    runtime metadata.  Its display identity must instead come from the active
    expert recorded by the Agent runtime.
    """
    skill_states = getattr(context, "skill_states", {}) or {}
    agent_runtime = skill_states.get("agent_runtime", {}) if isinstance(skill_states, dict) else {}
    agent_runtime = agent_runtime if isinstance(agent_runtime, dict) else {}
    expert_id = str(
        agent_runtime.get("expert_id")
        or agent_runtime.get("active_expert_id")
        or ""
    ).strip()
    expert_name = str(agent_runtime.get("expert_name") or "").strip() or expert_id or "专家"
    team_id = str(agent_runtime.get("expert_team_id") or "").strip()
    description = (
        f"由专家团成员 {expert_name} 基于当前对话和专家规则直接回答。"
        if team_id
        else f"由 {expert_name} 基于当前对话和专家规则直接回答。"
    )
    return {
        "skill_id": EXPERT_DIRECT_EXECUTION_ID,
        "skill_name": expert_name,
        "description": description,
        "brief": description,
        "info": description,
        "active_skill_label": expert_name,
        "agent_label": expert_name,
        "scene_name": "",
        "skill_theme": "expert-direct",
        "theme_key": "expert-direct",
    }


def build_skill_catalog(
    runtime_registry,
    *,
    include_fallback: bool = False,
    grade: str = "",
) -> list[dict[str, str]]:
    """Build the user-facing list of skills that can be entered explicitly.

    ``grade`` is retained as a backward-compatible argument.  Toolbar entry
    is no longer filtered by school stage.
    """
    del grade
    if runtime_registry is None:
        return []
    items: list[dict[str, str]] = []
    for skill_id, bundle in _iter_runtime_bundles(runtime_registry):
        skill_id = canonical_skill_id(skill_id)
        if not skill_id or skill_id in NON_SELECTABLE_SKILL_IDS or (not include_fallback and skill_id == GENERAL_CHAT_SKILL_ID):
            continue
        display = build_skill_display(_DisplayContext(), active_skill=skill_id, runtime_registry=runtime_registry)
        items.append(
            {
                "skill_id": skill_id,
                "label": display["agent_label"],
                "description": display.get("description", ""),
                "brief": display.get("brief", ""),
                "info": display.get("info", ""),
                "scene_name": display["scene_name"],
                "skill_theme": display["skill_theme"],
            }
        )
    return sorted(items, key=lambda item: (item["label"], item["skill_id"]))


def build_runtime_skill_catalog(runtime_registry) -> tuple[SkillCatalogEntry, ...]:
    """Return enabled child/specialist Skills for the runtime prompt catalog."""
    if runtime_registry is None:
        return ()

    entries: list[SkillCatalogEntry] = []
    seen_skill_ids: set[str] = set()
    for raw_skill_id, bundle in _iter_runtime_bundles(runtime_registry):
        skill_id = canonical_skill_id(raw_skill_id)
        if not skill_id or skill_id in seen_skill_ids or skill_id in NON_SELECTABLE_SKILL_IDS:
            continue
        runtime_meta = getattr(bundle, "runtime_metadata", None)
        role = str(
            getattr(runtime_meta, "entrypoint_role", "")
            or getattr(getattr(bundle, "contract", None), "skill_role", "")
            or "child"
        ).strip()
        if role not in {"child", "specialist"}:
            continue

        routing = getattr(runtime_meta, "routing", None)
        display = build_skill_display(
            _DisplayContext(),
            active_skill=skill_id,
            runtime_registry=runtime_registry,
        )
        seen_skill_ids.add(skill_id)
        entries.append(
            SkillCatalogEntry(
                skill_id=skill_id,
                name=display["agent_label"],
                brief=display.get("brief", ""),
                description=display.get("description", ""),
                scene_name=str(getattr(routing, "scene_name", "") or ""),
                routing_examples=tuple(getattr(routing, "routing_examples", ()) or ()),
                school_stage_scope=str(getattr(routing, "school_stage_scope", "") or ""),
            )
        )
    return tuple(sorted(entries, key=lambda item: (item.name, item.skill_id)))


class _DisplayContext:
    skill_states: dict = {}
    interaction_state: dict = {}


def _legacy_target_matches(route_target: str, skill_id: str) -> bool:
    pairs = {
        ("mock_admission", "admission"),
        ("multi_path_planning", "convergence"),
    }
    return (route_target, skill_id) in pairs


def _theme_key(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z_-]+", "-", value.strip().lower()).strip("-")
    return normalized or "career-plan-entity"


def _agent_label_for_skill(
    *,
    skill_id: str,
    skill_name: str,
    scene_name: str,
    runtime_registry,
) -> str:
    label = _preferred_chinese_label(skill_id=skill_id, skill_name=skill_name, scene_name=scene_name)
    if not label:
        return skill_id
    return label


def _preferred_chinese_label(*, skill_id: str, skill_name: str, scene_name: str) -> str:
    # User-visible Skill names and descriptions are authored by SKILL.md.
    # Scene names remain routing vocabulary and are only a compatibility
    # fallback for legacy packages without a declared display name.
    for value in (skill_name, scene_name, LEGACY_SKILL_SCENES.get(skill_id, "")):
        text = str(value or "").strip()
        if text and _has_chinese(text):
            return text
    for value in (skill_name, skill_id):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _iter_runtime_bundles(runtime_registry):
    enabled_bundles = getattr(runtime_registry, "enabled_bundles", None)
    if callable(enabled_bundles):
        yield from enabled_bundles().items()
        return
    bundles = getattr(runtime_registry, "bundles", None)
    if isinstance(bundles, dict):
        for skill_id, bundle in bundles.items():
            if str(skill_id) == LEGACY_MAIN_PLANNER_SKILL_ID:
                continue
            yield skill_id, bundle
        return
    bundles = getattr(runtime_registry, "_bundles", None)
    if isinstance(bundles, dict):
        for skill_id, bundle in bundles.items():
            if str(skill_id) == LEGACY_MAIN_PLANNER_SKILL_ID:
                continue
            yield skill_id, bundle
        return
    names = getattr(runtime_registry, "names", None)
    getter = getattr(runtime_registry, "get", None)
    if callable(names) and callable(getter):
        for name in names():
            yield name, getter(name)


def _has_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))
