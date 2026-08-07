from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from hailiang_skills.core.skill_analytics import SkillAnalyticsService, parse_analytics_time
from hailiang_skills.core.skill_ids import canonical_skill_id


def build_skill_analytics_router(orchestrator) -> APIRouter:
    router = APIRouter()
    service = SkillAnalyticsService()

    @router.get("/analytics/skills")
    def get_skill_analytics(
        start_time: str | None = Query(default=None),
        end_time: str | None = Query(default=None),
        skill_id: str | None = Query(default=None),
    ) -> dict:
        try:
            start_at = parse_analytics_time(start_time, name="start_time")
            end_at = parse_analytics_time(end_time, name="end_time")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        requested = canonical_skill_id(skill_id)
        if requested:
            registry = getattr(orchestrator, "runtime_registry", None)
            if registry is None or not registry.get(requested):
                raise HTTPException(status_code=422, detail="UNKNOWN_SKILL_ID")
        try:
            return service.query(start_at=start_at, end_at=end_at, skill_id=requested or None)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return router
