from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hailiang_skills.api.session_lifecycle import ContextData, open_or_resume_session
from hailiang_skills.core.concurrency import CapacityExceededError, TurnCoordinator
from hailiang_skills.core.fact_service import FactService
from hailiang_skills.core.sse_protocol import SSE_V2_PROTOCOL
from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID, GENERAL_CHAT_SKILL_ID, canonical_skill_id
from hailiang_skills.core.streaming_runner import StreamingRunner, expert_context_payload, format_sse_event
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository
from hailiang_skills.storage.repositories.postgres_repo import SessionVersionConflict
from hailiang_skills.core.rate_limit import LLMRateLimitError, LLMRateLimiter
from hailiang_skills.core.logging import make_event
from hailiang_skills.core.message_interactions import ACTIVE, SELECTED, ensure_message_interactions, expire_active_interactions, update_interaction
from hailiang_skills.core.session_logging import append_session_events
from hailiang_skills.core.team_handoff_confirmation import active_handoff_decision, block_text_handoff_confirmation


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpertContextInput(StrictInput):
    # These three keys are an intentionally fixed client contract.  ``null``
    # is meaningful (no team / no expert), whereas a missing key means the
    # client did not send a complete view of its current expert state.
    expert_team_id: str | None = Field(..., min_length=1)
    expert_id: str | None = Field(..., min_length=1)
    expected_branch_version: int | None = Field(default=None, ge=0)
    expected_selection_version: int | None = Field(default=None, ge=0)
    operation: Literal["continue", "select_team", "select_team_member", "select_expert", "clear_expert"] = Field(...)


class ProfileBoundInput(StrictInput):
    context_scope: Literal["profile", "unbound"] | None = None
    # Every non-stop action can activate the child selected by outer
    # context_data. Omitting this field remains equivalent to auto for older
    # clients.
    context_activation: Literal["auto", "strict"] = "auto"
    expert_context: ExpertContextInput


