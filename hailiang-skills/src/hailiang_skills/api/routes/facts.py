from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from hailiang_skills.core.fact_scope import (
    FACT_SCOPE_PROFILE,
    FACT_SCOPE_SESSION,
    FACT_SCOPE_SHARED,
)
from hailiang_skills.core.fact_prompt_builder import build_fact_form_fields
from hailiang_skills.core.fact_service import FactService
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository


class FactUpdateItem(BaseModel):
    key: str
    value: object | None = None


class FactSourcePayload(BaseModel):
    type: str = "user_form"
    source_id: str | None = None
    source_label: str | None = None
    turn_id: str | None = None


class FactBatchUpsertRequest(BaseModel):
    scope: str = FACT_SCOPE_SHARED
    source: FactSourcePayload = Field(default_factory=FactSourcePayload)
    updates: list[FactUpdateItem] = Field(default_factory=list)


class FactResetRequest(BaseModel):
    scope: str = FACT_SCOPE_SHARED
    source: FactSourcePayload = Field(default_factory=lambda: FactSourcePayload(type="manual_reset"))
    fact_keys: list[str] = Field(default_factory=list)


class FactClearBySourceRequest(BaseModel):
    source: FactSourcePayload = Field(default_factory=FactSourcePayload)


def _load_context(
    repository: InMemorySessionRepository,
    session_id: str,
):
    try:
        return repository.get(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc


def _build_shadow_context(repository: InMemorySessionRepository, user_id: str):
    for item in repository.list():
        if item.user_id == user_id:
            return item
    from hailiang_skills.core.context import SessionContext

    return SessionContext(user_id=user_id)


def build_facts_router(
    repository: InMemorySessionRepository,
    fact_service: FactService,
) -> APIRouter:
    router = APIRouter()

    @router.get("/users/{user_id}/facts")
    def get_user_facts(user_id: str) -> dict:
        return {
            "user_id": user_id,
            **fact_service.get_user_facts_payload(user_id),
            "shared_facts": fact_service.get_shared_facts_payload(user_id)["facts"],
        }

    @router.get("/facts/form-config")
    def get_fact_form_config() -> dict:
        return {
            "fields": build_fact_form_fields(),
        }

    @router.post("/users/{user_id}/facts:batch-upsert")
    def upsert_user_facts(user_id: str, request: FactBatchUpsertRequest) -> dict:
        shadow_context = _build_shadow_context(repository, user_id)
        changes = fact_service.batch_upsert(
            shadow_context,
            [item.model_dump() for item in request.updates],
            scope=FACT_SCOPE_SHARED,
            source=request.source.model_dump(),
        )
        return {
            "user_id": user_id,
            "applied_updates": [item.model_dump() for item in request.updates],
            "fact_changes": changes,
            "current_facts": fact_service.get_user_facts_payload(user_id)["facts"],
            "shared_facts": fact_service.get_shared_facts_payload(user_id)["facts"],
        }

    @router.post("/users/{user_id}/facts:reset")
    def reset_user_facts(user_id: str, request: FactResetRequest) -> dict:
        shadow_context = _build_shadow_context(repository, user_id)
        changes = fact_service.reset(
            shadow_context,
            request.fact_keys,
            scope=FACT_SCOPE_SHARED,
            source=request.source.model_dump(),
        )
        return {
            "user_id": user_id,
            "fact_changes": changes,
            "current_facts": fact_service.get_user_facts_payload(user_id)["facts"],
            "shared_facts": fact_service.get_shared_facts_payload(user_id)["facts"],
        }

    @router.post("/users/{user_id}/facts:clear-by-source")
    def clear_user_facts_by_source(user_id: str, request: FactClearBySourceRequest) -> dict:
        shadow_context = _build_shadow_context(repository, user_id)
        try:
            changes = fact_service.clear_user_facts_by_source(
                shadow_context,
                selector=request.source.model_dump(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = fact_service.get_user_facts_payload(user_id)
        return {
            "user_id": user_id,
            "cleared_source": request.source.model_dump(),
            "fact_changes": changes,
            "current_facts": payload["facts"],
            "sources": payload["sources"],
            "shared_facts": payload["facts"],
        }

    @router.get("/users/{user_id}/profiles/{profile_id}/facts")
    def get_profile_facts(user_id: str, profile_id: str) -> dict:
        try:
            fact_service.profile_repo.get_profile(user_id, profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="profile not found") from exc
        payload = fact_service.get_profile_facts_payload(user_id, profile_id)
        return {
            "user_id": user_id,
            "profile_id": profile_id,
            "facts": payload["facts"],
            "sources": payload["sources"],
        }

    @router.post("/users/{user_id}/profiles/{profile_id}/facts:batch-upsert")
    def upsert_profile_facts(user_id: str, profile_id: str, request: FactBatchUpsertRequest) -> dict:
        shadow_context = _build_shadow_context(repository, user_id)
        shadow_context.profile_id = profile_id
        changes = fact_service.batch_upsert(
            shadow_context,
            [item.model_dump() for item in request.updates],
            scope=FACT_SCOPE_PROFILE,
            source=request.source.model_dump(),
        )
        payload = fact_service.get_profile_facts_payload(user_id, profile_id)
        return {
            "user_id": user_id,
            "profile_id": profile_id,
            "applied_updates": [item.model_dump() for item in request.updates],
            "fact_changes": changes,
            "current_facts": payload["facts"],
            "sources": payload["sources"],
        }

    @router.post("/users/{user_id}/profiles/{profile_id}/facts:reset")
    def reset_profile_facts(user_id: str, profile_id: str, request: FactResetRequest) -> dict:
        shadow_context = _build_shadow_context(repository, user_id)
        shadow_context.profile_id = profile_id
        changes = fact_service.reset(
            shadow_context,
            request.fact_keys,
            scope=FACT_SCOPE_PROFILE,
            source=request.source.model_dump(),
        )
        payload = fact_service.get_profile_facts_payload(user_id, profile_id)
        return {
            "user_id": user_id,
            "profile_id": profile_id,
            "fact_changes": changes,
            "current_facts": payload["facts"],
            "sources": payload["sources"],
        }

    @router.get("/sessions/{session_id}/facts")
    def get_session_facts(session_id: str) -> dict:
        context = _load_context(repository, session_id)
        fact_service.hydrate_context(context)
        return {
            "session_id": session_id,
            **fact_service.get_session_facts_payload(context),
        }

    @router.post("/sessions/{session_id}/facts:batch-upsert")
    def upsert_session_facts(session_id: str, request: FactBatchUpsertRequest) -> dict:
        context = _load_context(repository, session_id)
        changes = fact_service.batch_upsert(
            context,
            [item.model_dump() for item in request.updates],
            scope=FACT_SCOPE_SESSION,
            source=request.source.model_dump(),
        )
        repository.save(context)
        return {
            "session_id": session_id,
            "applied_updates": [item.model_dump() for item in request.updates],
            "fact_changes": changes,
            "current_facts": fact_service.get_session_facts_payload(context),
        }

    @router.post("/sessions/{session_id}/facts:reset")
    def reset_session_facts(session_id: str, request: FactResetRequest) -> dict:
        context = _load_context(repository, session_id)
        changes = fact_service.reset(
            context,
            request.fact_keys,
            scope=FACT_SCOPE_SESSION,
            source=request.source.model_dump(),
        )
        repository.save(context)
        return {
            "session_id": session_id,
            "fact_changes": changes,
            "current_facts": fact_service.get_session_facts_payload(context),
        }

    return router
