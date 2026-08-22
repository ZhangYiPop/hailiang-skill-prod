"""AgentScope-backed, permission-limited Expert runtime (single expert v1)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, SecretStr

from hailiang_skills.runtime_bridge.expert_bundle import ExpertDefinition, ExpertRegistry
from hailiang_skills.runtime_bridge.expert_team_bundle import ExpertTeamDefinition, ExpertTeamRegistry
from hailiang_skills.runtime_bridge.expert_models import ExpertMember, SkillObservation
from hailiang_skills.runtime_bridge.native_skill_executor import NativeSkillExecutor


AGENT_RUNTIME_STATE_KEY = "agent_runtime"
DEFAULT_EXPERT_ID = "career_plan_expert"
_FRAMEWORK_FAILURE_REPLY_MARKERS = (
    "maximum reasoning-acting iterations",
    "maximum reasoning acting iterations",
    "max iterations",
)


class _AgentScopeParameters(BaseModel):
    """The existing Hailiang client owns its request parameters."""


class AgentScopeRuntimeUnavailable(RuntimeError):
    pass


def agentscope_available() -> tuple[bool, str]:
    try:
        import agentscope  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment issue
        return False, str(exc)
    return True, ""


class AgentScopeExpertRuntime:
    """Coordinates an expert without giving AgentScope any host capabilities.

    AgentScope can only call the three tools declared in the bundle.  The
    final native Skill execution remains Hailiang's existing bridge; the tool
    call records an authorized, bounded handoff that the bridge consumes.
    """

    def __init__(
        self,
        expert_registry: ExpertRegistry,
        runtime_registry,
        *,
        team_registry: ExpertTeamRegistry | None = None,
        default_expert_id: str = DEFAULT_EXPERT_ID,
        client_factory=None,
        event_recorder=None,
    ) -> None:
        self.expert_registry = expert_registry
        self.team_registry = team_registry or ExpertTeamRegistry(definitions={})
        self.runtime_registry = runtime_registry
        self.default_expert_id = default_expert_id
        self.client_factory = client_factory
        self.event_recorder = event_recorder
        self.native_executor = NativeSkillExecutor(runtime_registry)
        self._available, self._availability_error = agentscope_available()

    def health(self) -> dict[str, Any]:
        definition = self.expert_registry.get(self.default_expert_id)
        return {
            "available": self._available,
            "error": self._availability_error or None,
            "default_expert_id": self.default_expert_id,
            "expert_loaded": definition is not None,
            "team_count": len(self.team_registry.definitions),
            "topology": definition.topology if definition else None,
        }

    def handle_message(self, user_message: str, context, legacy_handler):
        # The API/UI may select a registered expert for a session.  In the
        # absence of a selection we retain the existing career expert as the
        # compatible default.  The registry is still the authority here: an
        # arbitrary ID cannot manufacture a new expert or expand its skills.
        team = self._current_team(context)
        user_message = self._apply_structured_team_switch(context, team, user_message)
        requested_expert_id = str(
            getattr(context, "session_meta", {}).get("active_expert_id")
            or getattr(context, "session_meta", {}).get("expert_id")
            or (team.coordinator_expert_id if team else self.default_expert_id)
        ).strip()
        if team is not None and requested_expert_id not in team.member_expert_ids:
            requested_expert_id = team.coordinator_expert_id
            context.session_meta["active_expert_id"] = requested_expert_id
            context.session_meta["expert_id"] = requested_expert_id
        definition = self.expert_registry.require(requested_expert_id)
        if not self._available:
            # The legacy planner is deliberately not an automatic fallback for
            # a missing AgentScope dependency. Readiness is false as well, so
            # deployment fails loudly instead of changing decision semantics.
            raise AgentScopeRuntimeUnavailable(
                f"AgentScope 专家运行时不可用: {self._availability_error or 'unknown error'}"
            )
        state = self._state(context, definition, team=team)
        state["turn_id"] = f"expert_turn_{uuid4().hex[:12]}"
        state["budget"] = {
            "max_iters": definition.max_iters,
            "max_skill_calls": definition.max_skill_calls,
            "skill_calls": 0,
        }
        state["pending_form"] = None
        # A handoff proposal belongs to exactly one coordinator turn.  Keeping
        # the previous value here caused a member's next reply to inherit and
        # re-attach the coordinator's already-consumed card.
        state.pop("team_handoff", None)
        state["member_runs"] = []  # Reserved: v1 never creates members.
        state["delegation_trace"] = []  # Reserved: v1 never delegates.
        event_payload = {"expert_id": definition.agent_id, "topology": definition.topology}
        if team is not None:
            event_payload.update({"team_id": team.team_id, "is_coordinator": definition.agent_id == team.coordinator_expert_id})
            self._event(context, "team_coordinator_started" if definition.agent_id == team.coordinator_expert_id else "team_member_started", event_payload)
        self._event(context, "expert_started", event_payload)

        client = self.client_factory(context) if self.client_factory else None
        if self._is_supported_client(client) and self._available:
            try:
                self._run_agent(definition, user_message, context, client, state, team=team)
            except Exception as exc:
                # Do not silently use the old orchestrator as the operational
                # fallback. Keep the existing native route as the controlled
                # executor, but expose an explicit degraded expert event.
                state["agent_scope_error"] = str(exc)
                self._event(context, "expert_runtime_degraded", {"expert_id": definition.agent_id, "error": str(exc)})
        else:
            self._event(context, "expert_decision_deferred", {"reason": "llm_client_unavailable"})

        # If the expert can answer within its own role boundary, its reply is
        # authoritative.  The legacy planner is only an executor for a Skill
        # handoff or native form; letting it run unconditionally here used to
        # overwrite the expert's answer with general_chat/soul.md output.
        agent_reply = str(state.get("agent_reply") or "").strip()
        handoff = state.get("team_handoff") if isinstance(state.get("team_handoff"), dict) else None
        if self._is_framework_failure_reply(agent_reply):
            # AgentScope may surface its ReAct-limit diagnostic as the final
            # text after a tool already completed.  That is infrastructure
            # text, never a valid expert reply, and must not reach users.
            state["agent_reply_error"] = agent_reply
            self._event(
                context,
                "expert_agent_reply_discarded",
                {"expert_id": definition.agent_id, "reason": "framework_iteration_limit"},
            )
            agent_reply = ""
        if not agent_reply and handoff is not None:
            # A handoff proposal is already authoritative and persisted by
            # the controlled tool.  Give it a deterministic, user-facing
            # explanation even when the Agent did not get a final ReAct turn.
            agent_reply = self._team_handoff_reply(handoff)
            state["agent_reply"] = agent_reply
        elif not agent_reply and state.get("agent_reply_error"):
            # Preserve the Expert boundary on a framework-only failure rather
            # than accidentally falling through to legacy general_chat.
            agent_reply = "抱歉，我这轮没有完成处理。请重试一次，或补充更具体的情况，我会继续协助你。"
            state["agent_reply"] = agent_reply
        has_native_handoff = bool(
            context.session_meta.get("expert_requested_skill_id")
            or state.get("pending_form")
            or self._has_pending_native_questionnaire(context)
        )
        if agent_reply and not has_native_handoff:
            state["execution_mode"] = "expert_direct"
            context.session_meta["expert_direct_reply"] = {
                "expert_id": definition.agent_id,
                "reply": agent_reply,
            }
        result = legacy_handler(user_message, context)
        if (
            team is not None
            and self._can_propose_team_handoff(team, definition.agent_id)
            and isinstance(state.get("team_handoff"), dict)
            and state["team_handoff"].get("proposal_turn_id") == state["turn_id"]
        ):
            self._attach_team_handoff(context, state["team_handoff"])
        state["last_result"] = {"active_skill_id": str(context.interaction_state.get("active_skill") or "")}
        if team is not None and definition.agent_id != team.coordinator_expert_id:
            self._event(context, "team_member_completed", {"team_id": team.team_id, "expert_id": definition.agent_id})
        self._event(context, "expert_completed", {"expert_id": definition.agent_id, "active_skill_id": state["last_result"]["active_skill_id"]})
        return result

    def _run_agent(self, definition: ExpertDefinition, user_message: str, context, client, state: dict[str, Any], *, team: ExpertTeamDefinition | None = None) -> None:
        from agentscope.agent import Agent, ReActConfig
        from agentscope.message import UserMsg
        from agentscope.permission import PermissionBehavior, PermissionDecision
        from agentscope.tool import FunctionTool, Toolkit

        runtime = self

        class TrustedFunctionTool(FunctionTool):
            async def check_permissions(self, *_args, **_kwargs):
                return PermissionDecision(behavior=PermissionBehavior.ALLOW, message="Hailiang 专家包已授权")

        def execute_skill(skill_id: str, task: str, handoff_context: dict[str, Any] | None = None) -> dict[str, Any]:
            """Authorize one selected runtime Skill for the current user task."""
            return runtime._execute_skill(definition, state, context, skill_id, task, handoff_context)

        def request_declared_form(skill_id: str, question_ids: list[str]) -> dict[str, Any]:
            """Request only question IDs declared by the selected Skill's questionnaire."""
            return runtime._request_declared_form(definition, state, context, skill_id, question_ids)

        def read_effective_facts() -> dict[str, Any]:
            """Read the current session's effective facts; this tool never writes facts."""
            return runtime._read_effective_facts(context)

        def propose_member_handoff(candidate_expert_ids: list[str], reason: str) -> dict[str, Any]:
            """Ask the user to choose one to three team members; never transfers automatically."""
            if team is None or not runtime._can_propose_team_handoff(team, definition.agent_id):
                raise ValueError("只有主协调专家可以建议转交")
            return runtime._propose_member_handoff(team, state, context, candidate_expert_ids, reason)

        toolkit = Toolkit()
        all_tools = {
            "execute_skill": TrustedFunctionTool(execute_skill, is_concurrency_safe=False),
            "request_declared_form": TrustedFunctionTool(request_declared_form, is_read_only=True),
            "read_effective_facts": TrustedFunctionTool(read_effective_facts, is_read_only=True),
        }
        enabled_capabilities = set(definition.capabilities)
        if team is not None and self._can_propose_team_handoff(team, definition.agent_id):
            all_tools["propose_member_handoff"] = TrustedFunctionTool(propose_member_handoff, is_concurrency_safe=False)
            enabled_capabilities.add("propose_member_handoff")
        model = _HailiangChatModel(client)
        catalog = self._catalog(definition)
        team_prompt = ""
        if team is not None:
            roster = "\n".join(
                f"- {member.expert_id}（@{member.mention_name}）：{member.routing_brief}"
                for member in team.members
            )
            if definition.agent_id == team.coordinator_expert_id:
                team_prompt = (
                    f"\n\n# 专家团规则\n你是“{team.name}”的主协调专家。\n{team.rules_markdown}\n"
                    f"# 团内专家\n{roster}\n"
                    "需要其他成员处理时，调用 propose_member_handoff；只给团内候选和简短原因，"
                    "不得自动转交、不得调用成员的 Skill。调用后必须立即输出简短的用户说明，"
                    "请用户点击转交卡确认；不要再次调用该工具或继续推理。"
                )
            else:
                team_prompt = (
                    f"\n\n# 专家团接管规则\n你正在“{team.name}”中作为成员接管对话。"
                    "只处理自己的职责和 Skill；禁止推荐、列出、调用或转交给其他专家。"
                    "超出边界时，仅提示用户可通过专家工具栏选择主协调专家；"
                    "不要提示用户在输入框中手动 @ 专家。"
                )
        system_prompt = (
            f"你是 {definition.name}。只能使用受控工具，不能读取文件、执行 Shell、安装工具或修改事实。\n"
            "先根据业务规则和有效事实判断；需要专项能力时调用 execute_skill。"
            "每次 execute_skill 必须传已选 Skill ID 和用户任务，且不得超过预算。\n\n"
            f"# 专家规则\n{definition.rules_markdown}\n\n# 授权 Skill 目录\n{catalog}{team_prompt}"
        )
        async def run_agent():
            await toolkit.add_tool([tool for name, tool in all_tools.items() if name in enabled_capabilities])
            agent = Agent(
                name=definition.agent_id,
                system_prompt=system_prompt,
                model=model,
                toolkit=toolkit,
                react_config=ReActConfig(max_iters=definition.max_iters),
            )
            return await agent.reply(UserMsg("user", user_message))

        reply = _run_async(run_agent())
        state["agent_reply"] = reply.get_text_content()[:1000]
        self._event(context, "expert_agent_completed", {"expert_id": definition.agent_id, "tool_calls": state["budget"]["skill_calls"]})

    def _execute_skill(self, definition: ExpertDefinition, state: dict[str, Any], context, skill_id: str, task: str, handoff_context: dict[str, Any] | None) -> dict[str, Any]:
        skill_id = str(skill_id or "").strip()
        if skill_id not in definition.authorized_skill_ids:
            raise ValueError(f"未授权 Skill: {skill_id}")
        budget = state["budget"]
        if budget["skill_calls"] >= budget["max_skill_calls"]:
            raise ValueError("已达到本轮 Skill 调用上限")
        observation: SkillObservation = self.native_executor.observe(skill_id, str(task or ""), handoff_context)
        budget["skill_calls"] += 1
        state["selected_skill_id"] = skill_id
        state["handoff_summary"] = observation.handoff_summary or observation.summary
        state.setdefault("call_trace", []).append(
            {"skill_id": skill_id, "task": str(task or "")[:500], "summary": observation.summary}
        )
        # Existing session state remains authoritative. This is only an
        # ephemeral routing hint consumed by MainPlannerOrchestrator.
        context.session_meta["expert_requested_skill_id"] = skill_id
        self._event(context, "expert_skill_executed", {"expert_id": definition.agent_id, "skill_id": skill_id})
        return {"status": "scheduled", "skill_id": skill_id, "summary": observation.summary}

    def _request_declared_form(self, definition: ExpertDefinition, state: dict[str, Any], context, skill_id: str, question_ids: list[str]) -> dict[str, Any]:
        if skill_id not in definition.authorized_skill_ids:
            raise ValueError(f"未授权 Skill: {skill_id}")
        # The native questionnaire remains the sole schema parser. Keeping the
        # request as a hint prevents model-invented labels/options from ever
        # reaching fact_form.
        ids = [str(item).strip() for item in question_ids if str(item).strip()]
        bundle = self.runtime_registry.get(skill_id)
        from hailiang_skills.runtime_bridge.native_questionnaire import question_specs, questionnaire_config

        # The native questionnaire will additionally apply the current-page
        # constraint when it renders a sequential assessment.  At this layer
        # we only need to reject model-invented IDs before the legacy executor
        # has reconstructed its authoritative SessionState.
        declared = {str(item.get("question_id") or "") for item in question_specs(bundle)} if bundle else set()
        invalid = sorted(set(ids) - declared)
        if invalid:
            raise ValueError(f"题库未声明的问题: {', '.join(invalid)}")
        max_fields = int(questionnaire_config(bundle).get("max_fields_per_form") or 1) if bundle else 1
        if len(ids) > max_fields:
            raise ValueError(f"单次表单最多请求 {max_fields} 个问题")
        state["pending_form"] = {"skill_id": skill_id, "question_ids": ids}
        context.session_meta["expert_requested_skill_id"] = skill_id
        self._event(context, "expert_form_requested", {"skill_id": skill_id, "question_ids": ids})
        return {"status": "deferred_to_native_questionnaire", "skill_id": skill_id, "question_ids": ids}

    def _propose_member_handoff(self, team: ExpertTeamDefinition, state: dict[str, Any], context, candidate_expert_ids: list[str], reason: str) -> dict[str, Any]:
        ids: list[str] = []
        for item in candidate_expert_ids:
            expert_id = str(item or "").strip()
            if expert_id and expert_id not in ids:
                ids.append(expert_id)
        if not ids or len(ids) > 3:
            raise ValueError("每次必须推荐一至三位专家")
        if any(expert_id not in team.member_expert_ids for expert_id in ids):
            raise ValueError("候选专家必须属于当前专家团")
        if team.coordinator_expert_id in ids:
            raise ValueError("主协调专家应直接回答，不能作为转交候选")
        candidates = []
        for expert_id in ids:
            member = team.member_for_expert(expert_id)
            definition = self.expert_registry.require(expert_id)
            candidates.append({
                "expert_id": expert_id,
                "name": definition.name,
                "mention_name": member.mention_name if member else definition.name,
                "brief": member.routing_brief if member else "",
            })
        handoff = {
            "handoff_id": f"handoff_{uuid4().hex[:16]}",
            "status": "active",
            "team_id": team.team_id,
            "source_message_id": None,
            "candidates": candidates,
            "reason": str(reason or "").strip()[:400],
            "proposal_turn_id": str(state.get("turn_id") or ""),
            "proposed_by_expert_id": team.coordinator_expert_id,
        }
        state["team_handoff"] = handoff
        context.session_meta["pending_team_handoff"] = handoff
        callback = (context.session_meta or {}).get("team_handoff_callback")
        if callable(callback):
            callback(self._public_team_handoff(handoff))
        self._event(context, "team_handoff_proposed", {
            "team_id": team.team_id,
            "candidate_expert_ids": ids,
            "reason": handoff["reason"],
        })
        return {**handoff, "status": "awaiting_user_confirmation"}

    @staticmethod
    def _is_framework_failure_reply(reply: str) -> bool:
        normalized = str(reply or "").strip().lower()
        return bool(normalized) and any(marker in normalized for marker in _FRAMEWORK_FAILURE_REPLY_MARKERS)

    @staticmethod
    def _team_handoff_reply(handoff: dict[str, Any]) -> str:
        candidates = handoff.get("candidates") or []
        names = [
            str(item.get("mention_name") or item.get("name") or "").strip()
            for item in candidates
            if isinstance(item, dict)
        ]
        names = [name for name in names if name]
        target = "、".join(names) or "合适的团内专家"
        reason = str(handoff.get("reason") or "这个问题更适合由专项专家继续处理。").strip()
        return f"我建议由{target}继续协助。{reason} 请点击下方转交卡确认后，由该专家接管回答。"

    @staticmethod
    def _can_propose_team_handoff(team: ExpertTeamDefinition, expert_id: str) -> bool:
        """Central policy hook for handoff proposal capabilities.

        v1 intentionally grants the capability only to the coordinator.  A
        future team policy can broaden this predicate without changing tool,
        state, SSE, or bundle-facing handoff contracts.
        """
        return expert_id == team.coordinator_expert_id

    @staticmethod
    def _public_team_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
        return {
            "handoff_id": str(handoff.get("handoff_id") or ""),
            "status": str(handoff.get("status") or "active"),
            "team_id": str(handoff.get("team_id") or ""),
            "source_message_id": handoff.get("source_message_id"),
            "candidates": list(handoff.get("candidates") or []),
            "reason": str(handoff.get("reason") or ""),
            "proposed_by_expert_id": str(handoff.get("proposed_by_expert_id") or ""),
        }

    def _current_team(self, context) -> ExpertTeamDefinition | None:
        team_id = str(getattr(context, "session_meta", {}).get("expert_team_id") or "").strip()
        return self.team_registry.require(team_id) if team_id else None

    def _apply_structured_team_switch(self, context, team: ExpertTeamDefinition | None, user_message: str) -> str:
        switch = context.session_meta.pop("team_member_switch", None)
        if not isinstance(switch, dict):
            # User text, including text beginning with '@', is ordinary
            # dialogue. Expert routing only accepts a structured expert ID.
            return user_message
        if team is None or self._has_pending_native_questionnaire(context):
            return user_message
        target_expert_id = str(switch.get("target_expert_id") or "").strip()
        member = team.member_for_expert(target_expert_id)
        if member is None:
            raise ValueError("目标专家不属于当前专家团")
        context.session_meta["active_expert_id"] = member.expert_id
        context.session_meta["expert_id"] = member.expert_id
        context.session_meta.pop("pending_team_handoff", None)
        context.session_meta["team_handoff_visible_user_message"] = str(
            switch.get("visible_user_message") or f"@{member.mention_name}"
        )
        state = context.skill_states.setdefault(AGENT_RUNTIME_STATE_KEY, {})
        if isinstance(state, dict):
            state["active_expert_id"] = member.expert_id
        source = str(switch.get("source") or "toolbar")
        self._event(context, "team_member_switched", {
            "team_id": team.team_id,
            "expert_id": member.expert_id,
            "source": source,
            "from_expert_id": str(switch.get("from_expert_id") or ""),
        })
        excerpt = str(switch.get("conversation_excerpt") or "").strip()
        if source == "team_handoff":
            source_question = str(switch.get("source_user_message") or "").strip()
            reason = str(switch.get("coordinator_reason") or "").strip()
            return (
                "主协调专家已征得用户确认，请接管并回答以下原始问题：\n"
                f"{source_question}\n"
                f"主协调说明：{reason or '该问题更适合由你处理。'}"
                + (f"\n最近会话摘录：\n{excerpt}" if excerpt else "")
            )
        content = str(switch.get("content") or user_message).strip()
        return (
            f"用户通过专家工具栏指定你接管。当前问题：\n{content}"
            + (f"\n最近会话摘录：\n{excerpt}" if excerpt else "")
        )

    @staticmethod
    def _attach_team_handoff(context, handoff: dict[str, Any]) -> None:
        for message in reversed(getattr(context, "messages", [])):
            if message.get("role") != "assistant":
                continue
            handoff = AgentScopeExpertRuntime._public_team_handoff(handoff)
            handoff["source_message_id"] = str(message.get("message_id") or "") or None
            message["team_handoff"] = handoff
            metadata = message.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["team_handoff"] = handoff
            from hailiang_skills.core.message_interactions import ensure_message_interactions
            ensure_message_interactions(message)
            return

    @staticmethod
    def _read_effective_facts(context) -> dict[str, Any]:
        def values(facts):
            return {
                key: record.value
                for key, record in getattr(facts, "facts", {}).items()
            }
        return {
            "profile": values(getattr(context, "profile_facts", None)),
            "session": values(getattr(context, "session_facts", None)),
            "shared": values(getattr(context, "shared_facts", None)),
        }

    @staticmethod
    def _has_pending_native_questionnaire(context) -> bool:
        runtime = getattr(context, "skill_states", {}).get("skill_runtime", {})
        if not isinstance(runtime, dict):
            return False
        active_skill_id = str(runtime.get("active_skill_id") or "")
        skill_facts = runtime.get("skill_facts", {})
        if not active_skill_id or not isinstance(skill_facts, dict):
            return False
        active_facts = skill_facts.get(active_skill_id, {})
        return isinstance(active_facts, dict) and isinstance(
            active_facts.get("_pending_questionnaire"), dict
        )

    @staticmethod
    def _is_supported_client(client) -> bool:
        """Do not consume legacy/test clients through the AgentScope adapter."""
        if client is None:
            return False
        try:
            from hailiang_skills.skill_runtime.llm_client import OpenAICompatibleChatClient
        except ImportError:  # pragma: no cover - import failure is reported by health
            return False
        return isinstance(client, OpenAICompatibleChatClient)

    def _catalog(self, definition: ExpertDefinition) -> str:
        lines = []
        for skill_id in definition.authorized_skill_ids:
            bundle = self.runtime_registry.get(skill_id)
            if bundle is None:
                continue
            meta = bundle.runtime_metadata
            lines.append(f"- {skill_id}: {meta.name or skill_id}。{meta.description or ''}")
        return "\n".join(lines)

    @staticmethod
    def _state(context, definition: ExpertDefinition, *, team: ExpertTeamDefinition | None = None) -> dict[str, Any]:
        state = context.skill_states.setdefault(AGENT_RUNTIME_STATE_KEY, {})
        state["expert_id"] = definition.agent_id
        state["expert_name"] = definition.name
        state["topology"] = definition.topology
        state["active_expert_id"] = definition.agent_id
        if team is not None:
            state["expert_team_id"] = team.team_id
            state["coordinator_expert_id"] = team.coordinator_expert_id
            state["topology"] = "team"
        state.setdefault("call_trace", [])
        state.setdefault("handoff_summary", "")
        return state

    def _event(self, context, event_type: str, payload: dict[str, Any]) -> None:
        from hailiang_skills.core.logging import make_event

        event = make_event(event_type, payload)
        if callable(self.event_recorder):
            self.event_recorder(context, [event])
            return
        trace = getattr(context, "event_trace", None)
        if isinstance(trace, list):
            trace.append(event)