class ChatInput(ProfileBoundInput):
    action: Literal["chat"]
    content: str = Field(min_length=1)
    source: Literal["chat", "toolbar"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class EnterSkillInput(ProfileBoundInput):
    action: Literal["enter_skill"]
    target_skill_id: str = Field(min_length=1)
    source: Literal["toolbar", "route_suggestion"]
    source_message_id: str | None = None
    source_interaction_id: str | None = None
    enable_thinking: bool = False
    return_reasoning: bool = False


class QuitSkillInput(ProfileBoundInput):
    action: Literal["quit_skill"]
    target_skill_id: str = Field(min_length=1)
    source: Literal["toolbar", "exit_button"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class ConfirmTeamHandoffInput(ProfileBoundInput):
    action: Literal["confirm_team_handoff"]
    source_message_id: str = Field(min_length=1)
    target_expert_id: str = Field(min_length=1)
    source: Literal["team_handoff"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class SwitchTeamMemberInput(ProfileBoundInput):
    action: Literal["switch_team_member"]
    target_expert_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source: Literal["toolbar"]
    enable_thinking: bool = False
    return_reasoning: bool = False


class OpenSessionInput(ProfileBoundInput):
    """Reserved wire contract for a future model-generated opening."""

    action: Literal["open_session"]
    opening_mode: Literal["model"]


class StopInput(StrictInput):
    action: Literal["stop"]
    source: Literal["composer"]


StreamInput = Annotated[ChatInput | EnterSkillInput | QuitSkillInput | ConfirmTeamHandoffInput | SwitchTeamMemberInput | OpenSessionInput | StopInput, Field(discriminator="action")]


class ChatStreamRequest(StrictInput):
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    input: str = Field(min_length=1)
    context_data: ContextData | None = None
    debug_session_id: str | None = Field(default=None, min_length=1)


def _parse_input(raw: str) -> StreamInput:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="INVALID_INPUT_JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="input must be a JSON object")
    try:
        action = payload.get("action")
        if action != "stop":
            if "profile_id" in payload:
                raise HTTPException(status_code=422, detail="INPUT_PROFILE_ID_FORBIDDEN")
            if "expert_team_id" in payload or "expert_id" in payload:
                raise HTTPException(status_code=422, detail="LEGACY_EXPERT_FIELDS_FORBIDDEN")
            if "expert_context" not in payload:
                raise HTTPException(status_code=422, detail="EXPERT_CONTEXT_REQUIRED")
            expert_context = payload.get("expert_context")
            if not isinstance(expert_context, dict) or {
                "expert_team_id", "expert_id", "operation",
            } - set(expert_context):
                raise HTTPException(status_code=422, detail="EXPERT_CONTEXT_FIELDS_REQUIRED")
        if action == "chat":
            result = ChatInput.model_validate(payload)
            if result.source == "toolbar" and result.expert_context.operation not in {
                "select_team",
                "select_expert",
                "select_team_member",
                "clear_expert",
            }:
                raise HTTPException(status_code=422, detail="toolbar chat requires an explicit expert operation")
            return result
        if action == "enter_skill":
            result = EnterSkillInput.model_validate(payload)
            if result.source == "route_suggestion" and not (
                result.source_message_id and result.source_interaction_id
            ):
                raise HTTPException(
                    status_code=422,
                    detail="route_suggestion requires source_message_id and source_interaction_id",
                )
            return result
        if action == "quit_skill":
            return QuitSkillInput.model_validate(payload)
        if action == "confirm_team_handoff":
            return ConfirmTeamHandoffInput.model_validate(payload)
        if action == "switch_team_member":
            return SwitchTeamMemberInput.model_validate(payload)
        if action == "open_session":
            return OpenSessionInput.model_validate(payload)
        if action == "stop":
            return StopInput.model_validate(payload)
        raise HTTPException(status_code=422, detail="unsupported action")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc


def _resolve_context_scope(input_data: ProfileBoundInput, data: ContextData) -> tuple[str, str | None]:
    """Resolve one turn's context solely from the BFF-selected context data."""
    context_profile_id = str(data.profile_id or "").strip()
    scope = input_data.context_scope or ("profile" if context_profile_id else "unbound")
    if scope == "unbound":
        if any((data.profile_id, data.student_name, data.school_year, data.grade)) or data.facts:
            raise HTTPException(status_code=422, detail="UNBOUND_CONTEXT_MUST_NOT_INCLUDE_PROFILE_DATA")
        return scope, None
    if not context_profile_id:
        raise HTTPException(status_code=422, detail="PROFILE_ID_REQUIRED")
    if not data.profile_id or not data.student_name:
        raise HTTPException(status_code=422, detail="PROFILE_CONTEXT_REQUIRED")
    return scope, context_profile_id


def _expert_context_error(code: str, message: str, *, status_code: int = 422, details: dict | None = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": details or {}},
    )


def _assert_expert_context_current(context, input_data: ProfileBoundInput, *, require_exact_identity: bool) -> dict:
    """Validate an echoed branch state before any expert action mutates it."""
    expected = input_data.expert_context
    actual = expert_context_payload(context)
    expected_selection_version = input_data.expert_context.expected_selection_version
    if expected_selection_version != actual["selection_version"]:
        raise _expert_context_error(
            "EXPERT_CONTEXT_STALE",
            "专家选择已在其他位置更新，请使用最新会话状态继续。",
            status_code=409,
            details={"expert_context": actual},
        )
    if expected.expected_branch_version != actual["branch_version"]:
        raise _expert_context_error(
            "EXPERT_CONTEXT_STALE",
            "专家上下文已更新，请使用最新会话状态继续。",
            status_code=409,
            details={"expert_context": actual},
        )
    if require_exact_identity and (
        expected.expert_team_id != actual["expert_team_id"]
        or expected.expert_id != actual["expert_id"]
    ):
        raise _expert_context_error(
            "EXPERT_CONTEXT_STALE",
            "当前专家或专家团已变化，请使用最新会话状态继续。",
            status_code=409,
            details={"expert_context": actual},
        )
    return actual


def _normalize_expert_context_versions(context, input_data: ProfileBoundInput) -> ProfileBoundInput:
    actual = expert_context_payload(context)
    expected = input_data.expert_context
    if expected.expected_branch_version is not None and expected.expected_selection_version is not None:
        return input_data
    return input_data.model_copy(
        update={
            "expert_context": expected.model_copy(
                update={
                    "expected_branch_version": actual["branch_version"]
                    if expected.expected_branch_version is None
                    else expected.expected_branch_version,
                    "expected_selection_version": actual["selection_version"]
                    if expected.expected_selection_version is None
                    else expected.expected_selection_version,
                }
            )
        }
    )


def _snapshot_expert_registries(context, orchestrator):
    """Resolve selection against the session's immutable deployment snapshot."""
    snapshot = (getattr(context, "session_meta", {}) or {}).get("configuration_snapshot")
    entries = snapshot.get("entries") if isinstance(snapshot, dict) else None
    if isinstance(entries, list) and entries:
        from hailiang_skills.workbench.catalog import build_runtime_registries

        _skills, experts, teams = build_runtime_registries(entries)
        return experts, teams
    return getattr(orchestrator, "expert_registry", None), getattr(orchestrator, "expert_team_registry", None)


def _apply_expert_context_operation(context, orchestrator, input_data: ProfileBoundInput) -> bool:
    """Apply explicit chat selections; ``null/null/continue`` inherits the server state."""
    expert_context = input_data.expert_context
    operation = expert_context.operation
    if not isinstance(input_data, ChatInput):
        if operation != "continue":
            raise _expert_context_error(
                "EXPERT_CONTEXT_OPERATION_INVALID",
                "切换专家团或专家只能通过 chat 的显式选择操作完成。",
            )
        _assert_expert_context_current(context, input_data, require_exact_identity=True)
        return False

    if operation == "continue":
        has_team_id = expert_context.expert_team_id is not None
        has_expert_id = expert_context.expert_id is not None
        # A direct Expert deliberately has no Team ID.  Therefore a concrete
        # ``expert_id`` with a null ``expert_team_id`` remains a valid legacy
        # strict assertion.  The only ambiguous shape is a Team without the
        # actual Expert currently answering for it.
        if has_team_id and not has_expert_id:
            raise _expert_context_error(
                "EXPERT_CONTEXT_OPERATION_INVALID",
                "继续对话时，提供 expert_team_id 时必须同时提供当前实际承接的 expert_id。",
            )
        # ``null/null`` deliberately has no client-side identity assertion.
        # It is the default ordinary-chat contract: another device, a restored
        # history page, or a newly selected child can continue under the
        # session's authoritative Agent without first hydrating local state.
        if not has_team_id:
            return context.apply_session_agent_selection()

        # A concrete pair remains the legacy strict assertion mode. It is
        # useful to callers that intentionally want stale-tab protection.
        _assert_expert_context_current(
            context,
            input_data,
            require_exact_identity=True,
        )
        return False

    if operation == "clear_expert":
        if expert_context.expert_team_id is not None or expert_context.expert_id is not None:
            raise _expert_context_error(
                "EXPERT_CONTEXT_OPERATION_INVALID",
                "退出专家模式时 expert_team_id 与 expert_id 必须均为 null。",
            )
        # This is intentionally an explicit user action rather than an
        # interpretation of null/null/continue.  It updates the session-wide
        # selection and lets the shared branch helper expire forms/Skills.
        context.set_session_agent_selection(
            expert_team_id=None,
            expert_id=None,
            selection_source="clear_expert",
        )
        context.session_meta.pop("branch_expert_override", None)
        changed = context.apply_session_agent_selection()
        context.session_meta.pop("pending_team_handoff_intent", None)
        context.event_trace.append(make_event("expert_mode_cleared", {
            "source": "toolbar",
        }))
        return changed

    # A selection is permitted only against the branch version the client just
    # rendered. Its target identity naturally differs from the current one.
    actual = _assert_expert_context_current(context, input_data, require_exact_identity=False)
    if operation == "select_team":
        if not expert_context.expert_team_id:
            raise _expert_context_error(
                "EXPERT_CONTEXT_OPERATION_INVALID",
                "选择专家团时必须提供 expert_team_id。",
            )
        if expert_context.expert_id is not None:
            raise _expert_context_error(
                "EXPERT_CONTEXT_OPERATION_INVALID",
                "选择专家团时 expert_id 必须为 null；直接选择成员请使用 select_expert。",
            )
        _experts, teams = _snapshot_expert_registries(context, orchestrator)
        team = teams.get(expert_context.expert_team_id) if teams is not None else None
        if team is None:
            raise HTTPException(status_code=422, detail="EXPERT_TEAM_NOT_FOUND")
        selected_expert_id = team.coordinator_expert_id
        if selected_expert_id not in team.member_expert_ids:
            raise HTTPException(status_code=422, detail="EXPERT_NOT_IN_ACTIVE_TEAM")
        if (actual["expert_team_id"], actual["expert_id"]) != (team.team_id, selected_expert_id):
            context.abandon_active_interactions_for_expert_change(
                reason="select_team",
                from_expert_id=actual["expert_id"],
                target_expert_id=selected_expert_id,
            )
        context.session_meta["expert_team_id"] = team.team_id
        context.session_meta["expert_id"] = selected_expert_id
        context.session_meta["active_expert_id"] = selected_expert_id
        context.session_meta["expert_selection_source"] = "manual_team"
        context.session_meta.pop("branch_expert_override", None)
        context.set_session_agent_selection(
            expert_team_id=team.team_id,
            expert_id=selected_expert_id,
            selection_source="manual_team",
        )
    elif operation in {"select_expert", "select_team_member"}:
        if not expert_context.expert_id:
            raise _expert_context_error("EXPERT_CONTEXT_OPERATION_INVALID", "选择专家时必须提供 expert_id。")
        registry, teams = _snapshot_expert_registries(context, orchestrator)
        requested_team_id = expert_context.expert_team_id
        if requested_team_id:
            team = teams.get(requested_team_id) if teams is not None else None
            if team is None:
                raise HTTPException(status_code=422, detail="EXPERT_TEAM_NOT_FOUND")
            if expert_context.expert_id not in team.member_expert_ids:
                raise HTTPException(status_code=422, detail="EXPERT_NOT_IN_ACTIVE_TEAM")
            team_id = team.team_id
        else:
            team_id = str(actual["expert_team_id"] or "")
            if team_id:
                raise _expert_context_error(
                    "EXPERT_CONTEXT_OPERATION_INVALID",
                    "当前已选择专家团时，选择成员必须同时提供 expert_team_id 和 expert_id。",
                )
            team = None
        definition = registry.get(expert_context.expert_id) if registry is not None else None
        if definition is None:
            raise HTTPException(status_code=422, detail="EXPERT_NOT_FOUND")
        if team is not None and definition.agent_id not in team.member_expert_ids:
            raise HTTPException(status_code=422, detail="EXPERT_NOT_IN_ACTIVE_TEAM")
        if (actual["expert_team_id"], actual["expert_id"]) != (team_id or None, definition.agent_id):
            context.abandon_active_interactions_for_expert_change(
                reason="select_expert",
                from_expert_id=actual["expert_id"],
                target_expert_id=definition.agent_id,
            )
        context.session_meta["expert_team_id"] = team_id or None
        context.session_meta["expert_id"] = definition.agent_id
        context.session_meta["active_expert_id"] = definition.agent_id
        context.session_meta["expert_selection_source"] = "manual"
        context.session_meta.pop("branch_expert_override", None)
        context.set_session_agent_selection(
            expert_team_id=team_id or None,
            expert_id=definition.agent_id,
            selection_source="manual",
        )
    else:  # Defensive: Pydantic owns the enum, but keep the router total.
        raise _expert_context_error("EXPERT_CONTEXT_OPERATION_INVALID", "未知的专家上下文操作。")
    context.session_meta.pop("expert_requested_skill_id", None)
    context.session_meta.pop("pending_team_handoff", None)
    context.session_meta.pop("pending_team_handoff_intent", None)
    return True


def _clear_legacy_implicit_expert_team(context) -> bool:
    """Migrate only the former automatic team entry back to general chat.

    Earlier releases wrote ``default_coordinator`` to every newly created
    profile branch. The public contract now keeps ordinary chat independent
    of experts; explicit team selection, direct expert selection and workbench
    snapshots must remain untouched.
    """
    meta = context.session_meta if isinstance(context.session_meta, dict) else {}
    if meta.get("expert_selection_source") != "default_coordinator":
        return False
    if meta.get("workbench_debug_session_id"):
        return False
    for key in (
        "expert_team_id",
        "active_expert_id",
        "expert_id",
        "expert_selection_source",
        "expert_requested_skill_id",
        "pending_team_handoff",
    ):
        meta.pop(key, None)
    active_skill = canonical_skill_id((context.interaction_state or {}).get("active_skill"))
    if active_skill in {"", CAREER_PLAN_SKILL_ID, "expert_direct"}:
        context.interaction_state["active_skill"] = GENERAL_CHAT_SKILL_ID
        runtime_state = context.skill_states.get("skill_runtime")
        if isinstance(runtime_state, dict):
            runtime_state["active_skill_id"] = GENERAL_CHAT_SKILL_ID
            runtime_state.setdefault("skill_facts", {}).setdefault(GENERAL_CHAT_SKILL_ID, {})
            runtime_state.setdefault("stage_facts", {}).setdefault(GENERAL_CHAT_SKILL_ID, {"answer": {}})
        planner_state = context.skill_states.get(CAREER_PLAN_SKILL_ID)
        if isinstance(planner_state, dict):
            planner_state["target_skill"] = GENERAL_CHAT_SKILL_ID
    return True


def _profile_context_notice(
    context,
    *,
    switched: bool,
    session_created: bool,
) -> dict[str, object]:
    """Build the public, user-displayable profile-context acknowledgement.

    The notice is deliberately part of the v2 state instead of assistant text:
    it is a deterministic session-context event, not something the model should
    phrase or remember.  The source/target metadata lets every client render a
    consistent system bubble without inferring it from profile IDs.
    """
    switch_item: dict[str, object] = {}
    for item in reversed(getattr(context, "timeline_items", [])):
        if isinstance(item, dict) and item.get("item_type") == "profile_switch":
            switch_item = item
            break

    target_scope = str(getattr(context, "context_scope", "profile") or "profile")
    target_profile_id = getattr(context, "profile_id", None)
    # Do not use ``context_label`` here: it intentionally falls back to a
    # generic label for UI chrome, whereas this acknowledgement promises that
    # the answer is based on a *named* child's archived data.
    target_name = str(getattr(context, "profile_name", "") or "").strip()
    target_label = str(getattr(context, "context_label", "") or "")
    from_scope = str(switch_item.get("from_context_scope") or ("profile" if switch_item.get("from_profile_id") else "unbound"))
    from_profile_id = switch_item.get("from_profile_id") or None
    from_label = str(switch_item.get("from_profile_name") or ("未绑定孩子" if from_scope == "unbound" else "未命名孩子"))

    if target_scope == "profile" and target_name and (switched or session_created):
        return {
            "type": "profile_switched" if switched else "profile_context_activated",
            "text": f"本轮回答将结合 **{target_name}** 的档案数据",
            "from_context_scope": from_scope if switched else None,
            "from_profile_id": str(from_profile_id) if switched and from_profile_id else None,
            "from_context_label": from_label if switched else "",
            "to_context_scope": target_scope,
            "to_profile_id": str(target_profile_id) if target_profile_id else None,
            "to_context_label": target_label,
        }
    if target_scope == "unbound" and switched:
        text = "已切换为未绑定孩子的上下文，后续回答不会使用任何孩子档案信息。"
        return {
            "type": "profile_switched",
            "text": text,
            "from_context_scope": from_scope,
            "from_profile_id": str(from_profile_id) if from_profile_id else None,
            "from_context_label": from_label,
            "to_context_scope": target_scope,
            "to_profile_id": None,
            "to_context_label": target_label,
        }
    return {}


def _claim_external_run(repository, context, run_id: str, *, action: str):
    """Persist the BFF run id before work starts, so retries cannot duplicate a turn."""
    # An older worker can be finishing exactly while a new action supersedes
    # it. Reload on optimistic-lock contention so this new run is claimed once
    # against the latest context instead of surfacing a transient 409/500.
    for _ in range(3):
        used = context.session_meta.setdefault("external_run_ids", [])
        if not isinstance(used, list):
            used = []
            context.session_meta["external_run_ids"] = used
        if run_id in used:
            raise HTTPException(status_code=409, detail="RUN_ID_CONFLICT")
        used.append(run_id)
        ledger = context.session_meta.setdefault("run_ledger", {})
        if not isinstance(ledger, dict):
            ledger = {}
            context.session_meta["run_ledger"] = ledger
        ledger[run_id] = {"status": "running", "action": action}
        try:
            repository.save(context)
            if hasattr(repository, "record_run"):
                repository.record_run(context, run_id, action)
            return context
        except SessionVersionConflict:
            context = repository.get(context.session_id)
    raise HTTPException(status_code=409, detail="SESSION_UPDATE_CONFLICT")


def _commit_run_action(repository, context, run_id: str, *, action: str, apply):
    """Atomically persist an interactive action together with its run claim.

    A team-handoff confirmation changes both the interaction card and the
    active expert.  Persisting that state after a separate run-claim save
    leaves an avoidable optimistic-lock window (and used to surface as a 409
    to the person clicking the card).  Re-apply the action to a freshly read
    context on contention so validation is still authoritative and no stale
    card state is written.
    """
    for _ in range(3):
        prepared = apply(context)
        used = context.session_meta.setdefault("external_run_ids", [])
        if not isinstance(used, list):
            used = []
            context.session_meta["external_run_ids"] = used
        if run_id in used:
            raise HTTPException(status_code=409, detail="RUN_ID_CONFLICT")
        used.append(run_id)
        ledger = context.session_meta.setdefault("run_ledger", {})
        if not isinstance(ledger, dict):
            ledger = {}
            context.session_meta["run_ledger"] = ledger
        ledger[run_id] = {"status": "running", "action": action}
        try:
            repository.save(context)
            if hasattr(repository, "record_run"):
                repository.record_run(context, run_id, action)
            return context, prepared
        except SessionVersionConflict:
            context = repository.get(context.session_id)
    raise HTTPException(status_code=409, detail="SESSION_UPDATE_CONFLICT")


def _save_and_refresh_context(repository, context):
    """Persist a pre-stream mutation and continue from its stored version.

    PostgreSQL guards every session write with an optimistic version. A single
    API action may need to save setup state (for example a legacy-mode
    migration or an explicit expert choice) before it claims its run. Reload
    after that save so the run-claim write cannot reuse the pre-save version.
    """
    repository.save(context)
    return repository.get(context.session_id)


def _stream_headers(request: Request) -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-SSE-Protocol": SSE_V2_PROTOCOL,
    }


def _state_snapshot(raw_sse: str) -> dict | None:
    """Extract a v2 state payload so the terminal event can repeat it verbatim."""
    lines = raw_sse.splitlines()
    if not lines or lines[0] != "event: state":
        return None
    data_lines = [line[5:].strip() for line in lines if line.startswith("data:")]
    if not data_lines:
        return None
    try:
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _with_done_event(stream, *, session_id: str, run_id: str):
    """Append one terminal event carrying the final complete state snapshot."""
    final_snapshot: dict | None = None
    for raw_sse in stream:
        snapshot = _state_snapshot(raw_sse)
        if snapshot is not None:
            final_snapshot = snapshot
        yield raw_sse
    yield format_sse_event(
        "done",
        final_snapshot
        or {
            "protocol": SSE_V2_PROTOCOL,
            "session_id": session_id,
            "run_id": run_id,
            "status": "completed",
        },
    )


def _pending_native_form(context) -> bool:
    runtime = context.skill_states.get("skill_runtime", {}) if isinstance(context.skill_states, dict) else {}
    active_skill_id = str(runtime.get("active_skill_id") or "") if isinstance(runtime, dict) else ""
    skill_facts = runtime.get("skill_facts", {}) if isinstance(runtime, dict) else {}
    active_facts = skill_facts.get(active_skill_id, {}) if isinstance(skill_facts, dict) else {}
    return isinstance(active_facts, dict) and isinstance(active_facts.get("_pending_questionnaire"), dict)


def _conversation_excerpt(context, *, end_index: int | None = None) -> str:
    end = len(context.messages) if end_index is None else end_index + 1
    excerpt_lines: list[str] = []
    for message in context.messages[max(0, end - 8):end]:
        if not isinstance(message, dict):
            continue
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if metadata.get("hidden"):
            continue
        text = str(message.get("content") or "").strip()
        if not text:
            continue
        label = "用户" if message.get("role") == "user" else "专家"
        excerpt_lines.append(f"{label}：{text}")
    return "\n".join(excerpt_lines)[-4000:]


def _find_team_handoff_source(context, source_message_id: str) -> tuple[dict, list[dict], str | None, str | None, dict | None] | None:
    """Find a handoff card in the active or an archived child branch.

    The card remains the authorization record even when the caller chose a
    different target child through top-level ``context_data.profile_id``.
    """
    candidates: list[tuple[list[dict], str | None, str | None, dict | None]] = [
        (context.messages, context.profile_id, context.profile_name, None),
    ]
    for profile_id, branch in (context.profile_branches or {}).items():
        if not isinstance(branch, dict) or str(profile_id or "") == str(context.profile_id or ""):
            continue
        messages = branch.get("messages")
        if isinstance(messages, list):
            candidates.append((messages, str(profile_id or "") or None, branch.get("profile_name"), branch))
    for messages, profile_id, profile_name, branch in candidates:
        source = next((
            message for message in messages
            if isinstance(message, dict)
            and str(message.get("message_id") or "") == source_message_id
            and message.get("role") == "assistant"
        ), None)
        if source is not None:
            return source, messages, profile_id, profile_name, branch
    return None


def _confirm_team_handoff(context, orchestrator, input_data: ConfirmTeamHandoffInput) -> dict:
    located = _find_team_handoff_source(context, input_data.source_message_id)
    if located is None:
        raise HTTPException(status_code=404, detail="TEAM_HANDOFF_SOURCE_NOT_FOUND")
    source, source_messages, source_profile_id, _source_profile_name, source_branch = located
    execution_profile_id = str(context.profile_id or "") or None
    cross_profile = source_profile_id != execution_profile_id
    handoff = source.get("team_handoff")
    if not isinstance(handoff, dict):
        metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
        handoff = metadata.get("team_handoff")
    team_id = str(handoff.get("team_id") or "").strip() if isinstance(handoff, dict) else ""
    _experts, teams = _snapshot_expert_registries(context, orchestrator)
    team = teams.get(team_id) if teams is not None else None
    if team is None:
        raise HTTPException(status_code=409, detail="EXPERT_TEAM_NOT_ACTIVE")
    if not isinstance(handoff, dict) or str(handoff.get("team_id") or "") != team.team_id:
        raise HTTPException(status_code=409, detail="TEAM_HANDOFF_NOT_ACTIVE")
    candidates = handoff.get("candidates") if isinstance(handoff.get("candidates"), list) else []
    candidate = next((item for item in candidates if isinstance(item, dict) and str(item.get("expert_id") or "") == input_data.target_expert_id), None)
    if candidate is None:
        raise HTTPException(status_code=422, detail="TEAM_HANDOFF_TARGET_NOT_ALLOWED")
    interaction = ensure_message_interactions(source).get("team_handoff")
    if not isinstance(interaction, dict) or interaction.get("status") != ACTIVE:
        raise HTTPException(status_code=409, detail="TEAM_HANDOFF_NOT_ACTIVE")
    from_expert_id = str(context.session_meta.get("active_expert_id") or team.coordinator_expert_id)
    context.abandon_active_interactions_for_expert_change(
        reason="confirm_team_handoff",
        from_expert_id=from_expert_id,
        target_expert_id=input_data.target_expert_id,
    )
    try:
        interaction = update_interaction(source, "team_handoff", status=SELECTED, selected_target_skill_id=input_data.target_expert_id)
        interaction["selected_target_expert_id"] = input_data.target_expert_id
    except KeyError:
        raise HTTPException(status_code=409, detail="TEAM_HANDOFF_NOT_ACTIVE") from None
    handoff["status"] = "selected"
    handoff["selected_target_expert_id"] = input_data.target_expert_id
    handoff.update({
        "source_profile_id": source_profile_id,
        "execution_profile_id": execution_profile_id,
        "cross_profile": cross_profile,
    })
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    if isinstance(metadata.get("team_handoff"), dict):
        metadata["team_handoff"].update({
            "status": "selected",
            "selected_target_expert_id": input_data.target_expert_id,
            "source_profile_id": source_profile_id,
            "execution_profile_id": execution_profile_id,
            "cross_profile": cross_profile,
        })
    context.session_meta["expert_team_id"] = team_id
    context.session_meta["active_expert_id"] = input_data.target_expert_id
    context.session_meta["expert_id"] = input_data.target_expert_id
    context.session_meta["expert_selection_source"] = "cross_profile_handoff" if cross_profile else "handoff_card"
    if cross_profile:
        # Cross-child confirmation is an authorization transfer, not a
        # session-wide Agent preference change.  It applies only to the
        # execution child selected by context_data.profile_id.
        context.session_meta["branch_expert_override"] = {
            "expert_team_id": team_id,
            "expert_id": input_data.target_expert_id,
            "source": "cross_profile_handoff",
            "source_profile_id": source_profile_id,
            "source_message_id": input_data.source_message_id,
        }
    else:
        context.set_session_agent_selection(
            expert_team_id=team_id,
            expert_id=input_data.target_expert_id,
            selection_source="handoff_card",
        )
    context.session_meta.pop("pending_team_handoff", None)
    if isinstance(source_branch, dict):
        branch_meta = source_branch.get("session_meta")
        if isinstance(branch_meta, dict):
            branch_meta.pop("pending_team_handoff", None)
            branch_meta.pop("pending_team_handoff_intent", None)
    source_user_message = ""
    source_index = source_messages.index(source)
    for message in reversed(source_messages[:source_index]):
        if message.get("role") == "user" and not (message.get("metadata") or {}).get("hidden"):
            source_user_message = str(message.get("content") or "")
            break
    switch_context = {
        "source": "team_handoff",
        "from_expert_id": from_expert_id,
        "target_expert_id": input_data.target_expert_id,
        "mention_name": str(candidate.get("mention_name") or "").strip(),
        "visible_user_message": f"@{str(candidate.get('mention_name') or '专家').strip()}",
        "source_user_message": source_user_message,
        "coordinator_reason": str(handoff.get("reason") or ""),
        "source_message_id": input_data.source_message_id,
        # Do not carry source-child history into another child's runtime.
        # The original question is the only permitted cross-branch payload.
        "conversation_excerpt": "" if cross_profile else _conversation_excerpt(context, end_index=source_index),
        "source_profile_id": source_profile_id,
        "execution_profile_id": execution_profile_id,
        "cross_profile": cross_profile,
    }
    return switch_context


def _acknowledge_team_handoff_text(context, orchestrator, acknowledgement: str) -> tuple[dict | None, dict | None]:
    """Recognize a text acknowledgement, but never use it to switch Expert."""
    team_id = str(context.session_meta.get("expert_team_id") or "").strip()
    if not team_id:
        return None, None
    decision = active_handoff_decision(context, team_id=team_id, text=acknowledgement)
    if decision.get("kind") == "none":
        return None, None
    fresh = block_text_handoff_confirmation(context, decision)
    return ({"blocked": True, "handoff": fresh} if fresh else None), None


def _switch_team_member(context, orchestrator, input_data: SwitchTeamMemberInput) -> dict:
    team_id = str(context.session_meta.get("expert_team_id") or "").strip()
    _experts, teams = _snapshot_expert_registries(context, orchestrator)
    team = teams.get(team_id) if teams is not None else None
    if team is None:
        raise HTTPException(status_code=409, detail="EXPERT_TEAM_NOT_ACTIVE")
    content = input_data.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="TEAM_SWITCH_CONTENT_REQUIRED")
    member = team.member_for_expert(input_data.target_expert_id)
    if member is None:
        raise HTTPException(status_code=422, detail="EXPERT_NOT_IN_ACTIVE_TEAM")
    from_expert_id = str(context.session_meta.get("active_expert_id") or team.coordinator_expert_id)
    if from_expert_id != member.expert_id:
        context.abandon_active_interactions_for_expert_change(
            reason="switch_team_member",
            from_expert_id=from_expert_id,
            target_expert_id=member.expert_id,
        )
    context.session_meta["active_expert_id"] = member.expert_id
    context.session_meta["expert_id"] = member.expert_id
    context.session_meta["expert_selection_source"] = "manual"
    context.set_session_agent_selection(
        expert_team_id=team_id,
        expert_id=member.expert_id,
        selection_source="manual",
    )
    context.session_meta.pop("pending_team_handoff", None)
    return {
        "source": "toolbar",
        "from_expert_id": from_expert_id,
        "target_expert_id": member.expert_id,
        "mention_name": member.mention_name,
        "content": content,
        "visible_user_message": f"@{member.mention_name} {content}",
        "source_message_id": None,
        "conversation_excerpt": _conversation_excerpt(context),
    }


def build_chat_stream_router(
    repository: InMemorySessionRepository,
    fact_service: FactService,
    orchestrator,
    turn_coordinator: TurnCoordinator | None = None,
    llm_rate_limiter: LLMRateLimiter | None = None,
    configuration_snapshot_resolver=None,
    debug_configuration_snapshot_resolver=None,
) -> APIRouter:
    router = APIRouter()
    runner = StreamingRunner(repository, fact_service, orchestrator, turn_coordinator=turn_coordinator)

    @router.post("/sessions/chat/stream")
    def post_chat_stream(request: ChatStreamRequest, http_request: Request) -> StreamingResponse:
        input_data = _parse_input(request.input)
        if isinstance(input_data, StopInput):
            try:
                context = repository.get(request.session_id)
            except KeyError as exc:
                raise HTTPException(status_code=409, detail="RUN_NOT_ACTIVE") from exc
            # ``stream_stop`` is a generator, so validation inside it would
            # otherwise happen after FastAPI has already returned HTTP 200.
            # Claim cancellation synchronously to keep a stale stop request a
            # deterministic 409 response.
            if not runner.cancel_run(request.session_id, context.user_id, request.run_id):
                raise HTTPException(status_code=409, detail="RUN_NOT_ACTIVE")
            stream = runner.stream_stop(
                request.session_id,
                context.user_id,
                run_id=request.run_id,
                source_endpoint="sessions/chat/stream",
                already_cancelled=True,
            )
            return StreamingResponse(
                _with_done_event(stream, session_id=request.session_id, run_id=request.run_id),
                media_type="text/event-stream",
                headers=_stream_headers(http_request),
            )

        if request.context_data is None:
            raise HTTPException(
                status_code=422,
                detail="context_data is required for non-stop actions",
            )
        if isinstance(input_data, OpenSessionInput):
            # The contract is deliberately reserved now, while the current
            # product keeps client_opening presentation-only and non-persistent.
            raise HTTPException(status_code=501, detail="MODEL_OPENING_NOT_ENABLED")
        if llm_rate_limiter is not None:
            request_id = str(getattr(http_request.state, "hailiang_request_id", "") or "")
            try:
                llm_rate_limiter.reserve_for_request(request_id)
            except LLMRateLimitError as exc:
                raise HTTPException(
                    status_code=429,
                    detail="LLM_RATE_LIMITED",
                    headers={"Retry-After": "1"},
                ) from exc
        context_scope, selected_profile_id = _resolve_context_scope(input_data, request.context_data)
        if context_scope == "profile":
            # ``context_data.profile_id`` is constructed by the BFF and is
            # deliberately the only child identity accepted by SSE v2.
            target_profile_id: str | None = selected_profile_id
            profile_context_status = "matched"
            allow_context_seed = True
        else:
            target_profile_id = None
            profile_context_status = "unbound"
            allow_context_seed = False
        try:
            existing_context = repository.get(request.session_id)
        except KeyError:
            existing_context = None
        if existing_context is not None and existing_context.user_id != request.context_data.user_id:
            raise HTTPException(status_code=409, detail="SESSION_ID_CONFLICT")
        # A handoff card can authorize an Expert for the target child supplied
        # by context_data.  The card itself is looked up safely by message ID
        # after the target branch is active, so this remains input-compatible
        # with existing clients.
        requested_context_activation = input_data.context_activation
        requested_target_key = str(target_profile_id or "")
        if (
            existing_context is not None
            and requested_context_activation == "strict"
            and str(existing_context.profile_id or "") != requested_target_key
        ):
            raise HTTPException(status_code=409, detail="CONTEXT_ACTIVATION_REQUIRED")
        if existing_context is not None and str(existing_context.profile_id or "") != str(target_profile_id or ""):
            ledger = existing_context.session_meta.get("run_ledger")
            running = [
                run_id
                for run_id, item in (ledger.items() if isinstance(ledger, dict) else [])
                if isinstance(item, dict) and item.get("status") == "running" and run_id != request.run_id
            ]
            if running:
                raise HTTPException(status_code=409, detail="ACTIVE_RUN_MUST_STOP")

        source_profile_id_before_activation = (
            str(existing_context.profile_id or "") or None
            if existing_context is not None
            else None
        )
        cross_profile_exit = bool(
            isinstance(input_data, QuitSkillInput)
            and existing_context is not None
            and requested_context_activation == "auto"
            and str(existing_context.profile_id or "") != str(target_profile_id or "")
        )
        prepared_cross_profile_exit = None
        if cross_profile_exit:
            # Exit belongs to the currently active source branch. Persist it
            # before activating the execution child selected by context_data.
            try:
                prepared_cross_profile_exit = runner.prepare_skill_transition(
                    request.session_id,
                    existing_context.user_id,
                    action="exit",
                    target_skill_id=input_data.target_skill_id,
                    source=input_data.source,
                    run_id=request.run_id,
                    source_profile_id=source_profile_id_before_activation,
                    execution_profile_id=target_profile_id,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        context, session_created = open_or_resume_session(
            repository,
            fact_service,
            session_id=request.session_id,
            data=request.context_data,
            target_profile_id=target_profile_id,
            allow_context_seed=allow_context_seed,
            context_scope=context_scope,
        )
        bound_debug_session_id = str(context.session_meta.get("workbench_debug_session_id") or "")
        if request.debug_session_id and bound_debug_session_id and request.debug_session_id != bound_debug_session_id:
            raise HTTPException(status_code=409, detail="DEBUG_SNAPSHOT_CONFLICT")
        preflight_context_saved = False
        if session_created and request.debug_session_id:
            if debug_configuration_snapshot_resolver is None:
                raise HTTPException(status_code=422, detail="DEBUG_SNAPSHOT_NOT_AVAILABLE")
            try:
                snapshot = debug_configuration_snapshot_resolver(request.debug_session_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            context.session_meta["configuration_snapshot"] = snapshot
            context.session_meta["workbench_debug_session_id"] = request.debug_session_id
            repository.save(context)
            preflight_context_saved = True
        elif session_created and configuration_snapshot_resolver is not None:
            snapshot = configuration_snapshot_resolver()
            if snapshot is not None:
                context.session_meta["configuration_snapshot"] = snapshot
                repository.save(context)
                preflight_context_saved = True
        if preflight_context_saved:
            context = repository.get(context.session_id)
        configuration_change = None
        if (
            not session_created
            and not bound_debug_session_id
            and configuration_snapshot_resolver is not None
        ):
            from hailiang_skills.api.configuration_sync import synchronize_configuration

            configuration_change = synchronize_configuration(context, configuration_snapshot_resolver())
            if configuration_change is not None:
                repository.save(context)
                context = repository.get(context.session_id)
                if isinstance(input_data, ConfirmTeamHandoffInput):
                    raise HTTPException(status_code=409, detail={
                        "code": "CONFIGURATION_UPDATED",
                        "message": "专家团配置已更新，原转交卡已失效，请重新提问。",
                        "configuration": configuration_change,
                    })
        input_data = _normalize_expert_context_versions(context, input_data)
        legacy_default_team_cleared = _clear_legacy_implicit_expert_team(context)
        profile_switched = bool(context.session_meta.get("_profile_switched"))
        profile_branch_created = bool(context.session_meta.get("_profile_branch_created"))
        automatic_context_activation = bool(
            input_data.context_activation == "auto"
            and (session_created or profile_switched or profile_branch_created)
        )

        # Every action carries the client-rendered expert state.  A normal
        # chat only asserts it, except while an auto activation has just
        # entered a different branch. In that case this very request is the
        # context preflight: restore the session-wide Agent selection first,
        # return it as authoritative state, and never make the client retry.
        if isinstance(input_data, ConfirmTeamHandoffInput):
            # The card is the authority for this action.  A cross-child click
            # may carry the old branch's rendered expert_context, which must
            # not reject activation of the target child before card validation.
            expert_context_changed = False
        elif automatic_context_activation and input_data.expert_context.operation == "continue":
            expert_context_changed = context.apply_session_agent_selection()
        else:
            expert_context_changed = _apply_expert_context_operation(context, orchestrator, input_data)

        if legacy_default_team_cleared or expert_context_changed:
            context = _save_and_refresh_context(repository, context)

        profile_context_event = {
            "profile_id": context.profile_id,
            "profile_name": context.profile_name,
            "context_scope": context.context_scope,
            "context_label": context.context_label,
            "context_switched": profile_switched,
            "context_notice": _profile_context_notice(
                context,
                switched=profile_switched,
                session_created=session_created,
            ),
            "branch_version": int(context.session_meta.get("_active_branch_version") or 0),
            "profile_context_status": profile_context_status,
            "session_created": bool(session_created),
            "profile_switched": profile_switched,
            "context_activation": "auto" if automatic_context_activation else "none",
            "configuration_changed": configuration_change is not None,
            "configuration": configuration_change or {
                "deployment_id": str((context.session_meta.get("configuration_snapshot") or {}).get("deployment_id") or "") or None,
                "package_hash": str((context.session_meta.get("configuration_snapshot") or {}).get("package_hash") or "") or None,
            },
        }
        context.session_meta["_sse_profile_context"] = profile_context_event
        # Persist this immediately.  ``stop`` is allowed before the model has
        # emitted its first token and must still be able to return the exact
        # scope/expert state for the run.
        context = _save_and_refresh_context(repository, context)

        if not profile_switched and hasattr(runner, "supersede_active_run"):
            runner.supersede_active_run(request.session_id, context.user_id, next_run_id=request.run_id)
        if isinstance(input_data, ConfirmTeamHandoffInput):
            def apply_handoff(current_context):
                team_member_switch = _confirm_team_handoff(current_context, orchestrator, input_data)
                event = make_event("team_handoff_confirmed", {
                    "team_id": str(current_context.session_meta.get("expert_team_id") or ""),
                    "expert_id": input_data.target_expert_id,
                    "source_message_id": input_data.source_message_id,
                    "source_profile_id": team_member_switch.get("source_profile_id"),
                    "execution_profile_id": team_member_switch.get("execution_profile_id"),
                    "cross_profile": bool(team_member_switch.get("cross_profile")),
                })
                # Keep the event in the same optimistic write as the selected
                # card and active-expert state.  The file index is appended
                # only after that write succeeds, avoiding orphan audit rows.
                current_context.event_trace.append(event)
                return team_member_switch, event

            context, (team_member_switch, event) = _commit_run_action(
                repository,
                context,
                request.run_id,
                action=input_data.action,
                apply=apply_handoff,
            )
            append_session_events(request.session_id, [event])
            try:
                lease = runner.reserve_turn(request.session_id, context.user_id, run_id=request.run_id)
            except CapacityExceededError as exc:
                raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
            handoff_state = {
                "status": "selected",
                "team_id": str(team_member_switch.get("team_id") or ""),
                "source_message_id": input_data.source_message_id,
                "selected_target_expert_id": input_data.target_expert_id,
                "source_profile_id": team_member_switch.get("source_profile_id"),
                "execution_profile_id": team_member_switch.get("execution_profile_id"),
                "cross_profile": bool(team_member_switch.get("cross_profile")),
            }
            stream = runner.stream_message(
                request.session_id,
                context.user_id,
                str(team_member_switch.get("source_user_message") or "专家接管"),
                enable_thinking=input_data.enable_thinking,
                return_reasoning=input_data.return_reasoning,
                team_member_switch=team_member_switch,
                lease=lease,
                protocol=SSE_V2_PROTOCOL,
                source_endpoint="sessions/chat/stream",
                initial_events=[
                    ("profile_context", profile_context_event),
                    ("team_handoff", handoff_state),
                ],
            )
        elif isinstance(input_data, SwitchTeamMemberInput):
            def apply_member_switch(current_context):
                team_member_switch = _switch_team_member(current_context, orchestrator, input_data)
                team_member_switch.update({
                    "source_profile_id": source_profile_id_before_activation,
                    "execution_profile_id": current_context.profile_id,
                    "cross_profile": bool(profile_switched and source_profile_id_before_activation != current_context.profile_id),
                })
                event = make_event("team_member_selected_from_toolbar", {
                    "team_id": str(current_context.session_meta.get("expert_team_id") or ""),
                    "from_expert_id": team_member_switch["from_expert_id"],
                    "expert_id": input_data.target_expert_id,
                })
                current_context.event_trace.append(event)
                return team_member_switch, event

            context, (team_member_switch, event) = _commit_run_action(
                repository,
                context,
                request.run_id,
                action=input_data.action,
                apply=apply_member_switch,
            )
            append_session_events(request.session_id, [event])
            try:
                lease = runner.reserve_turn(request.session_id, context.user_id, run_id=request.run_id)
            except CapacityExceededError as exc:
                raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
            stream = runner.stream_message(
                request.session_id,
                context.user_id,
                input_data.content,
                enable_thinking=input_data.enable_thinking,
                return_reasoning=input_data.return_reasoning,
                team_member_switch=team_member_switch,
                lease=lease,
                protocol=SSE_V2_PROTOCOL,
                source_endpoint="sessions/chat/stream",
                initial_events=[("profile_context", profile_context_event)],
            )
        elif isinstance(input_data, ChatInput):
            # A new free-form turn supersedes any unconfirmed coordinator
            # recommendation.  The same rule already applies to client-side
            # cards; persist it so stale cards cannot be confirmed through
            # a delayed request.  Expire those interactions in the same
            # optimistic write that claims this run: ``supersede_active_run``
            # may have advanced the stored version immediately beforehand.
            def apply_free_form_turn(current_context):
                switch, replay_handoff = _acknowledge_team_handoff_text(
                    current_context, orchestrator, input_data.content,
                )
                if switch is not None:
                    if switch.get("blocked"):
                        fresh_handoff = switch.get("handoff") if isinstance(switch.get("handoff"), dict) else None
                        event = make_event("team_handoff_text_confirmation_blocked", {
                            "team_id": str(current_context.session_meta.get("expert_team_id") or ""),
                            "old_handoff_id": str((switch.get("handoff") or {}).get("previous_handoff_id") or ""),
                            "new_handoff_id": str((switch.get("handoff") or {}).get("handoff_id") or ""),
                            "reason": "card_only_confirmation",
                        })
                        current_context.event_trace.append(event)
                        current_context.session_meta["control_reply"] = "如需切换专家，请点击专家转交卡片完成确认；如果不切换，也可以继续描述您的问题。"
                        current_context.session_meta["control_handoff"] = fresh_handoff
                        return {"switch": None, "replay_handoff": fresh_handoff, "event": event, "blocked": True}
                    event = make_event("team_handoff_text_confirmed", {
                        "team_id": str(current_context.session_meta.get("expert_team_id") or ""),
                        "expert_id": str(switch.get("target_expert_id") or ""),
                        "source": str(switch.get("source") or "team_handoff_ack"),
                    })
                    current_context.event_trace.append(event)
                    return {"switch": switch, "replay_handoff": None, "event": event}
                if replay_handoff is not None:
                    event = make_event("team_handoff_text_confirmation_ambiguous", {
                        "team_id": str(current_context.session_meta.get("expert_team_id") or ""),
                        "handoff_id": str(replay_handoff.get("handoff_id") or ""),
                    })
                    current_context.event_trace.append(event)
                    return {"switch": None, "replay_handoff": replay_handoff, "event": event}
                expired = expire_active_interactions(current_context.messages)
                if expired:
                    current_context.session_meta.pop("pending_team_handoff", None)
                    current_context.session_meta.pop("pending_team_handoff_intent", None)
                return {"switch": None, "replay_handoff": None, "event": None}

            context, free_form_result = _commit_run_action(
                repository,
                context,
                request.run_id,
                action=input_data.action,
                apply=apply_free_form_turn,
            )
            team_member_switch = free_form_result.get("switch") if isinstance(free_form_result, dict) else None
            control_handoff = bool(free_form_result.get("blocked")) if isinstance(free_form_result, dict) else False
            replay_handoff = free_form_result.get("replay_handoff") if isinstance(free_form_result, dict) else None
            event = free_form_result.get("event") if isinstance(free_form_result, dict) else None
            if event:
                append_session_events(request.session_id, [event])
            try:
                lease = runner.reserve_turn(request.session_id, context.user_id, run_id=request.run_id)
            except CapacityExceededError as exc:
                raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
            stream = runner.stream_message(
                request.session_id,
                context.user_id,
                input_data.content,
                enable_thinking=input_data.enable_thinking,
                return_reasoning=input_data.return_reasoning,
                team_member_switch=team_member_switch,
                lease=lease,
                protocol=SSE_V2_PROTOCOL,
                source_endpoint="sessions/chat/stream",
                initial_events=([("profile_context", profile_context_event)] + ([ ("team_handoff", replay_handoff) ] if replay_handoff and not control_handoff else [])),
            )
        else:
            action = "enter" if input_data.action == "enter_skill" else "exit"
            if isinstance(input_data, EnterSkillInput) and (
                context.session_meta.get("expert_team_id") or context.session_meta.get("expert_id")
            ):
                # Toolbar/route-suggestion Skill entry is an explicit
                # standalone action. It deliberately leaves the current
                # team/expert before entering the selected Skill, including
                # after an automatic child activation.
                from_expert_id = str(context.session_meta.get("active_expert_id") or context.session_meta.get("expert_id") or "")
                context.abandon_active_interactions_for_expert_change(
                    reason="enter_direct_skill",
                    from_expert_id=from_expert_id,
                    target_expert_id=None,
                )
                context.session_meta.pop("expert_team_id", None)
                context.session_meta.pop("expert_id", None)
                context.session_meta.pop("active_expert_id", None)
                context.session_meta.pop("expert_requested_skill_id", None)
                context.set_session_agent_selection(
                    expert_team_id=None,
                    expert_id=None,
                    selection_source="direct_skill",
                )
                repository.save(context)
            if isinstance(input_data, QuitSkillInput) and prepared_cross_profile_exit is None:
                active_skill = str(
                    context.interaction_state.get("active_skill")
                    or context.skill_states.get("skill_runtime", {}).get("active_skill_id")
                    or CAREER_PLAN_SKILL_ID
                )
                active_skill = canonical_skill_id(active_skill)
                if input_data.target_skill_id != active_skill:
                    raise HTTPException(status_code=409, detail="QUIT_SKILL_TARGET_MISMATCH")
            try:
                prepared = prepared_cross_profile_exit or runner.prepare_skill_transition(
                    request.session_id,
                    context.user_id,
                    action=action,
                    target_skill_id=input_data.target_skill_id,
                    source=input_data.source,
                    source_message_id=getattr(input_data, "source_message_id", None),
                    source_interaction_id=getattr(input_data, "source_interaction_id", None),
                    run_id=request.run_id,
                    source_profile_id=source_profile_id_before_activation if profile_switched else None,
                    execution_profile_id=context.profile_id if profile_switched else None,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            context = _claim_external_run(repository, context, request.run_id, action=input_data.action)
            try:
                lease = runner.reserve_turn(request.session_id, context.user_id, run_id=request.run_id)
            except CapacityExceededError as exc:
                raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "5"}) from exc
            stream = runner.stream_skill_transition(
                request.session_id,
                context.user_id,
                action=action,
                target_skill_id=input_data.target_skill_id,
                source=input_data.source,
                source_message_id=getattr(input_data, "source_message_id", None),
                source_interaction_id=getattr(input_data, "source_interaction_id", None),
                enable_thinking=input_data.enable_thinking,
                return_reasoning=input_data.return_reasoning,
                prepared_transition=prepared,
                lease=lease,
                protocol=SSE_V2_PROTOCOL,
                source_endpoint="sessions/chat/stream",
                initial_events=[("profile_context", profile_context_event)],
            )
        return StreamingResponse(
            _with_done_event(stream, session_id=request.session_id, run_id=request.run_id),
            media_type="text/event-stream",
            headers=_stream_headers(http_request),
        )

    return router
