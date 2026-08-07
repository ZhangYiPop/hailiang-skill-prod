from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PathRule(BaseModel):
    path_id: str
    primary_category: str
    sheet_group: str
    rule_text_raw: str = ""
    rule_expr_normalized: str | None = None
    dependent_slots: list[str] = Field(default_factory=list)


class PathDefinition(BaseModel):
    path_id: str
    primary_category: str
    sheet_group: str
    description: str = ""
    features: str = ""
    target_users: str = ""
    process_flow: str = ""
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class PathPresentation(BaseModel):
    path_id: str
    match_reason: str = ""
    mismatch_reason: str = ""
    risk_hint: str = ""
    recommended_visibility: str = ""
    timeline: list[dict[str, Any]] = Field(default_factory=list)
