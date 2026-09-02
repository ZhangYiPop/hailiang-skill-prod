"""Default expert-team initialization for new sessions and child branches."""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

import yaml


_RUNTIME_CONFIG = Path(__file__).resolve().parents[3] / "config" / "runtime.yml"


@lru_cache(maxsize=1)
def default_expert_team_id() -> str:
    env_value = os.getenv("HAILIANG_DEFAULT_EXPERT_TEAM_ID", "").strip()
    configured = ""
    if _RUNTIME_CONFIG.is_file():
        raw = yaml.safe_load(_RUNTIME_CONFIG.read_text(encoding="utf-8")) or {}
        section = raw.get("expert_runtime") if isinstance(raw, dict) else None
        if isinstance(section, dict):
            configured = str(section.get("default_team_id") or "").strip()
    return env_value or configured or "student_growth_expert_team"


def require_default_expert_team(orchestrator, team_id: str | None = None):
    team_id = str(team_id or default_expert_team_id())
    teams = getattr(orchestrator, "expert_team_registry", None)
    experts = getattr(orchestrator, "expert_registry", None)
    team = teams.get(team_id) if teams is not None else None
    if team is None:
        raise RuntimeError(f"default expert team does not exist: {team_id}")
    coordinator = experts.get(team.coordinator_expert_id) if experts is not None else None
    if coordinator is None or team.coordinator_expert_id not in team.member_expert_ids:
        raise RuntimeError(f"default expert-team coordinator is invalid: {team.coordinator_expert_id}")
    return team


def initialize_default_expert_team(context, orchestrator, *, force: bool = False) -> bool:
    """Install the coordinator only for a new/empty profile branch."""
    if not force and context.session_meta.get("expert_team_id"):
        return False
    snapshot = context.session_meta.get("configuration_snapshot")
    root = snapshot.get("root", {}) if isinstance(snapshot, dict) else {}
    snapshot_team_id = str(root.get("object_key") or "") if root.get("object_type") == "expert_team" else ""
    team = require_default_expert_team(orchestrator, snapshot_team_id or None)
    context.session_meta["expert_team_id"] = team.team_id
    context.session_meta["active_expert_id"] = team.coordinator_expert_id
    context.session_meta["expert_id"] = team.coordinator_expert_id
    context.session_meta["expert_selection_source"] = "deployment_snapshot" if snapshot_team_id else "default_coordinator"
    context.session_meta.pop("pending_team_handoff", None)
    # A team, not general_chat, is the business entrypoint.  The planner is
    # retained as an internal runtime coordinator only.
    context.interaction_state["active_skill"] = "career_plan_entity"
    runtime_state = context.skill_states.setdefault("skill_runtime", {})
    if isinstance(runtime_state, dict):
        runtime_state["active_skill_id"] = "career_plan_entity"
    return True
