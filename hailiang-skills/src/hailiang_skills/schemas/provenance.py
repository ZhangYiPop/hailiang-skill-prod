from __future__ import annotations

from pydantic import BaseModel


class Provenance(BaseModel):
    source_type: str = "skill"
    source_id: str | None = None
    source_label: str | None = None
    turn_id: str | None = None
    file: str | None = None
    sheet: str | None = None
    record_id: str | None = None
    variant_id: str | None = None