class _HailiangChatModel:
    """A small AgentScope model adapter reusing Hailiang's LLM client/rate limits."""

    def __new__(cls, client):
        from agentscope.credential import OpenAICredential
        from agentscope.model import ChatModelBase

        config = client._config

        class Model(ChatModelBase):
            async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
                del model_name, tool_choice, kwargs
                from agentscope.message import TextBlock, ToolCallBlock
                from agentscope.model import ChatResponse
                from hailiang_skills.skill_runtime.models import ChatMessage, ToolCallRequest, ToolSpec

                translated: list[ChatMessage] = []
                for message in messages:
                    text = message.get_text_content() or ""
                    calls = []
                    for block in message.get_content_blocks("tool_call"):
                        try:
                            arguments = json.loads(block.input or "{}")
                        except json.JSONDecodeError:
                            arguments = {}
                        calls.append(ToolCallRequest(id=block.id, name=block.name, arguments=arguments))
                    result_blocks = message.get_content_blocks("tool_result")
                    if message.role == "assistant":
                        translated.append(ChatMessage(role="assistant", content=text, tool_calls=tuple(calls)))
                    else:
                        translated.append(ChatMessage(role=message.role, content=text))
                    # AgentScope represents a tool result as a content block
                    # in some versions and as a separate tool Msg in others.
                    # Normalize both forms to the existing OpenAI-compatible
                    # protocol so a second ReAct iteration sees the result.
                    for block in result_blocks:
                        output = block.output if isinstance(block.output, str) else " ".join(item.text for item in block.output if hasattr(item, "text"))
                        translated.append(ChatMessage(role="tool", content=output, tool_call_id=block.id, name=block.name))
                specs = []
                for tool in tools or []:
                    function = tool.get("function", {}) if isinstance(tool, dict) else {}
                    specs.append(ToolSpec(name=str(function.get("name") or ""), description=str(function.get("description") or ""), parameters_schema=function.get("parameters") or {}, enabled=True))
                result = await asyncio.to_thread(client.complete_with_tools, translated, specs, preferred_mode="native", request_purpose="agentscope_expert")
                if result.tool_calls:
                    return ChatResponse(content=[ToolCallBlock(id=item.id, name=item.name, input=json.dumps(item.arguments, ensure_ascii=False)) for item in result.tool_calls], is_last=True)
                return ChatResponse(content=[TextBlock(text=result.final_text or "")], is_last=True)

        return Model(
            credential=OpenAICredential(api_key=SecretStr(config.api_key), base_url=config.base_url),
            model=config.model,
            parameters=_AgentScopeParameters(),
            stream=False,
        )


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # FastAPI's sync handlers should not have a running loop. If they do, make
    # the limitation explicit rather than blocking the loop or silently
    # spawning an untracked task.
    raise AgentScopeRuntimeUnavailable("同步专家运行时不能在活动 asyncio loop 内运行")
