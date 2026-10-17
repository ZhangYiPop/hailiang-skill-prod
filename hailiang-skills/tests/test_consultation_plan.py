import pytest

from hailiang_skills.runtime_bridge.consultation_plan import (
    ConsultationPlanError,
    parse_and_validate_consultation_plan,
    parse_consultation_plan,
    validate_consultation_plan,
)


MEMBERS = [
    {"expert_id": "career_plan_expert", "applicable_stages": ["小学", "初中", "高中"]},
    {"expert_id": "family_education_expert", "applicable_stages": ["小学", "初中"]},
]


def test_parse_consult_plan_from_json_string():
    plan = parse_consultation_plan(
        """
        {
          "action": "consult",
          "clarification_question": "",
          "subtasks": [
            {
              "subtask_id": "st_1",
              "question": "帮我看升学路径",
              "target_expert_id": "career_plan_expert",
              "priority": 1,
              "depends_on": []
            }
          ],
          "reason": "匹配升学规划"
        }
        """
    )
    assert plan.action == "consult"
    assert plan.subtasks[0].target_expert_id == "career_plan_expert"


def test_validate_consult_accepts_depends_on_dag():
    plan = parse_and_validate_consultation_plan(
        {
            "action": "consult",
            "clarification_question": "",
            "subtasks": [
                {
                    "subtask_id": "st_1",
                    "question": "先做测评解读",
                    "target_expert_id": "family_education_expert",
                    "priority": 1,
                    "depends_on": [],
                },
                {
                    "subtask_id": "st_2",
                    "question": "再做升学规划",
                    "target_expert_id": "career_plan_expert",
                    "priority": 2,
                    "depends_on": ["st_1"],
                },
            ],
            "reason": "有信息依赖",
        },
        members=MEMBERS,
        school_stage="初中",
        max_subtasks=3,
    )
    assert [item.subtask_id for item in plan.subtasks] == ["st_1", "st_2"]


def test_validate_rejects_unknown_expert_and_stage_mismatch():
    with pytest.raises(ConsultationPlanError) as unknown:
        parse_and_validate_consultation_plan(
            {
                "action": "consult",
                "subtasks": [{
                    "subtask_id": "st_1",
                    "question": "q",
                    "target_expert_id": "missing_expert",
                    "depends_on": [],
                }],
                "reason": "x",
            },
            members=MEMBERS,
            max_subtasks=3,
        )
    assert unknown.value.code in {"UNKNOWN_EXPERT", "PLAN_UNKNOWN_EXPERT"}

    with pytest.raises(ConsultationPlanError) as stage:
        parse_and_validate_consultation_plan(
            {
                "action": "consult",
                "subtasks": [{
                    "subtask_id": "st_1",
                    "question": "q",
                    "target_expert_id": "family_education_expert",
                    "depends_on": [],
                }],
                "reason": "x",
            },
            members=MEMBERS,
            school_stage="高中",
            max_subtasks=3,
        )
    assert stage.value.code in {"STAGE_MISMATCH", "PLAN_STAGE_MISMATCH"}


def test_clarify_and_reject_forbid_subtasks():
    clarify = validate_consultation_plan(
        {
            "action": "clarify",
            "clarification_question": "孩子现在读几年级？",
            "subtasks": [],
            "reason": "缺学段",
        },
        members=MEMBERS,
    )
    assert clarify.action == "clarify"

    with pytest.raises(ConsultationPlanError) as exc:
        validate_consultation_plan(
            {
                "action": "reject_out_of_scope",
                "clarification_question": "这个问题超出服务范围。",
                "subtasks": [{
                    "subtask_id": "st_1",
                    "question": "不应出现",
                    "target_expert_id": "career_plan_expert",
                }],
                "reason": "x",
            },
            members=MEMBERS,
        )
    assert exc.value.code in {"CLARIFY_NO_SUBTASKS", "PLAN_SUBTASKS_FORBIDDEN"}


def test_detects_depends_on_cycle_and_overflow():
    with pytest.raises(ConsultationPlanError) as cycle:
        parse_and_validate_consultation_plan(
            {
                "action": "consult",
                "subtasks": [
                    {
                        "subtask_id": "st_1",
                        "question": "a",
                        "target_expert_id": "career_plan_expert",
                        "depends_on": ["st_2"],
                    },
                    {
                        "subtask_id": "st_2",
                        "question": "b",
                        "target_expert_id": "family_education_expert",
                        "depends_on": ["st_1"],
                    },
                ],
                "reason": "cycle",
            },
            members=MEMBERS,
            max_subtasks=3,
        )
    assert cycle.value.code in {"DEPENDENCY_CYCLE", "PLAN_DEPENDS_ON_CYCLE"}

    with pytest.raises(ConsultationPlanError) as overflow:
        parse_and_validate_consultation_plan(
            {
                "action": "consult",
                "subtasks": [
                    {
                        "subtask_id": f"st_{index}",
                        "question": "q",
                        "target_expert_id": "career_plan_expert",
                        "depends_on": [],
                    }
                    for index in range(1, 5)
                ],
                "reason": "too many",
            },
            members=MEMBERS,
            max_subtasks=3,
        )
    assert overflow.value.code in {"MAX_SUBTASKS", "PLAN_SUBTASKS_OVERFLOW"}
