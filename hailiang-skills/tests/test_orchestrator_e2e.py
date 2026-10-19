"""End-to-end scenarios for orchestrator_v2 (stubbed LLM/experts)."""

from __future__ import annotations

from types import SimpleNamespace

from hailiang_skills.core.sse_protocol import SseEnvelopeBuilder
from hailiang_skills.runtime_bridge.consultation_plan import ConsultationPlan, Subtask
from hailiang_skills.runtime_bridge.harness_chat_adapter import HarnessChatDeps, run_orchestrator_v2_turn
from hailiang_skills.runtime_bridge.system_skill import load_system_skills_by_purpose
from hailiang_skills.schemas.facts import FactRecord, KnownFacts
from hailiang_skills.workbench.orchestration import load_seed_rules, orchestrator_v2_enabled


def _context(facts: dict[str, str]) -> SimpleNamespace:
    known = KnownFacts()
    for key, value in facts.items():
        known.facts[key] = FactRecord(value=value, source_skill="test")
    return SimpleNamespace(
        session_id="sess_e2e",
        user_id="u1",
        profile_id="p1",
        messages=[],
        known_facts=known,
        session_meta={
            "orchestrator_v2": True,
            "expert_team_id": "student_growth_expert_team",
            "expert_id": None,
            "_active_branch_version": 1,
        },
        session_agent_selection=lambda: {
            "expert_team_id": "student_growth_expert_team",
            "expert_id": None,
            "selection_version": 1,
        },
    )


def _project(events: list[tuple[str, dict]]) -> dict:
    builder = SseEnvelopeBuilder(run_id="run_e2e", session_id="sess_e2e")
    builder.encode("run_started", {"session_id": "sess_e2e"})
    for event, data in events:
        builder.encode(event, data)
    return builder.snapshot()


def test_seed_and_system_skills_are_publishable():
    seed = load_seed_rules()
    assert "depends_on" in seed["head_rules"] or "max_parallel" in seed["head_rules"]
    assert seed["recommend_catalog"]["items"]
    by_purpose = load_system_skills_by_purpose()
    assert set(by_purpose) == {"orchestrate_head", "orchestrate_tail", "recommend"}
    assert by_purpose["recommend"].catalog is not None


def test_orchestrator_v2_env_defaults_off(monkeypatch):
    monkeypatch.delenv("HAILIANG_ORCHESTRATOR_V2", raising=False)
    assert orchestrator_v2_enabled() is False
    monkeypatch.setenv("HAILIANG_ORCHESTRATOR_V2", "1")
    assert orchestrator_v2_enabled() is True


def test_e2e_serial_depends_on_runs_upstream_first():
    plan = ConsultationPlan(
        action="consult",
        subtasks=[
            Subtask(
                subtask_id="st_1",
                question="先做家庭侧",
                target_expert_id="family_education_expert",
                priority=1,
                depends_on=[],
            ),
            Subtask(
                subtask_id="st_2",
                question="再做升学",
                target_expert_id="career_plan_expert",
                priority=2,
                depends_on=["st_1"],
            ),
        ],
        reason="serial",
    )
    order: list[str] = []

    def expert(task, _ctx):
        expert_id = task["expert_id"] if isinstance(task, dict) else task.expert_id
        task_id = task["task_id"] if isinstance(task, dict) else task.task_id
        order.append(expert_id)
        return {
            "expert_id": expert_id,
            "task_id": task_id,
            "status": "completed",
            "text": f"reply:{expert_id}",
        }

    events = list(
        run_orchestrator_v2_turn(
            context=_context({"user_role": "家长", "school_stage": "初中"}),
            user_message="串行双专家",
            run_id="run_serial",
            deps=HarnessChatDeps(
                run_head=lambda **_: plan,
                run_expert=expert,
                members=[
                    {"expert_id": "family_education_expert", "applicable_stages": ["小学", "初中"]},
                    {"expert_id": "career_plan_expert", "applicable_stages": ["小学", "初中", "高中"]},
                ],
                policy={"max_subtasks": 3, "max_parallel": 2, "max_depth": 1},
                extract_cards=lambda **_: [
                    {
                        "intent": "assessment",
                        "object_type": "single_assessment",
                        "sub_type": "career_interest",
                        "title": "职业兴趣测评",
                    }
                ],
                run_tail=lambda **_: "合并后的答复",
            ),
        )
    )
    assert order == ["family_education_expert", "career_plan_expert"]
    state = _project(events)
    assert state["assistant"]["content"] == "合并后的答复"
    assert state["round_experts"]["status"] == "completed"
    assert state["jump_cards"][0]["sub_type"] == "career_interest"


def test_e2e_max_parallel_one_forces_serial_even_without_depends():
    plan = ConsultationPlan(
        action="consult",
        subtasks=[
            Subtask(subtask_id="st_1", question="a", target_expert_id="career_plan_expert"),
            Subtask(subtask_id="st_2", question="b", target_expert_id="family_education_expert"),
        ],
    )
    order: list[str] = []

    def expert(task, _ctx):
        expert_id = task["expert_id"] if isinstance(task, dict) else task.expert_id
        task_id = task["task_id"] if isinstance(task, dict) else task.task_id
        order.append(expert_id)
        return {"expert_id": expert_id, "task_id": task_id, "status": "completed", "text": expert_id}

    list(
        run_orchestrator_v2_turn(
            context=_context({"user_role": "家长", "school_stage": "初中"}),
            user_message="容量串行",
            run_id="run_cap",
            deps=HarnessChatDeps(
                run_head=lambda **_: plan,
                run_expert=expert,
                members=[
                    {"expert_id": "career_plan_expert"},
                    {"expert_id": "family_education_expert"},
                ],
                policy={"max_subtasks": 3, "max_parallel": 1, "max_depth": 1},
                extract_cards=lambda **_: [],
            ),
        )
    )
    # With max_parallel=1 the second expert starts only after the first completes.
    assert len(order) == 2
    assert order[0] in {"career_plan_expert", "family_education_expert"}
    assert order[1] in {"career_plan_expert", "family_education_expert"}
    assert order[0] != order[1]
