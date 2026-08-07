from __future__ import annotations

from hailiang_skills.skill_runtime.models import RouteDecision, SessionState, SkillBundle


def choose_route(
    current_bundle: SkillBundle,
    state: SessionState,
    *,
    available_bundles: dict[str, SkillBundle] | None = None,
) -> RouteDecision:
    available_bundles = available_bundles or {}
    requested_scene = str(state.status_flags.get("pending_route_scene") or "").strip()
    if not requested_scene:
        return RouteDecision()
    for route in current_bundle.contract.routes:
        if route.scene != requested_scene:
            continue
        target_bundle = available_bundles.get(route.target_skill_id)
        if target_bundle and requested_scene not in target_bundle.contract.accepts_scenes:
            return RouteDecision(
                should_route=False,
                scene=requested_scene,
                target_skill_id=route.target_skill_id,
                reason="目标 skill 未声明接收该场景。",
            )
        missing_global = tuple(
            item for item in route.required_global_facts if not str(state.global_facts.get(item, "")).strip()
        )
        current_skill_id = state.active_skill_id or current_bundle.contract.skill_id
        skill_facts = state.skill_facts.get(current_skill_id, {})
        missing_skill = tuple(
            item for item in route.required_skill_facts if not str(skill_facts.get(item, "")).strip()
        )
        return RouteDecision(
            should_route=not missing_global and not missing_skill,
            target_skill_id=route.target_skill_id,
            scene=requested_scene,
            reason="场景与双向声明匹配。" if not missing_global and not missing_skill else "缺少前置 facts。",
            missing_global_facts=missing_global,
            missing_skill_facts=missing_skill,
        )
    return RouteDecision(
        should_route=False,
        scene=requested_scene,
        reason="当前 skill 未声明该路由场景。",
    )
