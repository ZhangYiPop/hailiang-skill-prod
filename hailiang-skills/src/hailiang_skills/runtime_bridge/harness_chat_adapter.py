"""Project orchestrator_v2 Harness rounds onto chat SSE v2 events.

App clients keep posting to ``/api/v2/sessions/chat/stream``. When
``session_meta.orchestrator_v2`` is true, this adapter drives one round of the
orchestration pipeline and yields internal events that ``SseEnvelopeBuilder``
understands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from hailiang_skills.runtime_bridge.consultation_outcome import RoundExpertsState, select_final_reply
from hailiang_skills.runtime_bridge.consultation_plan import ConsultationPlan
from hailiang_skills.runtime_bridge.orchestration_head import run_orchestrator_head
from hailiang_skills.runtime_bridge.plan_executor import (
    ExpertTaskSpec,
    mark_complete,
    materialize_expert_tasks,
    ready_tasks,
    resolve_max_parallel,
)
from hailiang_skills.runtime_bridge.platform_gate import (
    build_platform_identity_form,
    missing_identity_fields,
)
from hailiang_skills.runtime_bridge.recommendation_extractor import extract_jump_cards


ExpertRunner = Callable[[ExpertTaskSpec | dict[str, Any], dict[str, Any]], dict[str, Any]]
HeadRunner = Callable[..., ConsultationPlan]
RecommendRunner = Callable[..., list[dict[str, Any]]]
TailRunner = Callable[..., str]


@dataclass
class HarnessChatDeps:
    """Injectable collaborators for unit tests / simple paths."""

    run_head: HeadRunner | None = None
    run_expert: ExpertRunner | None = None
    run_tail: TailRunner | None = None
    extract_cards: RecommendRunner | None = None
    head_client: Any = None
    tail_client: Any = None
    recommend_client: Any = None
    members: list[dict[str, Any]] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)
    fallback_expert_id: str = ""
    soul: str = ""
    head_rules: str = ""
    tail_rules: str = ""
    recommendation_rules: str = ""
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    collaboration_rules: str = ""


def uses_orchestrator_v2(context: Any) -> bool:
    meta = getattr(context, "session_meta", None)
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("orchestrator_v2"))


def empty_round_experts() -> dict[str, Any]:
    return RoundExpertsState().to_sse()


def _known_facts(context: Any) -> Any:
    return getattr(context, "known_facts", None) or getattr(context, "session_meta", {}).get("facts")


def _task_field(task: ExpertTaskSpec | dict[str, Any], key: str, default: Any = "") -> Any:
    if isinstance(task, dict):
        return task.get(key, default)
    return getattr(task, key, default)


def _task_as_dict(task: ExpertTaskSpec | dict[str, Any]) -> dict[str, Any]:
    if isinstance(task, ExpertTaskSpec):
        return task.as_dict()
    return dict(task)


def _default_expert_runner(task: ExpertTaskSpec | dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    expert_id = str(_task_field(task, "expert_id") or "")
    question = str(_task_field(task, "question") or "")
    return {
        "expert_id": expert_id,
        "task_id": _task_field(task, "task_id"),
        "status": "completed",
        "text": f"（{expert_id}）{question}".strip() or f"（{expert_id}）已完成咨询。",
    }


def _resolve_members(context: Any, deps: HarnessChatDeps) -> list[dict[str, Any]]:
    if deps.members:
        return list(deps.members)
    meta = getattr(context, "session_meta", {}) or {}
    snapshot = meta.get("configuration_snapshot") if isinstance(meta.get("configuration_snapshot"), dict) else {}
    orchestrator = snapshot.get("orchestrator") if isinstance(snapshot.get("orchestrator"), dict) else {}
    if isinstance(orchestrator.get("members"), list):
        return [item for item in orchestrator["members"] if isinstance(item, dict)]
    policy = meta.get("harness_policy") if isinstance(meta.get("harness_policy"), dict) else {}
    orch = policy.get("orchestrator") if isinstance(policy.get("orchestrator"), dict) else {}
    if isinstance(orch.get("members"), list):
        return [item for item in orch["members"] if isinstance(item, dict)]
    return []


def _resolve_orchestrator_view(context: Any, deps: HarnessChatDeps) -> dict[str, Any]:
    meta = getattr(context, "session_meta", {}) or {}
    snapshot = meta.get("configuration_snapshot") if isinstance(meta.get("configuration_snapshot"), dict) else {}
    rules = snapshot.get("orchestration_rules") if isinstance(snapshot.get("orchestration_rules"), dict) else {}
    policy_blob = meta.get("harness_policy") if isinstance(meta.get("harness_policy"), dict) else {}
    orch = policy_blob.get("orchestrator") if isinstance(policy_blob.get("orchestrator"), dict) else {}
    return {
        "soul": deps.soul or str(orch.get("soul") or meta.get("soul") or ""),
        "head_rules": deps.head_rules or str(rules.get("head_rules") or orch.get("head_rules") or ""),
        "tail_rules": deps.tail_rules or str(rules.get("tail_rules") or orch.get("tail_rules") or ""),
        "recommendation_rules": deps.recommendation_rules
        or str(rules.get("recommendation_rules") or orch.get("recommendation_rules") or ""),
        "recommendations": deps.recommendations
        or list(rules.get("recommendations") or orch.get("recommendations") or []),
        "policy": deps.policy or dict(orch.get("consultation_policy") or {}),
        "fallback_expert_id": deps.fallback_expert_id
        or str(orch.get("fallback_expert_id") or meta.get("fallback_expert_id") or ""),
        "collaboration_rules": deps.collaboration_rules or str(orch.get("collaboration_rules") or ""),
    }


def _expert_context_event(context: Any, *, active_expert_id: str | None = None) -> dict[str, Any]:
    meta = getattr(context, "session_meta", {}) or {}
    team_id = str(meta.get("expert_team_id") or "").strip() or None
    expert_id = (
        str(active_expert_id or meta.get("active_expert_id") or meta.get("expert_id") or "").strip() or None
    )
    selection = {}
    getter = getattr(context, "session_agent_selection", None)
    if callable(getter):
        selection = getter() or {}
    return {
        "mode": "team" if team_id else "none",
        "team": {"team_id": team_id} if team_id else {},
        "active": {"expert_id": expert_id} if expert_id else {},
        "activation": {},
        "transition": {},
        "expert_context": {
            "expert_team_id": team_id,
            "expert_id": expert_id,
            "branch_version": int(meta.get("_active_branch_version") or 0),
            "selection_version": int(selection.get("selection_version") or 0),
        },
    }


def run_orchestrator_v2_turn(
    *,
    context: Any,
    user_message: str,
    run_id: str,
    deps: HarnessChatDeps | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(internal_event, data)`` pairs for one orchestrator_v2 round."""
    deps = deps or HarnessChatDeps()
    view = _resolve_orchestrator_view(context, deps)
    members = _resolve_members(context, deps)
    facts = _known_facts(context)

    missing = missing_identity_fields(facts)
    if missing:
        form_block = build_platform_identity_form(missing=missing)
        yield (
            "skill_action",
            {
                "message_blocks": [form_block],
                "active_skill": "platform_gate",
            },
        )
        yield (
            "final_text_delta",
            {"delta": "请先补充身份与学段信息，再继续咨询。"},
        )
        yield ("round_experts", empty_round_experts())
        yield ("jump_cards", {"jump_cards": []})
        yield ("expert_context", _expert_context_event(context))
        yield (
            "run_completed",
            {"session_id": getattr(context, "session_id", ""), "run_id": run_id, "status": "completed"},
        )
        return

    def _default_head(**kwargs: Any) -> ConsultationPlan:
        if deps.head_client is None:
            # Simple stub path when no LLM is wired (tests inject run_head).
            from hailiang_skills.runtime_bridge.consultation_plan import Subtask

            fallback = str(kwargs.get("fallback_expert_id") or view["fallback_expert_id"] or "")
            member_ids = [
                str(item.get("expert_id") or "")
                for item in (kwargs.get("members") or members)
                if isinstance(item, dict) and item.get("expert_id")
            ]
            expert_id = fallback or (member_ids[0] if member_ids else "fallback_expert")
            message = str(kwargs.get("user_message") or user_message)
            if "澄清" in message or "clarify" in message.lower():
                return ConsultationPlan(
                    action="clarify",
                    clarification_question="你更想先了解升学路径，还是学习方法？",
                    reason="stub_clarify",
                )
            if "拒答" in message or "reject" in message.lower():
                return ConsultationPlan(
                    action="reject_out_of_scope",
                    clarification_question="这个问题超出当前教育咨询范围，请换一个相关问题。",
                    reason="stub_reject",
                )
            if ("双专家" in message or "parallel" in message.lower()) and len(member_ids) >= 2:
                return ConsultationPlan(
                    action="consult",
                    subtasks=[
                        Subtask(subtask_id="st_1", question=message, target_expert_id=member_ids[0], priority=1),
                        Subtask(subtask_id="st_2", question=message, target_expert_id=member_ids[1], priority=2),
                    ],
                    reason="stub_parallel",
                )
            return ConsultationPlan(
                action="consult",
                subtasks=[
                    Subtask(
                        subtask_id="st_1",
                        question=message or "请回答用户问题",
                        target_expert_id=expert_id,
                    )
                ],
                reason="stub_single",
            )
        return run_orchestrator_head(
            deps.head_client,
            soul=str(kwargs.get("soul") or view["soul"]),
            head_rules=str(kwargs.get("head_rules") or view["head_rules"]),
            members=kwargs.get("members") or members,
            policy=kwargs.get("policy") or view["policy"],
            profile=kwargs.get("profile") or {"facts": facts},
            messages=kwargs.get("messages") or list(getattr(context, "messages", []) or []),
            fallback_expert_id=str(kwargs.get("fallback_expert_id") or view["fallback_expert_id"]),
        )

    head_runner = deps.run_head or _default_head
    plan = head_runner(
        soul=view["soul"],
        head_rules=view["head_rules"],
        members=members,
        policy=view["policy"],
        profile={"facts": facts},
        messages=list(getattr(context, "messages", []) or []),
        fallback_expert_id=view["fallback_expert_id"],
        user_message=user_message,
    )
    if not isinstance(plan, ConsultationPlan):
        plan = ConsultationPlan(
            action="clarify",
            clarification_question="请再具体说明一下你的问题。",
            reason="invalid_head_result",
        )

    if plan.action in {"clarify", "reject_out_of_scope"}:
        text = plan.clarification_question or "请补充更多信息。"
        yield ("final_text_delta", {"delta": text})
        yield ("round_experts", empty_round_experts())
        yield ("jump_cards", {"jump_cards": []})
        yield ("expert_context", _expert_context_event(context))
        yield (
            "run_completed",
            {"session_id": getattr(context, "session_id", ""), "run_id": run_id, "status": "completed"},
        )
        return

    tasks = materialize_expert_tasks(plan, view["policy"])
    assigned = [str(_task_field(task, "expert_id") or "") for task in tasks if _task_field(task, "expert_id")]
    round_state = RoundExpertsState(
        assigned_expert_ids=list(dict.fromkeys(assigned)),
        status="assigned",
    )
    yield ("round_experts", round_state.to_sse())

    expert_runner = deps.run_expert or _default_expert_runner
    results: list[dict[str, Any]] = []
    completed_ids: set[str] = set()
    running_ids: set[str] = set()
    max_parallel = resolve_max_parallel(view["policy"])

    while True:
        ready = ready_tasks(
            tasks,
            completed=completed_ids,
            running=running_ids,
            max_parallel=max_parallel,
            policy=view["policy"],
        )
        if not ready:
            pending = [task for task in tasks if str(_task_field(task, "status") or "") in {"pending", "ready"}]
            if not pending:
                break
            for task in pending:
                task_id = str(_task_field(task, "task_id") or "")
                expert_id = str(_task_field(task, "expert_id") or "")
                mark_complete(tasks, task_id, success=False)
                completed_ids.add(task_id)
                round_state.mark_failed(expert_id)
                results.append(
                    {
                        "expert_id": expert_id,
                        "task_id": task_id,
                        "status": "failed",
                        "text": "",
                    }
                )
            yield ("round_experts", round_state.to_sse())
            break

        for task in ready:
            task_id = str(_task_field(task, "task_id") or "")
            expert_id = str(_task_field(task, "expert_id") or "")
            running_ids.add(task_id)
            if isinstance(task, ExpertTaskSpec):
                task.status = "running"
            elif isinstance(task, dict):
                task["status"] = "running"
            round_state.mark_running(expert_id)
            yield ("round_experts", round_state.to_sse())
            try:
                result = expert_runner(task, {"context": context, "user_message": user_message, "task": _task_as_dict(task)})
            except Exception as exc:  # pragma: no cover - defensive
                result = {
                    "expert_id": expert_id,
                    "task_id": task_id,
                    "status": "failed",
                    "text": "",
                    "error": str(exc),
                }
            status = str(result.get("status") or "completed")
            success = status != "failed"
            mark_complete(tasks, task_id, success=success)
            running_ids.discard(task_id)
            completed_ids.add(task_id)
            if success:
                round_state.mark_completed(expert_id)
            else:
                round_state.mark_failed(expert_id)
            results.append(result)
            yield ("round_experts", round_state.to_sse())

    def _call_tail(**kwargs: Any) -> str:
        expert_results = kwargs.get("expert_results") or results
        if deps.run_tail is not None:
            return deps.run_tail(
                tail_rules=view["tail_rules"],
                user_message=user_message,
                user_question=user_message,
                expert_results=expert_results,
                collaboration_rules=view["collaboration_rules"],
            )
        if deps.tail_client is not None:
            from hailiang_skills.runtime_bridge.orchestration_tail import run_tail_merge

            return run_tail_merge(
                deps.tail_client,
                tail_rules=view["tail_rules"],
                expert_results=expert_results,
                user_question=user_message,
                collaboration_rules=view["collaboration_rules"],
            )
        from hailiang_skills.runtime_bridge.orchestration_tail import simple_concat_merge

        return simple_concat_merge(expert_results)

    multi_success = len([row for row in results if row.get("status") != "failed" and row.get("text")]) > 1
    final = select_final_reply(
        results,
        call_tail=_call_tail if multi_success else None,
        client=deps.tail_client if multi_success else None,
        tail_rules=view["tail_rules"],
        user_question=user_message,
        collaboration_rules=view["collaboration_rules"],
    )
    text = str(final.get("text") or "")
    if text:
        yield ("final_text_delta", {"delta": text})

    active_expert = str(final.get("source_expert_id") or "").strip() or None
    if not active_expert and len(assigned) == 1:
        active_expert = assigned[0]
    yield ("expert_context", _expert_context_event(context, active_expert_id=active_expert))

    def _default_recommend(**kwargs: Any) -> list[dict[str, Any]]:
        if deps.recommend_client is None:
            return []
        return extract_jump_cards(
            deps.recommend_client,
            kwargs.get("catalog") if "catalog" in kwargs else view["recommendations"],
            recommendation_rules=str(kwargs.get("recommendation_rules") or view["recommendation_rules"]),
            user_question=str(kwargs.get("user_question") or kwargs.get("user_message") or user_message),
            assistant_reply=str(kwargs.get("assistant_reply") or text),
        )

    recommend = deps.extract_cards or _default_recommend
    cards = recommend(
        recommendation_rules=view["recommendation_rules"],
        catalog=view["recommendations"],
        user_message=user_message,
        user_question=user_message,
        assistant_reply=text,
    )
    if isinstance(cards, dict):
        cards = cards.get("jump_cards") or []
    yield ("jump_cards", {"jump_cards": list(cards or [])})
    yield ("round_experts", round_state.to_sse())
    yield (
        "run_completed",
        {"session_id": getattr(context, "session_id", ""), "run_id": run_id, "status": "completed"},
    )
