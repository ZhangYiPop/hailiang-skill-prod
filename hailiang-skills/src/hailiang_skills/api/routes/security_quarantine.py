from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from hailiang_skills.security.quarantine_store import QuarantineStore, QuarantineStoreError


class ReviewUpdateRequest(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=128)
    review_status: str
    note: str | None = Field(default=None, max_length=2000)


class PayloadDeleteRequest(BaseModel):
    reviewer_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)


def build_security_quarantine_router(store: QuarantineStore) -> APIRouter:
    router = APIRouter(prefix="/security/quarantine", tags=["security"])

    def authorize(token: str | None, authorization: str | None = None) -> str:
        expected = os.getenv("HAILIANG_SECURITY_ADMIN_TOKEN", "")
        bearer = None
        if authorization and authorization.lower().startswith("bearer "):
            bearer = authorization[7:].strip()
        candidate = token or bearer
        if not expected or not candidate or not hmac.compare_digest(candidate, expected):
            raise HTTPException(status_code=403, detail="security administrator access required")
        return token

    @router.get("")
    def list_quarantine_cases(
        x_security_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
        status: str | None = Query(default=None),
        risk_level: str | None = Query(default=None),
        stage: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict:
        authorize(x_security_admin_token, authorization)
        return {"cases": store.list_cases(status=status, risk_level=risk_level, stage=stage, limit=limit)}

    @router.get("/{case_id}")
    def get_quarantine_case(
        case_id: str,
        reviewer_id: str = Query(min_length=1, max_length=128),
        x_security_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict:
        authorize(x_security_admin_token, authorization)
        try:
            store.record_case_view(case_id, reviewer_id=reviewer_id)
            return store.get_case(case_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="security case not found") from exc

    @router.get("/{case_id}/payload")
    def get_quarantine_payload(
        case_id: str,
        reviewer_id: str = Query(min_length=1, max_length=128),
        x_security_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict:
        authorize(x_security_admin_token, authorization)
        try:
            return {"case_id": case_id, "payload": store.read_payload(case_id, reviewer_id=reviewer_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="security payload not found") from exc
        except QuarantineStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/{case_id}/payload/export")
    def export_quarantine_payload(
        case_id: str,
        reviewer_id: str = Query(min_length=1, max_length=128),
        x_security_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict:
        authorize(x_security_admin_token, authorization)
        try:
            return {"case_id": case_id, "payload": store.export_payload(case_id, reviewer_id=reviewer_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="security payload not found") from exc
        except QuarantineStoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.patch("/{case_id}/review")
    def update_quarantine_review(
        case_id: str,
        request: ReviewUpdateRequest,
        x_security_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict:
        authorize(x_security_admin_token, authorization)
        try:
            return store.update_review(
                case_id,
                reviewer_id=request.reviewer_id,
                review_status=request.review_status,
                note=request.note,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="security case not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.delete("/{case_id}/payload")
    def delete_quarantine_payload(
        case_id: str,
        request: PayloadDeleteRequest,
        x_security_admin_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> dict:
        authorize(x_security_admin_token, authorization)
        try:
            return store.delete_payload(
                case_id,
                reviewer_id=request.reviewer_id,
                reason=request.reason,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="security case not found") from exc

    return router
