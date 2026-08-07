from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class QuestionDefinition(BaseModel):
    question_id: str
    question_text: str
    question_type: str = "single"
    answer_options: list[str] = Field(default_factory=list)
    slot_targets: list[str] = Field(default_factory=list)
    ask_when: str | None = None
    stop_when: str | None = None
    priority: int = 100
    group_id: str | None = None
    depends_on_paths: list[str] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)
