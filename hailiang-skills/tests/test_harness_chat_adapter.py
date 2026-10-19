"""Projection tests for orchestrator_v2 chat SSE ↔ Harness adapter."""

from __future__ import annotations

from types import SimpleNamespace

from hailiang_skills.core.sse_protocol import SseEnvelopeBuilder
from hailiang_skills.runtime_bridge.consultation_plan import ConsultationPlan, Subtask
from hailiang_skills.runtime_bridge.harness_chat_adapter import (
    HarnessChatDeps,
    run_orchestrator_v2_turn,
    uses_orchestrator_v2,
)
from hailiang_skills.schemas.facts import FactRecord, KnownFacts


def _context(*, facts: dict[str, str] | None = None, orchestrator_v2: bool = True) -> SimpleNamespace:
    known = KnownFacts()
    for key, value in (facts or {}).items():
        known.facts[key] = FactRecord(value=value, source_skill="test")
    return SimpleNamespace(
        session_id="sess_orch",
        user_id="u1",
        profile_id="p1",
        messages=[],
        known_facts=known,
        session_meta={
            "orchestrator_v2": orchestrator_v2,
            "expert_team_id": "student_growth_expert_team",
            "expert_id": None,
            "_active_branch_version": 1,
        },
        session_agent_selection=lambda: {
            "expert_team_id": "student_growth_expert_team",
            "expert_id": None,
            "selection_version": 2,
        },
    )


def _project(events: list[tuple[str, dict]]) -> dict:
    builder = SseEnvelopeBuilder(run_id="run_1", session_id="sess_orch")
    builder.encode("run_started", {"session_id": "sess_orch"})
    for event, data in events:
        builder.encode(event, data)
    return builder.snapshot()


def test_uses_orchestrator_v2_flag() -> None:
    assert uses_orchestrator_v2(_context(orchestrator_v2=True)) is True
    assert uses_orchestrator_v2(_context(orchestrator_v2=False)) is False


def test_adapter_gate_missing_identity_does_not_call_head() -> None:
    calls: list[str] = []

    def head(**_kwargs):
        calls.append("head")
        raise AssertionError("head must not run when identity is missing")

    events = list(
        run_orchestrator_v2_turn(
            context=_context(facts={}),
            user_message="你好",
            run_id="run_1",
            deps=HarnessChatDeps(run_head=head, members=[{"expert_id": "career_plan_expert"}]),
        )
    )
    assert calls == []
    state = _project(events)
    assert state["assistant"]["content"]
    assert state["form"]["form_id"] == "platform_identity_form"
    assert {field["fact_key"] for field in state["form"]["fields"]} == {"user_role", "school_stage"}
    assert state["round_experts"]["status"] == "idle"
    assert state["jump_cards"] == []


def test_adapter_consult_single_expert_goes_through_tail() -> None:
    plan = ConsultationPlan(
        action="consult",
        subtasks=[
            Subtask(
                subtask_id="st_1",
                question="升学怎么规划",
                target_expert_id="career_plan_expert",
            )
        ],
    )
    tail_calls: list[dict] = []

    def head(**_kwargs):
        return plan

    def expert(task, _ctx):
        expert_id = task["expert_id"] if isinstance(task, dict) else task.expert_id
        task_id = task["task_id"] if isinstance(task, dict) else task.task_id
        return {
            "expert_id": expert_id,
            "task_id": task_id,
            "status": "completed",
            "text": "单专家直出答复",
        }

    def run_tail(**kwargs):
        tail_calls.append(kwargs)
        rows = kwargs.get("expert_results") or []
        assert len(rows) == 1
        return rows[0]["text"]

    events = list(
        run_orchestrator_v2_turn(
            context=_context(facts={"user_role": "家长", "school_stage": "高中"}),
            user_message="升学怎么规划",
            run_id="run_1",
            deps=HarnessChatDeps(
                run_head=head,
                run_expert=expert,
                run_tail=run_tail,
                members=[{"expert_id": "career_plan_expert"}],
                extract_cards=lambda **_: [],
            ),
        )
    )
    state = _project(events)
    assert len(tail_calls) == 1
    assert state["assistant"]["content"] == "单专家直出答复"
    assert state["round_experts"]["assigned_expert_ids"] == ["career_plan_expert"]
    assert state["round_experts"]["completed_expert_ids"] == ["career_plan_expert"]
    assert state["round_experts"]["running_expert_ids"] == []
    assert state["round_experts"]["status"] == "completed"
    assert state["expert_context"]["expert_id"] == "career_plan_expert"


