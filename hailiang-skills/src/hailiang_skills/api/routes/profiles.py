from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from hailiang_skills.core.fact_service import FactService, serialize_known_facts


class CreateProfileRequest(BaseModel):
    name: str
    initialize_from_shared_facts: bool = True


class UpdateProfileRequest(BaseModel):
    name: str | None = None
    is_default: bool | None = None


def build_profiles_router(fact_service: FactService, user_metadata_repository=None) -> APIRouter:
    router = APIRouter()

    @router.get("/users/{user_id}/profiles")
    def list_profiles(user_id: str) -> dict:
        return {
            "user_id": user_id,
            "user": user_metadata_repository.get(user_id) if user_metadata_repository else {"user_id": user_id, "display_name": ""},
            "profiles": fact_service.profile_repo.list_profiles(user_id),
        }

    @router.post("/users/{user_id}/profiles")
    def create_profile(user_id: str, request: CreateProfileRequest) -> dict:
        profile = fact_service.profile_repo.create_profile(
            user_id,
            name=request.name,
            shared_facts_initialized=request.initialize_from_shared_facts,
        )
        if request.initialize_from_shared_facts:
            shared_facts = fact_service.get_shared_facts(user_id)
            fact_service.profile_repo.save_profile_facts(
                user_id,
                str(profile["profile_id"]),
                shared_facts.model_copy(deep=True),
            )
        return {
            "user_id": user_id,
            "user": user_metadata_repository.get(user_id) if user_metadata_repository else {"user_id": user_id, "display_name": ""},
            "profile": profile,
            "facts": serialize_known_facts(
                fact_service.profile_repo.get_profile_facts(user_id, str(profile["profile_id"]))
            ),
        }

    @router.get("/users/{user_id}/profiles/{profile_id}")
    def get_profile(user_id: str, profile_id: str) -> dict:
        try:
            profile = fact_service.profile_repo.get_profile(user_id, profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="profile not found") from exc
        facts = fact_service.get_profile_facts(user_id, profile_id)
        return {
            "user_id": user_id,
            "profile": profile,
            "facts": serialize_known_facts(facts),
        }

    @router.patch("/users/{user_id}/profiles/{profile_id}")
    def update_profile(user_id: str, profile_id: str, request: UpdateProfileRequest) -> dict:
        try:
            profile = fact_service.profile_repo.update_profile(
                user_id,
                profile_id,
                name=request.name,
                is_default=request.is_default,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="profile not found") from exc
        return {
            "user_id": user_id,
            "profile": profile,
        }

    return router
