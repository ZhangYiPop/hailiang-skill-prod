"""Stable domain objects for Hailiang Expert runtimes.

The v1 runtime only executes one expert.  These objects deliberately do not
encode that limitation so an Expert Bundle and the chat API do not need a
migration when delegation is added later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ExpertMember:
    member_id: str
    role: str
    authorized_skill_ids: tuple[str, ...] = ()
    kind: str = "expert"


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    delegation_id: str
    from_member_id: str
    to_member_id: str
    task: str
    handoff_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DelegationResult:
    delegation_id: str
    member_id: str
    summary: str
    exported_facts: dict[str, Any] = field(default_factory=dict)
    status: str = "completed"


@dataclass(frozen=True, slots=True)
class SkillObservation:
    """Only the data a different Skill is allowed to observe."""

    skill_id: str
    summary: str
    fact_changes: dict[str, Any] = field(default_factory=dict)
    form_request: dict[str, Any] | None = None
    handoff_summary: str = ""
    trace: tuple[dict[str, Any], ...] = ()


class ExpertRuntime(Protocol):
    """Runtime boundary shared by single-expert and future team runtimes."""

    def handle_message(self, user_message: str, context, legacy_handler):
        """Run one expert turn and return the existing SkillResult contract."""