def test_adapter_parallel_experts_assigned_to_completed() -> None:
    plan = ConsultationPlan(
        action="consult",
        subtasks=[
            Subtask(subtask_id="st_1", question="升学", target_expert_id="career_plan_expert"),
            Subtask(subtask_id="st_2", question="家庭", target_expert_id="family_education_expert"),
        ],
    )
    seen_statuses: list[str] = []

    def head(**_kwargs):
        return plan

    def expert(task, _ctx):
        expert_id = task["expert_id"] if isinstance(task, dict) else task.expert_id
        task_id = task["task_id"] if isinstance(task, dict) else task.task_id
        return {
            "expert_id": expert_id,
            "task_id": task_id,
            "status": "completed",
            "text": f"来自{expert_id}",
        }

    events = list(
        run_orchestrator_v2_turn(
            context=_context(facts={"user_role": "家长", "school_stage": "高中"}),
            user_message="双专家并行",
            run_id="run_1",
            deps=HarnessChatDeps(
                run_head=head,
                run_expert=expert,
                members=[
                    {"expert_id": "career_plan_expert"},
                    {"expert_id": "family_education_expert"},
                ],
                extract_cards=lambda **_: [],
            ),
        )
    )
    for event, data in events:
        if event == "round_experts":
            seen_statuses.append(str(data.get("status")))
    assert "assigned" in seen_statuses
    assert "running" in seen_statuses
    assert seen_statuses[-1] == "completed"
    state = _project(events)
    assert set(state["round_experts"]["assigned_expert_ids"]) == {
        "career_plan_expert",
        "family_education_expert",
    }
    assert set(state["round_experts"]["completed_expert_ids"]) == {
        "career_plan_expert",
        "family_education_expert",
    }
    assert "来自career_plan_expert" in state["assistant"]["content"]
    assert "来自family_education_expert" in state["assistant"]["content"]


def test_adapter_clarify_and_reject() -> None:
    clarify = ConsultationPlan(action="clarify", clarification_question="你想先问升学还是学习？")
    reject = ConsultationPlan(
        action="reject_out_of_scope",
        clarification_question="这个问题超出教育咨询范围。",
    )

    clarify_events = list(
        run_orchestrator_v2_turn(
            context=_context(facts={"user_role": "家长", "school_stage": "高中"}),
            user_message="随便问问",
            run_id="run_c",
            deps=HarnessChatDeps(run_head=lambda **_: clarify, members=[{"expert_id": "career_plan_expert"}]),
        )
    )
    reject_events = list(
        run_orchestrator_v2_turn(
            context=_context(facts={"user_role": "家长", "school_stage": "高中"}),
            user_message="买股票",
            run_id="run_r",
            deps=HarnessChatDeps(run_head=lambda **_: reject, members=[{"expert_id": "career_plan_expert"}]),
        )
    )
    clarify_state = _project(clarify_events)
    reject_state = _project(reject_events)
    assert clarify_state["assistant"]["content"] == "你想先问升学还是学习？"
    assert reject_state["assistant"]["content"] == "这个问题超出教育咨询范围。"
    assert clarify_state["round_experts"]["assigned_expert_ids"] == []
    assert reject_state["round_experts"]["status"] == "idle"


def test_adapter_jump_cards_injected_into_state() -> None:
    plan = ConsultationPlan(
        action="consult",
        subtasks=[Subtask(subtask_id="st_1", question="q", target_expert_id="career_plan_expert")],
    )
    cards = [{"intent": "explore", "object_type": "assessment", "sub_type": "career"}]

    events = list(
        run_orchestrator_v2_turn(
            context=_context(facts={"user_role": "家长", "school_stage": "高中"}),
            user_message="想做测评",
            run_id="run_1",
            deps=HarnessChatDeps(
                run_head=lambda **_: plan,
                run_expert=lambda task, _ctx: {
                    "expert_id": task["expert_id"] if isinstance(task, dict) else task.expert_id,
                    "task_id": task["task_id"] if isinstance(task, dict) else task.task_id,
                    "status": "completed",
                    "text": "可以先做兴趣测评。",
                },
                extract_cards=lambda **_: cards,
                members=[{"expert_id": "career_plan_expert"}],
            ),
        )
    )
    state = _project(events)
    assert state["jump_cards"] == cards
    assert state["assistant"]["content"] == "可以先做兴趣测评。"
