from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProvinceFlow(BaseModel):
    flow_type: str
    provinces: list[str] = Field(default_factory=list)
    notes: str = ""


class ProvinceScoreBand(BaseModel):
    province: str
    region_variant: str | None = None
    exam_mode: str = ""
    subject_group: str | None = None
    tier_name: str = ""
    min_score: int | None = None
    max_score: int | None = None
    sample_schools: list[str] = Field(default_factory=list)
    recommended_paths: list[str] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class TierCopywriting(BaseModel):
    tier_name: str
    intro: str
