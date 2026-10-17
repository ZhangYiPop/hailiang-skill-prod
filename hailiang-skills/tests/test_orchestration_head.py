from __future__ import annotations

import json

from hailiang_skills.runtime_bridge.consultation_policy import ConsultationPolicy
from hailiang_skills.runtime_bridge.orchestration_head import (
    build_orchestrator_head_prompt,
    run_orchestrator_head,
)
from hailiang_skills.skill_runtime.models import ChatMessage


MEMBERS = [
    {
        "expert_id": "career_plan_expert",
        "mention_name": "升学规划专家",
        "routing_brief": "升学规划",
        "applicable_stages": ["高中"],
    },
    {
        "expert_id": "family_education_expert",
        "mention_name": "家庭教育专家",
        "routing_brief": "家庭教育",
        "applicable_stages": ["小学", "初中", "高中"],
    },
]


class ScriptedClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages, **kwargs):
        self.calls.append(list(messages))
        if not self.responses:
            raise AssertionError("no scripted response left")
        return self.responses.pop(0)


def test_build_orchestrator_head_prompt_includes_soul_rules_and_members():
    prompt = build_orchestrator_head_prompt(
        soul="SOUL",
        head_rules="HEAD",
        members=MEMBERS,
        policy=ConsultationPolicy(max_subtasks=3, max_parallel=2, max_depth=1),
        profile={"user_role": "家长", "school_stage": "高中"},
        messages=[{"role": "user", "content": "想了解高考志愿"}],
    )
    assert prompt[0].role == "system"
    assert "SOUL" in prompt[0].content
    assert "HEAD" in prompt[0].content
    assert "career_plan_expert" in prompt[0].content
    assert "max_parallel" in prompt[0].content
    assert any(item.role == "user" and "高考志愿" in item.content for item in prompt)


def test_run_orchestrator_head_parses_valid_plan():
    plan_json = json.dumps(
        {
            "action": "consult",
            "clarification_question": "",
            "subtasks": [
                {
                    "subtask_id": "st_1",
                    "question": "请帮我看高考志愿",
                    "target_expert_id": "career_plan_expert",
                    "priority": 1,
                    "depends_on": [],
                }
            ],
            "reason": "对口升学",
        },
        ensure_ascii=False,
    )
    client = ScriptedClient([plan_json])
    plan = run_orchestrator_head(
        client,
        soul="s",
        head_rules="h",
        members=MEMBERS,
        policy=ConsultationPolicy(),
        profile={"school_stage": "高中"},
        messages=[{"role": "user", "content": "高考志愿"}],
        fallback_expert_id="career_plan_expert",
    )
    assert plan.action == "consult"
    assert plan.subtasks[0].target_expert_id == "career_plan_expert"
    assert len(client.calls) == 1


def test_run_orchestrator_head_retries_once_then_uses_fallback():
    bad = "not-json"
    client = ScriptedClient([bad, bad])
    plan = run_orchestrator_head(
        client,
        soul="s",
        head_rules="h",
        members=MEMBERS,
        policy=ConsultationPolicy(),
        profile={"school_stage": "高中"},
        messages=[{"role": "user", "content": "随便问问"}],
        fallback_expert_id="family_education_expert",
    )
    assert len(client.calls) == 2
    assert plan.action == "consult"
    assert plan.subtasks[0].target_expert_id == "family_education_expert"
    assert "validation_failed" in plan.reason


def test_run_orchestrator_head_clarify_when_no_fallback():
    client = ScriptedClient(["{}", "{}"])
    plan = run_orchestrator_head(
        client,
        soul="s",
        head_rules="h",
        members=MEMBERS,
        policy=ConsultationPolicy(),
        profile={"school_stage": "高中"},
        messages=[{"role": "user", "content": "你好"}],
        fallback_expert_id="",
    )
    assert plan.action == "clarify"
    assert plan.clarification_question
    assert plan.subtasks == []


def test_run_orchestrator_head_recovers_on_second_attempt():
    good = {
        "action": "clarify",
        "clarification_question": "你更关心升学还是家庭沟通？",
        "subtasks": [],
        "reason": "缺分流信息",
    }
    client = ScriptedClient(["bad", json.dumps(good, ensure_ascii=False)])
    plan = run_orchestrator_head(
        client,
        soul="s",
        head_rules="h",
        members=MEMBERS,
        policy=ConsultationPolicy(),
        profile={"school_stage": "高中"},
        messages=[{"role": "user", "content": "帮帮我"}],
        fallback_expert_id="career_plan_expert",
    )
    assert plan.action == "clarify"
    assert "升学" in plan.clarification_question
    assert len(client.calls) == 2
