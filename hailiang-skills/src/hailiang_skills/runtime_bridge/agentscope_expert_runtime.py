"""AgentScope-backed, permission-limited Expert runtime (single expert v1)."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, SecretStr

from hailiang_skills.runtime_bridge.expert_bundle import ExpertDefinition, ExpertRegistry, LockedSkill
from hailiang_skills.runtime_bridge.expert_team_bundle import ExpertTeamDefinition, ExpertTeamMember, ExpertTeamRegistry
from hailiang_skills.runtime_bridge.expert_models import ExpertMember, SkillObservation
from hailiang_skills.runtime_bridge.native_skill_executor import NativeSkillExecutor


AGENT_RUNTIME_STATE_KEY = "agent_runtime"
DEFAULT_EXPERT_ID = "career_plan_expert"
_FRAMEWORK_FAILURE_REPLY_MARKERS = (
    "maximum reasoning-acting iterations",
    "maximum reasoning acting iterations",
    "max iterations",
)

# These are deliberately narrow business-domain hints for the currently
# supported student-growth expert team. Other teams still use their saved
# routing briefs and the coordinator's model decision.
_STUDENT_GROWTH_HANDOFF_HINTS: dict[str, tuple[str, ...]] = {
    "academic_coach": ("提分", "学习方法", "数学", "语文", "英语", "物理", "化学", "备考", "成绩", "学习习惯"),
    "talent_dev_specialist": ("绘画", "画画", "美术", "特长", "才艺", "兴趣班", "艺术", "音乐", "舞蹈"),
    "career_explore_mentor": ("职业", "专业方向", "未来方向", "生涯", "做什么工作"),
    "study_abroad_consultant": ("留学", "出国", "海外院校", "国外大学", "国际学校"),
    "admission_specialist": ("升学", "志愿", "报考", "院校", "综评", "高考", "中考", "录取"),
}


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
        history_messages: int = 12,
        history_message_chars: int = 1_500,
        history_max_chars: int = 6_000,
        reply_max_chars: int = 1_000,
    ) -> None:
        self.expert_registry = expert_registry
        self.team_registry = team_registry or ExpertTeamRegistry(definitions={})
        self.runtime_registry = runtime_registry
        self.default_expert_id = default_expert_id
        self.client_factory = client_factory
        self.event_recorder = event_recorder
        self.history_messages = max(1, int(history_messages))
        self.history_message_chars = max(1, int(history_message_chars))
        self.history_max_chars = max(1, int(history_max_chars))
        self.reply_max_chars = max(1_000, int(reply_max_chars))
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
        team = self._configured_team(context, self._current_team(context))
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
        base_definition = self.expert_registry.get(requested_expert_id) or self._expert_from_snapshot(context, requested_expert_id)
        if base_definition is None:
            raise ValueError(f"专家不存在: {requested_expert_id}")
        definition = self._configured_expert(context, base_definition)
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
        state["handoff_tool_calls"] = 0
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

        # An Expert owns the decision on *every* turn, including Experts with
        # only one locked Skill.  A unique dependency is an authorization
        # boundary, never proof that the Skill is appropriate for the message:
        # AGENT.md may require a direct answer, a clarification, or a bounded
        # Skill handoff.
        client = self.client_factory(context) if self.client_factory else None
        if not self._is_supported_client(client):
            self._event(context, "expert_decision_unavailable", {
                "expert_id": definition.agent_id,
                "reason": "llm_client_unavailable",
            })
            raise AgentScopeRuntimeUnavailable("专家决策暂不可用，请稍后重试")
        try:
            self._run_agent(
                definition,
                user_message,
                context,
                client,
                state,
                team=team,
                routing_instruction=self._active_skill_routing_instruction(context, definition),
            )
        except Exception as exc:
            # Never bypass AGENT.md by silently falling back to a unique or
            # previously active Skill.  The caller can surface this as a
            # retryable turn failure while the trace remains auditable.
            state["agent_scope_error"] = str(exc)
            self._event(context, "expert_decision_unavailable", {
                "expert_id": definition.agent_id,
                "reason": "agent_execution_failed",
                "error": str(exc),
            })
            raise AgentScopeRuntimeUnavailable("专家决策暂不可用，请稍后重试") from exc

        # A coordinator must not silently turn a clearly-specialist request
        # into an unstructured direct reply merely because a ReAct tool call
        # was malformed or omitted. Reuse the same controlled handoff record
        # consumed by formal chat and candidate tests.
        if team is not None and self._can_propose_team_handoff(team, definition.agent_id):
            self._ensure_controlled_team_handoff(definition, team, user_message, context, state)

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
            # than accidentally falling through to legacy general_chat.  A
            # coordinator failure must also not expose the framework's generic
            # apology: it remains responsible for obtaining enough routing
            # information from the user.
            if team is not None and self._can_propose_team_handoff(team, definition.agent_id):
                agent_reply = self._generate_team_clarification_reply(
                    definition,
                    team,
                    user_message,
                    context,
                    client,
                )
                self._event(context, "team_handoff_clarification", {
                    "team_id": team.team_id,
                    "expert_id": definition.agent_id,
                    "reason": "framework_iteration_limit_without_specialist_match",
                })
            else:
                agent_reply = "抱歉，我这轮没有完成处理。请重试一次，或补充更具体的情况，我会继续协助你。"
            state["agent_reply"] = agent_reply
        # Do not expose a half-completed routing suggestion.  The UI can only
        # confirm a structured intent, never a name mentioned in prose.
        if (
            agent_reply
            and handoff is None
            and team is not None
            and self._can_propose_team_handoff(team, definition.agent_id)
            and self._contains_unstructured_team_routing_text(agent_reply, team)
        ):
            self._event(context, "team_handoff_text_suppressed", {
                "team_id": team.team_id, "expert_id": definition.agent_id,
            })
            agent_reply = self._generate_team_clarification_reply(definition, team, user_message, context, client)
            state["agent_reply"] = agent_reply
        has_native_handoff = bool(
            context.session_meta.get("expert_requested_skill_id")
            or state.get("pending_form")
            or self._has_pending_native_questionnaire(context)
        )
        if not agent_reply and not has_native_handoff:
            # A completed ReAct call that neither answered nor selected an
            # authorized native action is not a decision.  Letting the legacy
            # planner continue here would reintroduce the bypass this runtime
            # exists to prevent.
            self._event(context, "expert_decision_unavailable", {
                "expert_id": definition.agent_id,
                "reason": "empty_agent_decision",
            })
            raise AgentScopeRuntimeUnavailable("专家决策暂不可用，请稍后重试")
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

    def select_candidate_skill_switch(self, user_message: str, context, *, current_skill_id: str) -> str | None:
        """Ask the active Expert whether a candidate conversation changed task.

        This is deliberately a routing-only pass: it exposes only the
        Expert's already-authorized ``execute_skill`` tool and discards any
        natural-language answer.  Returning ``None`` means the current Skill
        continues untouched.  It is used by the workbench candidate runtime,
        where a direct Skill continuation would otherwise hide a semantic
        change from the Expert indefinitely.
        """
        team = self._configured_team(context, self._current_team(context))
        expert_id = str(
            getattr(context, "session_meta", {}).get("active_expert_id")
            or getattr(context, "session_meta", {}).get("expert_id")
            or (team.coordinator_expert_id if team else self.default_expert_id)
        ).strip()
        if team is not None and expert_id not in team.member_expert_ids:
            expert_id = team.coordinator_expert_id
        base_definition = self.expert_registry.get(expert_id) or self._expert_from_snapshot(context, expert_id)
        if base_definition is None or not self._available:
            return None
        definition = self._configured_expert(context, base_definition)
        client = self.client_factory(context) if self.client_factory else None
        if not self._is_supported_client(client):
            return None
        state = {
            "turn_id": f"candidate_route_probe_{uuid4().hex[:12]}",
            "budget": {
                "max_iters": min(definition.max_iters, 3),
                "max_skill_calls": 1,
                "skill_calls": 0,
            },
            "pending_form": None,
            "call_trace": [],
            "handoff_summary": "",
        }
        context.session_meta.pop("expert_requested_skill_id", None)
        instruction = (
            "\n# 候选测试中的续聊路由判断\n"
            f"当前正在执行 Skill：{current_skill_id}。本轮只判断用户是否已切换到另一个业务任务。"
            "只有当新任务明显不属于当前 Skill、且另一个授权 Skill 更匹配时，才调用 execute_skill。"
            "若仍在当前任务内（包括题目答案、追问、补充信息），不要调用任何工具，也不要给用户回答。"
        )
        try:
            self._run_agent(definition, user_message, context, client, state, team=team, routing_instruction=instruction)
        except Exception as exc:
            self._event(context, "candidate_skill_redispatch_deferred", {"expert_id": definition.agent_id, "error": str(exc)})
            return None
        selected = str(context.session_meta.pop("expert_requested_skill_id", "") or "")
        if selected and selected != current_skill_id and selected in definition.authorized_skill_ids:
            self._event(
                context,
                "candidate_skill_semantic_redispatch_selected",
                {"expert_id": definition.agent_id, "from_skill_id": current_skill_id, "to_skill_id": selected},
            )
            return selected
        return None

    @staticmethod
    def _active_skill_id(context) -> str:
        interaction = getattr(context, "interaction_state", {}) or {}
        active = str(interaction.get("active_skill") or "") if isinstance(interaction, dict) else ""
        if active:
            return active
        runtime = getattr(context, "skill_states", {}).get("skill_runtime", {})
        return str(runtime.get("active_skill_id") or "") if isinstance(runtime, dict) else ""

    def _active_skill_routing_instruction(self, context, definition: ExpertDefinition) -> str:
        """Inject the active Skill's resumable state into Expert policy."""
        active_skill_id = self._active_skill_id(context)
        if not active_skill_id or active_skill_id not in definition.authorized_skill_ids:
            return ""
        return (
            "\n# 当前会话的 Skill 路由规则\n"
            f"当前正在执行的已授权 Skill 是：{active_skill_id}。先判断用户本轮消息的业务意图。"
            "AGENT.md 是本轮选择的最高业务策略。若当前 Skill 的表单、阶段或上下文仍能处理本轮，"
            "调用 execute_skill 并传入当前 Skill；短选项、追问和补充资料通常属于这一类。"
            "若另一已授权 Skill 更适合本轮，即使问题与当前 Skill 有部分重叠，也可调用 execute_skill "
            "选择该 Skill，系统会自动完成内部切换，不得要求用户点击按钮。"
            "如 AGENT.md 要求由专家直接回答，则不要调用 Skill。只能从上方授权 Skill 目录中选择。"
        )

    @staticmethod
    def _snapshot_entry(context, object_type: str, object_key: str) -> dict[str, Any] | None:
        snapshot = (getattr(context, "session_meta", {}) or {}).get("configuration_snapshot")
        entries = snapshot.get("entries", []) if isinstance(snapshot, dict) else []
        return next(
            (
                item for item in entries
                if isinstance(item, dict)
                and item.get("object_type") == object_type
                and str(item.get("object_key") or "") == object_key
                and isinstance(item.get("payload"), dict)
            ),
            None,
        )

    def _snapshot_expert_name(self, context, expert_id: str) -> str:
        entry = self._snapshot_entry(context, "expert", expert_id)
        return str((entry or {}).get("name") or "").strip()

    def _expert_history_messages(self, context) -> list[dict[str, str]]:
        """Return bounded visible dialogue for an Expert model invocation."""
        history: list[dict[str, str]] = []
        for item in getattr(context, "messages", []) or []:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            if metadata.get("hidden") or metadata.get("message_type") in {
                "skill_transition_command",
                "team_handoff_confirmation",
            }:
                continue
            content = str(item.get("content") or "").strip()
            if content:
                history.append({"role": str(item["role"]), "content": content[:self.history_message_chars]})
        return history[-self.history_messages :]

    def _expert_conversation_history(self, context) -> str:
        history = self._expert_history_messages(context)
        if not history:
            return "（暂无历史对话）"
        labels = {"user": "用户", "assistant": "助手"}
        rendered = "\n".join(f"{labels[item['role']]}：{item['content']}" for item in history)
        return rendered[-self.history_max_chars :]

    def _configured_expert(self, context, definition: ExpertDefinition) -> ExpertDefinition:
        entry = self._snapshot_entry(context, "expert", definition.agent_id)
        if entry is None:
            return definition
        payload = entry["payload"]
        locks = tuple(
            LockedSkill(str(item.get("object_key") or ""), f"v{int(item.get('release_no') or 0)}")
            for item in entry.get("dependency_locks", [])
            if str(item.get("object_key") or "")
        )
        budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
        capabilities = tuple(str(item) for item in payload.get("capabilities", []) if str(item))
        return replace(
            definition,
            rules_markdown=str(payload.get("rules_markdown") or definition.rules_markdown),
            skills=locks or definition.skills,
            max_iters=max(1, min(int(budget.get("max_iters", definition.max_iters)), 4)),
            max_skill_calls=max(1, min(int(budget.get("max_skill_calls", definition.max_skill_calls)), 3)),
            capabilities=capabilities or definition.capabilities,
        )

    def _expert_from_snapshot(self, context, expert_id: str) -> ExpertDefinition | None:
        entry = self._snapshot_entry(context, "expert", expert_id)
        if entry is None:
            return None
        payload = entry["payload"]
        budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
        locks = tuple(
            LockedSkill(str(item.get("object_key") or ""), f"v{int(item.get('release_no') or 0)}")
            for item in entry.get("dependency_locks", [])
            if str(item.get("object_key") or "")
        )
        return ExpertDefinition(
            agent_id=expert_id,
            name=str(entry.get("name") or expert_id),
            rules_markdown=str(payload.get("rules_markdown") or ""),
            skills=locks,
            brief=str(payload.get("brief") or "").strip(),
            max_iters=max(1, min(int(budget.get("max_iters", 4)), 4)),
            max_skill_calls=max(1, min(int(budget.get("max_skill_calls", 3)), 3)),
            capabilities=tuple(str(item) for item in payload.get("capabilities", []) if str(item)) or (
                "execute_skill", "request_declared_form", "read_effective_facts",
            ),
        )

    def _configured_team(self, context, team: ExpertTeamDefinition | None) -> ExpertTeamDefinition | None:
        if team is None:
            return None
        entry = self._snapshot_entry(context, "expert_team", team.team_id)
        if entry is None:
            return team
        payload = entry["payload"]
        locks = entry.get("dependency_locks", [])
        coordinator_object_id = str(payload.get("coordinator_expert_id") or "")
        coordinator_expert_id = next(
            (str(item.get("object_key") or "") for item in locks if str(item.get("object_id") or "") == coordinator_object_id),
            team.coordinator_expert_id,
        )
        member_payloads = payload.get("members") if isinstance(payload.get("members"), list) else []
        by_expert = {str(item.get("expert_id") or ""): item for item in member_payloads if isinstance(item, dict)}
        members = tuple(
            ExpertTeamMember(
                expert_id=str(item.get("object_key") or ""),
                mention_name=str(
                    (by_expert.get(str(item.get("object_key") or "")) or {}).get("mention_name")
                    or self._snapshot_expert_name(context, str(item.get("object_key") or ""))
                    or item.get("object_key")
                    or ""
                ),
                routing_brief=str((by_expert.get(str(item.get("object_key") or "")) or {}).get("routing_brief") or ""),
            )
            for item in locks
            if str(item.get("object_key") or "")
        )
        return replace(
            team,
            brief=str(payload.get("brief") or team.brief).strip(),
            rules_markdown=str(payload.get("rules_markdown") or team.rules_markdown),
            coordinator_expert_id=coordinator_expert_id,
            members=members or team.members,
        )

    def _run_agent(
        self,
        definition: ExpertDefinition,
        user_message: str,
        context,
        client,
        state: dict[str, Any],
        *,
        team: ExpertTeamDefinition | None = None,
        routing_instruction: str = "",
    ) -> None:
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

        def record_candidate_fact(fact_key: str, value: str, confidence: float, evidence_summary: str) -> dict[str, Any]:
            """Store a tentative, current-child-only profile observation for later natural confirmation."""
            return runtime._record_candidate_fact(
                context,
                definition,
                source_turn_id=str(state.get("turn_id") or "") or None,
                fact_key=fact_key,
                value=value,
                confidence=confidence,
                evidence_summary=evidence_summary,
            )

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
            "record_candidate_fact": TrustedFunctionTool(record_candidate_fact, is_concurrency_safe=False),
        }
        enabled_capabilities = set(definition.capabilities)
        enabled_capabilities.add("record_candidate_fact")
        if team is not None and self._can_propose_team_handoff(team, definition.agent_id):
            all_tools["propose_member_handoff"] = TrustedFunctionTool(propose_member_handoff, is_concurrency_safe=False)
            enabled_capabilities.add("propose_member_handoff")
        model = _HailiangChatModel(
            client,
            completion_recorder=lambda result, metrics: runtime._record_model_completion(
                context,
                result=result,
                metrics=metrics,
                source="expert_agent",
                expert_id=definition.agent_id,
                expert_turn_id=str(state.get("turn_id") or ""),
            ),
        )
        catalog = self._catalog(definition)
        # AgentScope does not implicitly receive SessionContext. Previously
        # the expert was merely given a tool *capable* of reading Facts, which
        # allowed a first-turn greeting to skip the tool and ask again for a
        # child's already-known grade. The active profile branch is isolated
        # before this method runs, so this snapshot is both safe to inject and
        # authoritative for the current turn.
        from hailiang_skills.core.profile_candidate_archive import candidate_archive
        effective_facts = json.dumps(
            {
                "confirmed_facts": self._read_effective_facts(context),
                "profile_candidate_archive": candidate_archive(context),
            },
            ensure_ascii=False,
            default=str,
        )
        conversation_history = self._expert_conversation_history(context)
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
                    "每次收到新的用户消息，都必须重新判断当前问题是否更适合团内成员；上一轮转交卡未点击，"
                    "也不能跳过本轮判断。只要某个成员比你更适合处理，必须调用 propose_member_handoff，"
                    "只给团内候选和简短原因，不得自动转交、不得调用成员的 Skill。调用后必须立即输出简短的用户说明，"
                    "请用户确认由哪位候选专家承接；不要描述任何卡片或控件的位置，也不要再次调用该工具或继续推理。未调用该工具时，禁止在正文中输出"
                    "@专家名称、建议由某专家承接或已经转交等表达。"
                )
            else:
                team_prompt = (
                    f"\n\n# 专家团接管规则\n你正在“{team.name}”中作为成员接管对话。"
                    "只处理自己的职责和 Skill；禁止推荐、列出、调用或转交给其他专家。"
                    "超出边界时，仅提示用户可通过专家工具栏选择主协调专家；"
                    "不要提示用户在输入框中手动 @ 专家。"
                )
        system_prompt = (
            f"你是 {definition.name}。只能使用受控工具，不能读取文件、执行 Shell 或安装工具；只有 record_candidate_fact 可写入候选档案。\n"
            "先根据业务规则和下方已注入的有效事实判断；需要专项能力时调用 execute_skill。"
            "当用户表达了与孩子相关、可在未来复用但尚不应视为确定结论的特质、偏好或倾向时，可调用 record_candidate_fact 保存候选观察；"
            "必须使用简短语义键、忠实的证据摘要和 0 到 1 的置信度，不得把候选当作已确认事实。"
            "不得重复询问下方已经有明确值的资料（例如年级、学年）；只有资料缺失或存在冲突时才追问。\n"
            f"\n# 当前孩子的上下文事实\n{effective_facts}\n"
            "候选档案不是已确认事实；请只在当前问题确实相关时，以自然方式决定是否确认、更新或忽略，"
            "不得把候选内容直接当成结论，也不得照抄固定确认话术。\n"
            f"\n# 最近对话（按时间顺序，仅用于保持上下文）\n{conversation_history}\n"
            "每次 execute_skill 必须传已选 Skill ID 和用户任务，且不得超过预算。\n\n"
            f"# 专家规则\n{definition.rules_markdown}\n\n# 授权 Skill 目录\n{catalog}{team_prompt}{routing_instruction}"
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
        state["agent_reply"] = self._limit_reply(
            reply.get_text_content(),
            context=context,
            source="expert_agent_reply",
            expert_id=definition.agent_id,
            expert_turn_id=str(state.get("turn_id") or ""),
        )
        self._event(context, "expert_agent_completed", {"expert_id": definition.agent_id, "tool_calls": state["budget"]["skill_calls"], "handoff_tool_calls": int(state.get("handoff_tool_calls") or 0), "structured_handoff": isinstance(state.get("team_handoff"), dict)})

    def _record_candidate_fact(
        self,
        context,
        definition: ExpertDefinition,
        *,
        source_turn_id: str | None,
        fact_key: str,
        value: str,
        confidence: float,
        evidence_summary: str,
    ) -> dict[str, Any]:
        """Store model-inferred evidence without promoting it to a business fact."""
        key = str(fact_key or "").strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            raise ValueError("候选事实键必须是 1 到 64 位的小写字母、数字或下划线，且以字母开头")
        text = str(value or "").strip()
        if not text:
            raise ValueError("候选事实值不能为空")
        try:
            normalized_confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("候选事实置信度必须是 0 到 1 的数字") from exc
        if not 0 <= normalized_confidence <= 1:
            raise ValueError("候选事实置信度必须在 0 到 1 之间")
        summary = str(evidence_summary or "").strip()
        if not summary:
            raise ValueError("候选事实必须提供简短证据摘要")

        from hailiang_skills.core.profile_candidate_archive import archive_candidate

        archive_key = f"conversation.{definition.agent_id}.{key}"
        archived = archive_candidate(
            context,
            key=archive_key,
            value=text[:500],
            source_skill=definition.agent_id,
            source_turn_id=source_turn_id,
            evidence_summary=summary[:500],
            confidence=normalized_confidence,
        )
        self._event(context, "expert_candidate_fact_recorded", {
            "expert_id": definition.agent_id,
            "fact_key": archive_key,
            "archived": archived,
            "confidence": normalized_confidence,
        })
        return {
            "status": "candidate_archived" if archived else "not_archived_unbound_context",
            "fact_key": archive_key,
            "confidence": normalized_confidence,
        }

    def _execute_skill(self, definition: ExpertDefinition, state: dict[str, Any], context, skill_id: str, task: str, handoff_context: dict[str, Any] | None) -> dict[str, Any]:
        skill_id = str(skill_id or "").strip()
        if skill_id not in definition.authorized_skill_ids:
            self._event(context, "expert_skill_handoff_rejected", {
                "expert_id": definition.agent_id,
                "skill_id": skill_id,
                "reason": "not_authorized",
            })
            raise ValueError(f"未授权 Skill: {skill_id}")
        budget = state["budget"]
        if budget["skill_calls"] >= budget["max_skill_calls"]:
            raise ValueError("已达到本轮 Skill 调用上限")
        previous_skill_id = self._active_skill_id(context)
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
        self._event(context, "expert_skill_executed", {
            "expert_id": definition.agent_id,
            "skill_id": skill_id,
            "source_skill_id": previous_skill_id or None,
        })
        if previous_skill_id and previous_skill_id != skill_id:
            self._event(context, "expert_skill_handoff_confirmed", {
                "expert_id": definition.agent_id,
                "from_skill_id": previous_skill_id,
                "to_skill_id": skill_id,
                "handoff_summary": state["handoff_summary"][:500],
            })
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
        state["handoff_tool_calls"] = int(state.get("handoff_tool_calls") or 0) + 1
        original_ids = [str(item or "").strip() for item in candidate_expert_ids]
        ids: list[str] = []
        rejected: list[dict[str, str]] = []
        for item in original_ids:
            expert_id, rejection = self._normalize_team_member_id(team, item, context=context)
            if rejection:
                rejected.append({"candidate": item, "reason": rejection})
                continue
            if expert_id and expert_id not in ids:
                ids.append(expert_id)
        if rejected or not ids or len(ids) > 3:
            details = {
                "team_id": team.team_id,
                "candidates": original_ids,
                "normalized_candidate_ids": ids,
                "rejected": rejected,
            }
            self._event(context, "team_handoff_rejected", details)
            raise ValueError("每次必须推荐一至三位专家")
        if any(expert_id not in team.member_expert_ids for expert_id in ids):
            self._event(context, "team_handoff_rejected", {
                "team_id": team.team_id, "candidates": original_ids,
                "normalized_candidate_ids": ids, "rejected": [{"reason": "not_team_member"}],
            })
            raise ValueError("候选专家必须属于当前专家团")
        if team.coordinator_expert_id in ids:
            self._event(context, "team_handoff_rejected", {
                "team_id": team.team_id, "candidates": original_ids,
                "normalized_candidate_ids": ids, "rejected": [{"reason": "coordinator_not_transfer_target"}],
            })
            raise ValueError("主协调专家应直接回答，不能作为转交候选")
        candidates = []
        for expert_id in ids:
            member = team.member_for_expert(expert_id)
            definition = self.expert_registry.get(expert_id) or self._expert_from_snapshot(context, expert_id)
            if definition is None:
                self._event(context, "team_handoff_rejected", {
                    "team_id": team.team_id,
                    "candidates": original_ids,
                    "normalized_candidate_ids": ids,
                    "rejected": [{"candidate": expert_id, "reason": "expert_definition_missing_from_current_configuration"}],
                })
                raise ValueError(f"当前配置快照缺少专家定义: {expert_id}")
            card_brief = next(
                (
                    value
                    for value in (
                        str(definition.brief or "").strip(),
                        str(member.routing_brief or "").strip() if member else "",
                        str(reason or "").strip(),
                    )
                    if value
                ),
                "",
            )
            candidates.append({
                "expert_id": expert_id,
                "name": definition.name,
                "mention_name": member.mention_name if member else definition.name,
                # Object brief is the user-facing expert introduction. The
                # member routing brief remains a routing-only configuration
                # but is a useful card fallback for legacy/incomplete expert
                # records; the coordinator's reason is the final fallback.
                "brief": card_brief,
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
        # Persist the routing decision before attempting any presentation.
        # A callback/SSE failure must never turn a valid specialist decision
        # into an unanswerable piece of coordinator prose.
        for message in reversed(getattr(context, "messages", [])):
            if isinstance(message, dict) and message.get("role") == "user" and not (message.get("metadata") or {}).get("hidden"):
                handoff["original_user_message"] = str(message.get("content") or "").strip()
                break
        handoff["presentation_status"] = "pending"
        state["team_handoff"] = handoff
        context.session_meta["pending_team_handoff"] = handoff
        context.session_meta["pending_team_handoff_intent"] = handoff
        callback = (context.session_meta or {}).get("team_handoff_callback")
        if callable(callback):
            try:
                callback(self._public_team_handoff(handoff))
            except Exception as exc:  # presentation is recoverable, routing is not
                handoff["presentation_status"] = "callback_failed"
                self._event(context, "team_handoff_presentation_deferred", {
                    "team_id": team.team_id, "handoff_id": handoff["handoff_id"], "reason": type(exc).__name__,
                })
        self._event(context, "team_handoff_proposed", {
            "team_id": team.team_id,
            "candidate_expert_ids": ids,
            "reason": handoff["reason"],
        })
        return {**handoff, "status": "awaiting_user_confirmation"}

    def _ensure_controlled_team_handoff(
        self,
        definition: ExpertDefinition,
        team: ExpertTeamDefinition,
        user_message: str,
        context,
        state: dict[str, Any],
    ) -> None:
        if isinstance(state.get("team_handoff"), dict):
            return
        candidates = self._controlled_team_handoff_candidates(team, user_message)
        if not candidates:
            return
        try:
            self._propose_member_handoff(
                team,
                state,
                context,
                candidates,
                "主协调专家已识别到该问题更适合由专项专家继续处理。",
            )
        except ValueError as exc:
            self._event(context, "team_handoff_controlled_fallback", {
                "team_id": team.team_id, "expert_id": definition.agent_id,
                "reason": "controlled_handoff_rejected", "error": str(exc),
            })
            return
        # Never let a previous direct answer or ReAct limit diagnostic replace
        # a valid confirmation card.
        state["agent_reply"] = ""
        state.pop("agent_reply_error", None)
        self._event(context, "team_handoff_controlled_selected", {
            "team_id": team.team_id,
            "expert_id": definition.agent_id,
            "candidate_expert_ids": candidates,
        })

    def _normalize_team_member_id(
        self,
        team: ExpertTeamDefinition,
        candidate: str,
        *,
        context=None,
    ) -> tuple[str, str]:
        """Resolve only a unique persisted member identity, never fuzzy text.

        Models frequently emit a user-facing Chinese name instead of the
        documented ID.  Accepting the exact saved mention, exact expert name,
        and either form with one leading ``@`` keeps the tool ergonomic while
        refusing an ambiguous alias rather than silently choosing a member.
        """
        value = str(candidate or "").strip().lstrip("@").strip()
        if not value:
            return "", "empty_candidate"
        normalized_value = value.casefold()
        matches: set[str] = set()
        for member in team.members:
            expert = self.expert_registry.get(member.expert_id)
            if expert is None and context is not None:
                expert = self._expert_from_snapshot(context, member.expert_id)
            aliases = {member.expert_id, member.mention_name}
            if expert is not None:
                aliases.add(expert.name)
            if any(normalized_value == str(alias or "").strip().casefold() for alias in aliases):
                matches.add(member.expert_id)
        if len(matches) == 1:
            return next(iter(matches)), ""
        if len(matches) > 1:
            return "", "ambiguous_member_alias"
        return "", "unknown_member_alias"

    @staticmethod
    def _controlled_team_handoff_candidates(team: ExpertTeamDefinition, user_message: str) -> list[str]:
        text = str(user_message or "").lower()
        scores: dict[str, int] = {}
        for member in team.members:
            if member.expert_id == team.coordinator_expert_id:
                continue
            hints = list(_STUDENT_GROWTH_HANDOFF_HINTS.get(member.expert_id, ()))
            hints.extend(
                token
                for token in re.split(r"[，,、；;\\n\\s]+", str(member.routing_brief or "").lower())
                if len(token) >= 2
            )
            scores[member.expert_id] = sum(1 for hint in hints if hint and hint.lower() in text)
        ranked = sorted((expert_id for expert_id, score in scores.items() if score > 0), key=lambda expert_id: (-scores[expert_id], expert_id))
        if not ranked:
            return []
        # A user may name more than one concrete domain in one sentence. Do
        # not let a richer routing brief for one member hide another explicit
        # domain: every positively matched member is a real candidate, bounded
        # by the card contract's three choices.
        return ranked[:3]

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
        return f"我建议由{target}继续协助。{reason} 请确认是否由该专家接管回答。"

    def _generate_team_clarification_reply(
        self,
        definition: ExpertDefinition,
        team: ExpertTeamDefinition,
        user_message: str,
        context,
        client,
    ) -> str:
        """Let the coordinator produce a contextual no-tool clarification.

        This is a second, bounded model pass used only when the ReAct loop
        ended without a valid handoff. It deliberately has no tools, so it
        cannot repeat the malformed call and must either clarify or answer
        within the coordinator's own boundary.
        """
        if not self._is_supported_client(client):
            raise AgentScopeRuntimeUnavailable("主协调专家无法生成兜底回复：模型客户端不可用")
        from hailiang_skills.skill_runtime.models import ChatMessage

        roster = "\n".join(
            f"- {member.mention_name}（{member.expert_id}）：{member.routing_brief or '未填写职责摘要'}"
            for member in team.members
            if member.expert_id != team.coordinator_expert_id
        )
        prompt = (
            f"你是“{team.name}”的主协调专家“{definition.name}”。\n"
            f"专家团规则：\n{team.rules_markdown}\n\n"
            f"团内专项专家：\n{roster}\n\n"
            "上一次受控分流没有完成。请根据最近对话和用户当前消息，亲自生成一条自然、简短、"
            "承接上下文的回复。如果专项方向仍不明确，只追问当前真正缺失的一项信息；"
            "不要机械罗列所有专家类别，不要声称已经转交，不要输出专家卡片或工具调用。"
        )
        messages = [ChatMessage(role="system", content=prompt)]
        for item in self._expert_history_messages(context):
            messages.append(ChatMessage(role=item["role"], content=item["content"]))
        messages.append(ChatMessage(role="user", content=str(user_message or "")))
        try:
            reply = str(client.complete(messages, request_purpose="team_coordinator_clarification") or "").strip()
        except Exception as exc:
            raise AgentScopeRuntimeUnavailable(f"主协调专家生成兜底回复失败: {exc}") from exc
        if not reply:
            raise AgentScopeRuntimeUnavailable("主协调专家生成兜底回复失败：模型返回为空")
        self._record_model_completion(
            context,
            result=None,
            metrics=client.last_request_metrics() if callable(getattr(client, "last_request_metrics", None)) else {},
            source="team_coordinator_clarification",
            expert_id=definition.agent_id,
            returned_chars=len(reply),
        )
        return self._limit_reply(
            reply,
            context=context,
            source="team_coordinator_clarification",
            expert_id=definition.agent_id,
        )

    def _limit_reply(
        self,
        reply: str,
        *,
        context=None,
        source: str = "expert_reply",
        expert_id: str = "",
        expert_turn_id: str = "",
    ) -> str:
        """Keep a configurable emergency ceiling without silently using 1k chars."""
        text = str(reply or "")
        limited = text[:self.reply_max_chars]
        if context is not None and len(limited) < len(text):
            self._event(context, "model_output_truncated", {
                "source": source,
                "expert_id": expert_id or None,
                "expert_turn_id": expert_turn_id or None,
                "truncation_reason_code": "application_reply_char_limit",
                "truncation_reason": "应用层专家回复保护上限截断",
                "configured_reply_max_chars": self.reply_max_chars,
                "received_chars": len(text),
                "returned_chars": len(limited),
            })
        return limited

    def _record_model_completion(
        self,
        context,
        *,
        result,
        metrics: dict[str, Any] | None,
        source: str,
        expert_id: str,
        expert_turn_id: str = "",
        returned_chars: int | None = None,
    ) -> None:
        """Persist provider completion evidence without recording reply content."""
        metrics = metrics if isinstance(metrics, dict) else {}
        finish_reason = str(metrics.get("finish_reason") or "").strip() or None
        final_text = str(getattr(result, "final_text", "") or "")
        limit_reasons = {"length", "max_tokens", "max_token", "token_limit"}
        truncated = bool(finish_reason and finish_reason.lower() in limit_reasons)
        truncation_status = "confirmed" if truncated else ("not_reported" if finish_reason is None else "not_truncated")
        payload = {
            "source": source,
            "expert_id": expert_id or None,
            "expert_turn_id": expert_turn_id or None,
            "model": metrics.get("model"),
            "request_purpose": metrics.get("request_purpose"),
            "finish_reason": finish_reason,
            "finish_reason_reported": finish_reason is not None,
            "configured_max_tokens": metrics.get("configured_max_tokens"),
            "input_tokens": metrics.get("input_tokens"),
            "output_tokens": metrics.get("output_tokens"),
            "returned_chars": len(final_text) if returned_chars is None else returned_chars,
            "truncated": truncated,
            "truncation_status": truncation_status,
            "diagnostic_reason": (
                "上游模型未提供 finish_reason，无法仅凭正文确认是否截断"
                if finish_reason is None
                else None
            ),
        }
        self._event(context, "model_output_completion", payload)
        if truncated:
            self._event(context, "model_output_truncated", {
                **payload,
                "truncation_reason_code": "upstream_finish_reason_length",
                "truncation_reason": "上游模型以输出长度上限结束",
            })

    @staticmethod
    def _can_propose_team_handoff(team: ExpertTeamDefinition, expert_id: str) -> bool:
        """Central policy hook for handoff proposal capabilities.

        v1 intentionally grants the capability only to the coordinator.  A
        future team policy can broaden this predicate without changing tool,
        state, SSE, or bundle-facing handoff contracts.
        """
        return expert_id == team.coordinator_expert_id

    @staticmethod
    def _contains_unstructured_team_routing_text(reply: str, team: ExpertTeamDefinition) -> bool:
        text = str(reply or "")
        if not any(token in text for token in ("转交", "交给", "由", "建议您找", "请咨询")):
            return False
        return any(member.mention_name and member.mention_name in text for member in team.members)

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
        if not team_id:
            return None
        # Deployment snapshots are immutable per session. They must win over
        # a same-ID filesystem/global registration left in this process.
        entry = self._snapshot_entry(context, "expert_team", team_id)
        registered = None if entry is not None else self.team_registry.get(team_id)
        if registered is not None:
            return registered
        if entry is None:
            raise ValueError(f"专家团不存在: {team_id}")
        payload = entry["payload"]
        locks = entry.get("dependency_locks", [])
        coordinator_object_id = str(payload.get("coordinator_expert_id") or "")
        coordinator = next(
            (str(item.get("object_key") or "") for item in locks if str(item.get("object_id") or "") == coordinator_object_id),
            "",
        )
        member_payloads = payload.get("members") if isinstance(payload.get("members"), list) else []
        by_key = {str(item.get("expert_id") or ""): item for item in member_payloads if isinstance(item, dict)}
        members = tuple(
            ExpertTeamMember(
                expert_id=str(item.get("object_key") or ""),
                mention_name=str(
                    (by_key.get(str(item.get("object_key") or "")) or {}).get("mention_name")
                    or self._snapshot_expert_name(context, str(item.get("object_key") or ""))
                    or item.get("object_key")
                    or ""
                ),
                routing_brief=str((by_key.get(str(item.get("object_key") or "")) or {}).get("routing_brief") or ""),
            )
            for item in locks if str(item.get("object_key") or "")
        )
        return ExpertTeamDefinition(
            team_id=team_id,
            name=str(entry.get("name") or team_id),
            rules_markdown=str(payload.get("rules_markdown") or ""),
            coordinator_expert_id=coordinator,
            members=members,
            brief=str(payload.get("brief") or "").strip(),
        )

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
        context.session_meta.pop("pending_team_handoff_intent", None)
        source = str(switch.get("source") or "toolbar")
        # A confirmed handoff/tool-bar selection is a user-visible Agent
        # choice. Preserve it at session scope so entering another child's
        # isolated branch continues with the same member, while that branch's
        # facts/forms/Skill state remain local.
        set_selection = getattr(context, "set_session_agent_selection", None)
        if callable(set_selection):
            set_selection(
                expert_team_id=team.team_id,
                expert_id=member.expert_id,
                selection_source="handoff_card" if source in {"team_handoff", "team_handoff_ack", "team_handoff_ack_recovered"} else "manual",
            )
        context.session_meta["team_handoff_visible_user_message"] = str(
            switch.get("visible_user_message") or f"@{member.mention_name}"
        )
        is_handoff_source = source in {"team_handoff", "team_handoff_ack", "team_handoff_ack_recovered"}
        if is_handoff_source:
            # This is a timeline/audit event, not a new semantic question.
            # Keep it visible for history restoration while the planner
            # filters it out of subsequent model prompts.
            context.session_meta["team_handoff_visible_user_message_type"] = "team_handoff_confirmation"
            context.session_meta["team_handoff_visible_user_message_metadata"] = {
                "source_message_id": str(switch.get("source_message_id") or ""),
                "target_expert_id": member.expert_id,
                "expert_team_id": team.team_id,
                "source": source,
            }
        state = context.skill_states.setdefault(AGENT_RUNTIME_STATE_KEY, {})
        if isinstance(state, dict):
            state["active_expert_id"] = member.expert_id
        self._event(context, "team_member_switched", {
            "team_id": team.team_id,
            "expert_id": member.expert_id,
            "source": source,
            "from_expert_id": str(switch.get("from_expert_id") or ""),
        })
        excerpt = str(switch.get("conversation_excerpt") or "").strip()
        if is_handoff_source:
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
    def _attach_team_handoff(context, handoff: dict[str, Any]) -> bool:
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
            pending = context.session_meta.get("pending_team_handoff_intent")
            if isinstance(pending, dict):
                pending["source_message_id"] = handoff["source_message_id"]
                pending["presentation_status"] = "attached"
            legacy_pending = context.session_meta.get("pending_team_handoff")
            if isinstance(legacy_pending, dict):
                legacy_pending["source_message_id"] = handoff["source_message_id"]
                legacy_pending["presentation_status"] = "attached"
            return True
        pending = context.session_meta.get("pending_team_handoff_intent")
        if isinstance(pending, dict):
            pending["presentation_status"] = "attach_failed"
        from hailiang_skills.core.logging import make_event
        if isinstance(getattr(context, "event_trace", None), list):
            context.event_trace.append(make_event("team_handoff_presentation_deferred", {
                "handoff_id": str(handoff.get("handoff_id") or ""), "reason": "assistant_message_missing",
            }))
        return False

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

    def __new__(cls, client, completion_recorder=None):
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
                if callable(completion_recorder):
                    metrics = client.last_request_metrics() if callable(getattr(client, "last_request_metrics", None)) else {}
                    completion_recorder(result, metrics)
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
