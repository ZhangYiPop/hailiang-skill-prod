from __future__ import annotations

from hailiang_skills.runtime_bridge.consultation_plan import ConsultationPlan, Subtask
from hailiang_skills.runtime_bridge.consultation_policy import ConsultationPolicy
from hailiang_skills.runtime_bridge.plan_executor import (
    materialize_expert_tasks,
    mark_complete,
    ready_tasks,
    resolve_max_parallel,
)


def _plan_with_deps() -> ConsultationPlan:
    return ConsultationPlan(
        action="consult",
        clarification_question="",
        subtasks=[
            Subtask(
                subtask_id="st_1",
                question="先做升学分析",
                target_expert_id="career_plan_expert",
                priority=1,
                depends_on=[],
            ),
            Subtask(
                subtask_id="st_2",
                question="再做家庭沟通建议",
                target_expert_id="family_education_expert",
                priority=2,
                depends_on=["st_1"],
            ),
            Subtask(
                subtask_id="st_3",
                question="并行补充学科建议",
                target_expert_id="subject_expert",
                priority=1,
                depends_on=[],
            ),
        ],
        reason="test",
    )


def test_resolve_max_parallel_reads_policy_and_legacy():
    assert resolve_max_parallel(ConsultationPolicy(max_subtasks=4, max_parallel=2)) == 2
    assert resolve_max_parallel({"max_subtasks": 3, "allow_parallel": True}) == 3
    assert resolve_max_parallel({"max_subtasks": 3, "allow_parallel": False}) == 1


def test_materialize_expert_tasks_preserves_depends_on():
    tasks = materialize_expert_tasks(_plan_with_deps(), ConsultationPolicy(max_subtasks=3, max_parallel=2))
    assert [task.task_id for task in tasks] == ["st_1", "st_2", "st_3"]
    assert tasks[1].depends_on == ["st_1"]
    assert tasks[0].expert_id == "career_plan_expert"


def test_materialize_skips_non_consult():
    plan = ConsultationPlan(action="clarify", clarification_question="还缺什么？", subtasks=[], reason="x")
    assert materialize_expert_tasks(plan) == []


def test_ready_tasks_respects_dag_and_max_parallel():
    tasks = materialize_expert_tasks(_plan_with_deps())
    first = ready_tasks(tasks, completed=set(), running=set(), max_parallel=2)
    assert [task.task_id for task in first] == ["st_1", "st_3"]
    assert all(task.status == "ready" for task in first)

    # capacity already full
    none_ready = ready_tasks(tasks, completed=set(), running={"st_1", "st_3"}, max_parallel=2)
    assert none_ready == []

    mark_complete(tasks, "st_1", success=True)
    mark_complete(tasks, "st_3", success=True)
    second = ready_tasks(
        tasks,
        completed={"st_1", "st_3"},
        running=set(),
        policy=ConsultationPolicy(max_subtasks=3, max_parallel=2),
    )
    assert [task.task_id for task in second] == ["st_2"]


def test_mark_complete_failed():
    tasks = materialize_expert_tasks(_plan_with_deps())
    updated = mark_complete(tasks, "st_3", success=False)
    assert updated is not None
    assert updated.status == "failed"
    still = ready_tasks(tasks, completed={"st_1"}, running=set(), max_parallel=3)
    assert [task.task_id for task in still] == ["st_2"]
