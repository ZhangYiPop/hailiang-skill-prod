from __future__ import annotations

from hailiang_skills.skill_runtime.models import RouteDecision, SessionState, SkillBundle


def choose_route(
    current_bundle: SkillBundle,
    state: SessionState,
    *,
    available_bundles: dict[str, SkillBundle] | None = None,
) -> RouteDecision:
    del available_bundles  # P2: target accepts_scenes is no longer consulted.
    requested_scene = str(state.status_flags.get("pending_route_scene") or "").strip()
    if not requested_scene:
        return RouteDecision()
    for route in current_bundle.contract.routes:
        # Transitional: routes may still declare a scene alias; also accept target_skill_id.
        aliases = {
            str(route.scene or "").strip(),
            str(route.target_skill_id or "").strip(),
        }
        aliases.discard("")
        if requested_scene not in aliases:
            continue
        missing_global = tuple(
            item for item in route.required_global_facts if not str(state.global_facts.get(item, "")).strip()
        )
        current_skill_id = state.active_skill_id or current_bundle.contract.skill_id
        skill_facts = state.skill_facts.get(current_skill_id, {})
        missing_skill = tuple(
            item for item in route.required_skill_facts if not str(skill_facts.get(item, "")).strip()
        )
        pass_fields = tuple(getattr(route, "pass_fields", ()) or ())
        pass_missing = tuple(
            item
            for item in pass_fields
            if not str(state.global_facts.get(item, "")).strip()
            and not str(skill_facts.get(item, "")).strip()
        )
        blocked = bool(missing_global or missing_skill or pass_missing)
        return RouteDecision(
            should_route=not blocked,
            target_skill_id=route.target_skill_id,
            scene=requested_scene,
            reason="按 target_skill_id 交接。" if not blocked else "缺少前置 facts。",
            missing_global_facts=missing_global + pass_missing,
            missing_skill_facts=missing_skill,
        )
    return RouteDecision(
        should_route=False,
        scene=requested_scene,
        reason="当前 skill 未声明该路由目标。",
    )
