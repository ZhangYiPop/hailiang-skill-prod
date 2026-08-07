from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MessageBlock(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AssistantMessageEnvelope(BaseModel):
    message_id: str
    role: str = "assistant"
    content: str = ""
    blocks: list[MessageBlock] = Field(default_factory=list)
