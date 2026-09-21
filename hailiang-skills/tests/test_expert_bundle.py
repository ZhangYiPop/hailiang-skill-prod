from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hailiang_skills.api.routes.chat import build_chat_router
from hailiang_skills.api.routes.chat_stream import SwitchTeamMemberInput, _switch_team_member
from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.message_interactions import EXPIRED, ensure_message_interactions
from hailiang_skills.core.profile_candidate_archive import candidate_archive
from hailiang_skills.core.skill_display import build_skill_display
from hailiang_skills.core.skill_ids import EXPERT_DIRECT_EXECUTION_ID
from hailiang_skills.runtime_bridge.agentscope_expert_runtime import AgentScopeExpertRuntime, AgentScopeRuntimeUnavailable
from hailiang_skills.runtime_bridge.agent_frontmatter import parse_agent_frontmatter, validate_agent_skill_routing
from hailiang_skills.runtime_bridge.expert_bundle import (
    ExpertBundleError,
    ExpertDefinition,
    ExpertRegistry,
    LockedSkill,
    load_expert_bundle,
    load_local_expert_registry,
)
from hailiang_skills.runtime_bridge.expert_team_bundle import (
    ExpertTeamBundleError,
    ExpertTeamDefinition,
    ExpertTeamMember,
    ExpertTeamRegistry,
    build_expert_team_catalog,
    load_expert_team_bundle,
    load_local_expert_team_registry,
)
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry
from hailiang_skills.skill_runtime.models import AssistantTurnResult, ToolCallRequest
from hailiang_skills.storage.repositories.session_repo import InMemorySessionRepository


ROOT = Path(__file__).resolve().parents[1]


def _runtime_registry():
    return load_local_skill_registry(ROOT / "runtime_skills")


def _copy_default_bundle(tmp_path: Path) -> Path:
    target = tmp_path / "career_plan_expert"
    shutil.copytree(ROOT / "runtime_agents" / "career_plan_expert", target)
    return target


def test_default_expert_is_reference_only_and_locks_runtime_skills():
    registry = load_local_expert_registry(ROOT / "runtime_agents", _runtime_registry())
    expert = registry.require("career_plan_expert")
    assert expert.topology == "single_expert"
    assert "multi_path_planning" in expert.authorized_skill_ids
    assert not (expert.source_dir / "skills").exists()


def test_agent_frontmatter_strictly_matches_locked_skills_but_legacy_markdown_is_allowed():
    markdown = """---
skill_routing:
  rules:
    - skill_id: score_improve
      when: []
---
# 专家规则
按需要调用 Skill。
"""
    parsed = parse_agent_frontmatter(markdown)

    assert parsed.body.startswith("# 专家规则")
    assert parsed.routing_rules[0].skill_id == "score_improve"
    assert validate_agent_skill_routing(markdown, {"score_improve"}) == []
    assert validate_agent_skill_routing(markdown, {"score_improve", "subject_advisor"}) == [
        "AGENT.md skill_routing.rules 缺少当前绑定 Skill: subject_advisor"
    ]
    assert validate_agent_skill_routing("# 旧专家规则", {"score_improve"}) == []


def test_family_education_expert_reuses_only_the_two_central_runtime_skills():
    registry = load_local_expert_registry(ROOT / "runtime_agents", _runtime_registry())
    expert = registry.require("family_education_expert")

    assert expert.authorized_skill_ids == ("parenting_action_planner", "mbti_self_exploration")
    assert all(_runtime_registry().get(skill_id) is not None for skill_id in expert.authorized_skill_ids)


def test_expert_direct_reply_keeps_expert_identity_outside_general_chat():
    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    teams = load_local_expert_team_registry(ROOT / "runtime_agent_teams", experts)
    runtime = AgentScopeExpertRuntime(experts, skill_registry, team_registry=teams)
    context = SessionContext()
    context.session_meta.update({
        "expert_team_id": "student_growth_expert_team",
        "active_expert_id": "family_education_expert",
    })

    # Keep this focused on the runtime handoff contract; AgentScope itself is
    # covered separately by the tool-authorisation tests below.
    runtime._available = True
    runtime.client_factory = lambda _context: object()
    runtime._is_supported_client = lambda _client: True
    runtime._run_agent = lambda _definition, _message, _context, _client, state, **_kwargs: state.update(
        {"agent_reply": "先和孩子约定一个可执行的睡前流程。"}
    )
    legacy_calls: list[str] = []

    def legacy_handler(message, received_context):
        legacy_calls.append(message)
        payload = received_context.session_meta.get("expert_direct_reply")
        assert payload and payload["expert_id"] == "family_education_expert"
        return "expert-direct-result"

    result = runtime.handle_message("孩子总熬夜", context, legacy_handler)

    assert result == "expert-direct-result"
    assert legacy_calls == ["孩子总熬夜"]
    assert context.skill_states["agent_runtime"]["execution_mode"] == "expert_direct"
    assert context.skill_states["agent_runtime"]["expert_name"] == "家庭教育专家"


def test_shared_expert_runtime_instructs_agent_to_route_or_continue_active_skill():
    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    runtime = AgentScopeExpertRuntime(experts, skill_registry)
    context = SessionContext()
    context.session_meta["active_expert_id"] = "family_education_expert"
    context.interaction_state["active_skill"] = "mbti_self_exploration"
    runtime._available = True
    runtime.client_factory = lambda _context: object()
    runtime._is_supported_client = lambda _client: True
    instructions: list[str] = []

    def capture_agent(_definition, _message, _context, _client, state, **kwargs):
        instructions.append(str(kwargs.get("routing_instruction") or ""))
        state["agent_reply"] = "我会先结合你的亲子沟通目标给出建议。"

    runtime._run_agent = capture_agent
    runtime.handle_message("我更想解决亲子冲突", context, lambda _message, _context: "legacy-result")

    assert instructions
    assert "mbti_self_exploration" in instructions[0]
    assert "另一已授权 Skill" in instructions[0]
    assert "AGENT.md 是本轮选择的最高业务策略" in instructions[0]


def test_team_handoff_replaces_agentscope_iteration_error_with_user_message():
    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    teams = load_local_expert_team_registry(ROOT / "runtime_agent_teams", experts)
    runtime = AgentScopeExpertRuntime(experts, skill_registry, team_registry=teams)
    context = SessionContext()
    context.session_meta["expert_team_id"] = "student_growth_expert_team"
    team = teams.require("student_growth_expert_team")

    runtime._available = True
    runtime.client_factory = lambda _context: object()
    runtime._is_supported_client = lambda _client: True

    def incomplete_agent(_definition, _message, received_context, _client, state, **_kwargs):
        runtime._propose_member_handoff(
            team,
            state,
            received_context,
            ["family_education_expert"],
            "这个问题更适合聚焦亲子沟通。",
        )
        state["agent_reply"] = "The maximum reasoning-acting iterations are exceeded."

    runtime._run_agent = incomplete_agent
    captured: dict[str, object] = {}

    def legacy_handler(_message, received_context):
        captured.update(received_context.session_meta["expert_direct_reply"])
        received_context.add_message("assistant", str(captured["reply"]))
        return "expert-direct-result"

    result = runtime.handle_message("孩子高一后明显焦虑", context, legacy_handler)

    assert result == "expert-direct-result"
    assert "maximum reasoning" not in str(captured["reply"]).lower()
    assert "家庭教育专家" in str(captured["reply"])
    assert "转交卡" in str(captured["reply"])
    assert context.messages[-1]["team_handoff"]["candidates"][0]["expert_id"] == "family_education_expert"
    assert any(event["event_type"] == "expert_agent_reply_discarded" for event in context.event_trace)


def test_expert_direct_display_uses_active_expert_instead_of_legacy_fallback():
    context = SessionContext()
    context.skill_states["agent_runtime"] = {
        "expert_id": "family_education_expert",
        "expert_name": "家庭教育专家",
        "expert_team_id": "student_growth_expert_team",
    }
    context.skill_states["skill_runtime"] = {"active_skill_id": EXPERT_DIRECT_EXECUTION_ID}
    context.interaction_state["active_skill"] = EXPERT_DIRECT_EXECUTION_ID

    display = build_skill_display(context, runtime_registry=_runtime_registry())

    assert display["skill_id"] == EXPERT_DIRECT_EXECUTION_ID
    assert display["active_skill_label"] == "家庭教育专家"
    assert display["agent_label"] == "家庭教育专家"
    assert "专家团成员" in display["description"]


def test_student_growth_team_reuses_published_single_experts_only():
    experts = load_local_expert_registry(ROOT / "runtime_agents", _runtime_registry())
    teams = load_local_expert_team_registry(ROOT / "runtime_agent_teams", experts)
    team = teams.require("student_growth_expert_team")

    assert team.coordinator_expert_id == "career_plan_expert"
    assert team.member_expert_ids == ("career_plan_expert", "family_education_expert")
    assert team.member_for_mention("家庭教育专家").expert_id == "family_education_expert"


def test_team_import_rejects_missing_member_and_nested_team(tmp_path: Path):
    experts = load_local_expert_registry(ROOT / "runtime_agents", _runtime_registry())
    target = tmp_path / "team"
    shutil.copytree(ROOT / "runtime_agent_teams" / "student_growth_expert_team", target)
    config = target / "team.yaml"
    config.write_text(config.read_text(encoding="utf-8").replace("family_education_expert", "not_published"), encoding="utf-8")
    with pytest.raises(ExpertTeamBundleError, match="成员不存在"):
        load_expert_team_bundle(target, experts)
    config.write_text((ROOT / "runtime_agent_teams" / "student_growth_expert_team" / "team.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (target / "teams").mkdir()
    with pytest.raises(ExpertTeamBundleError, match="不能嵌套"):
        load_expert_team_bundle(target, experts)


def test_expert_catalog_has_no_out_of_band_session_mutation_api():
    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    repository = InMemorySessionRepository()
    context = SessionContext(session_id="expert-selection", user_id="u1", profile_id="p1")
    repository.create(context)
    orchestrator = SimpleNamespace(expert_registry=experts, runtime_registry=skill_registry)
    app = FastAPI()
    app.include_router(build_chat_router(repository, orchestrator, SimpleNamespace()), prefix="/api/v1")
    client = TestClient(app)

    catalog = client.get("/api/v1/experts")
    assert catalog.status_code == 200
    family = next(item for item in catalog.json()["experts"] if item["expert_id"] == "family_education_expert")
    assert family["skill_ids"] == ["parenting_action_planner", "mbti_self_exploration"]

    selected = client.put("/api/v1/sessions/expert-selection/expert", json={"expert_id": "family_education_expert"})
    assert selected.status_code == 404
    assert "expert_id" not in repository.get("expert-selection").session_meta


def test_expert_team_catalog_exposes_active_release_and_revision_audit_metadata():
    team = ExpertTeamDefinition(
        team_id="catalog_team",
        name="目录测试专家团",
        rules_markdown="团队规则",
        coordinator_expert_id="catalog_expert",
        members=(ExpertTeamMember("catalog_expert", "目录专家"),),
    )
    teams = ExpertTeamRegistry(definitions={team.team_id: team})
    experts = ExpertRegistry(definitions={
        "catalog_expert": ExpertDefinition("catalog_expert", "目录专家", "规则", (LockedSkill("catalog_skill", "v5"),)),
    })

    catalog = build_expert_team_catalog(
        teams,
        experts,
        metadata_by_team_id={
            "catalog_team": {
                "version": "v3",
                "release": {"release_id": "rel_3", "version": "v3"},
                "revision": {"revision_id": "rev_7", "version": "r7", "modified_by": "actor_1"},
                "modified_by": "actor_1",
                "modified_by_display_name": "业务修改人",
                "modified_at": "2026-09-08T00:00:00+00:00",
                "deployment": {"deployment_id": "deploy_1", "activated_by": "ops_1"},
            },
        },
        metadata_by_expert_id={
            "catalog_expert": {
                "version": "v4",
                "release": {"release_id": "rel_expert_4", "version": "v4"},
                "revision": {"revision_id": "rev_expert_9", "version": "r9"},
                "modified_by": "actor_2",
                "modified_by_display_name": "专家修改人",
                "modified_at": "2026-09-08T00:00:00+00:00",
            },
        },
        metadata_by_skill_id={
            "catalog_skill": {
                "release": {"release_id": "rel_skill_5", "version": "v5", "published_by": "actor_3"},
                "revision": {"revision_id": "rev_skill_11", "version": "r11"},
                "modified_by": "actor_3",
                "modified_by_display_name": "Skill 修改人",
                "modified_at": "2026-09-08T00:00:00+00:00",
            },
        },
    )

    item = catalog[0]
    assert item["version"] == "v3"
    assert item["revision"]["version"] == "r7"
    assert item["modified_by_display_name"] == "业务修改人"
    assert item["deployment"]["activated_by"] == "ops_1"
    assert item["members"][0]["version"] == "v4"
    assert item["members"][0]["skills"] == [{
        "skill_id": "catalog_skill",
        "version": "v5",
        "release": {"release_id": "rel_skill_5", "version": "v5", "published_by": "actor_3"},
        "revision": {"revision_id": "rev_skill_11", "version": "r11"},
        "modified_by": "actor_3",
        "modified_by_display_name": "Skill 修改人",
        "modified_at": "2026-09-08T00:00:00+00:00",
    }]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "runtime_skills Skill 不存在"),
        ("version", "Skill 版本不匹配"),
    ],
)
def test_expert_import_rejects_missing_or_wrong_version_skill(tmp_path: Path, mutation: str, message: str):
    bundle_dir = _copy_default_bundle(tmp_path)
    lock_path = bundle_dir / "skills.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if mutation == "missing":
        lock["skills"][0]["skill_id"] = "not_published"
        agent = (bundle_dir / "agent.yaml").read_text(encoding="utf-8")
        (bundle_dir / "agent.yaml").write_text(agent.replace("career_plan_entity", "not_published", 1), encoding="utf-8")
    elif mutation == "version":
        lock["skills"][0]["version"] = "999.0.0"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ExpertBundleError, match=message):
        load_expert_bundle(bundle_dir, _runtime_registry())


def test_expert_import_rejects_team_and_embedded_skills(tmp_path: Path):
    bundle_dir = _copy_default_bundle(tmp_path)
    agent_path = bundle_dir / "agent.yaml"
    agent_path.write_text(agent_path.read_text(encoding="utf-8").replace("single_expert", "team"), encoding="utf-8")
    with pytest.raises(ExpertBundleError, match="single_expert"):
        load_expert_bundle(bundle_dir, _runtime_registry())
    agent_path.write_text(agent_path.read_text(encoding="utf-8").replace("team", "single_expert"), encoding="utf-8")
    (bundle_dir / "skills").mkdir()
    with pytest.raises(ExpertBundleError, match="禁止携带"):
        load_expert_bundle(bundle_dir, _runtime_registry())


def test_expert_tool_rejects_unauthorized_skill_and_persists_handoff():
    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    runtime = AgentScopeExpertRuntime(experts, skill_registry)
    context = SessionContext()
    definition = experts.require("career_plan_expert")
    state = runtime._state(context, definition)
    state["budget"] = {"max_iters": 4, "max_skill_calls": 3, "skill_calls": 0}
    with pytest.raises(ValueError, match="未授权"):
        runtime._execute_skill(definition, state, context, "not_published", "test", None)
    result = runtime._execute_skill(definition, state, context, "score_improve", "如何提分", {"facts": {"grade": "高二"}})
    assert result["status"] == "scheduled"
    assert context.session_meta["expert_requested_skill_id"] == "score_improve"
    assert state["budget"]["skill_calls"] == 1


def test_expert_candidate_fact_is_profile_scoped_and_not_effective_fact():
    skill_registry = _runtime_registry()
    definition = ExpertDefinition(
        agent_id="interest_expert",
        name="兴趣探索专家",
        rules_markdown="记录长期可复用的兴趣倾向。",
        skills=(),
    )
    runtime = AgentScopeExpertRuntime(
        ExpertRegistry(definitions={definition.agent_id: definition}),
        skill_registry,
    )
    context = SessionContext(session_id="candidate_session", user_id="user_1", profile_id="child_a")

    result = runtime._record_candidate_fact(
        context,
        definition,
        source_turn_id="expert_turn_1",
        fact_key="sport_preference",
        value="爱运动",
        confidence=0.74,
        evidence_summary="用户描述孩子喜欢运动并愿意尝试新活动。",
    )

    assert result["status"] == "candidate_archived"
    assert context.known_facts.get_value("conversation.interest_expert.sport_preference") is None
    archive = candidate_archive(context)
    assert archive[0]["source_turn_id"] == "expert_turn_1"
    assert archive[0]["observed_at"]
    assert any(event["event_type"] == "expert_candidate_fact_recorded" for event in context.event_trace)


def test_single_skill_expert_still_uses_agent_rules_for_a_direct_reply():
    skill_registry = _runtime_registry()
    definition = ExpertDefinition(
        agent_id="single_skill_expert",
        name="单技能专家",
        rules_markdown="普通咨询由专家直接回答，提分计划才调用技能。",
        skills=(LockedSkill("score_improve", "v1"),),
    )
    runtime = AgentScopeExpertRuntime(
        ExpertRegistry(definitions={definition.agent_id: definition}),
        skill_registry,
    )
    context = SessionContext()
    context.session_meta["active_expert_id"] = definition.agent_id
    runtime._available = True
    runtime.client_factory = lambda _context: object()
    runtime._is_supported_client = lambda _client: True
    calls: list[str] = []

    def decide(_definition, _message, _context, _client, state, **_kwargs):
        calls.append("agent")
        state["agent_reply"] = "美国和英国各有优势，建议先看你的专业和预算。"

    runtime._run_agent = decide

    result = runtime.handle_message(
        "去美国好还是英国好？",
        context,
        lambda _message, received_context: received_context.session_meta["expert_direct_reply"]["reply"],
    )

    assert result.startswith("美国和英国")
    assert calls == ["agent"]
    assert context.skill_states["agent_runtime"]["execution_mode"] == "expert_direct"
    assert not any(event["event_type"] == "expert_skill_executed" for event in context.event_trace)


def test_structured_route_selects_authorized_skill_before_any_expert_reply():
    class RouteClient:
        def complete(self, _messages, **_kwargs):
            return json.dumps({
                "mode": "execute_skill",
                "skill_id": "score_improve",
                "candidate_skill_ids": ["score_improve"],
                "confidence": 0.94,
                "agent_policy_basis": "",
                "scope_decision": "in_scope",
                "skill_scope_basis": "当前版本是占位版子场景 skill，用于验证 runtime 的多 skill 路由、facts 继承与状态切换能力。",
                "reason": "用户明确咨询提分方案",
            })

        def last_request_metrics(self):
            return {}

    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    definition = experts.require("career_plan_expert")
    runtime = AgentScopeExpertRuntime(experts, skill_registry)
    context = SessionContext()
    state = runtime._state(context, definition)
    state["budget"] = {"max_iters": 4, "max_skill_calls": 3, "skill_calls": 0}

    runtime._route_answering_expert_turn(definition, "怎么提高数学成绩", context, RouteClient(), state)

    assert context.session_meta["expert_requested_skill_id"] == "score_improve"
    assert "agent_reply" not in state
    assert any(event["event_type"] == "expert_skill_route_selected" for event in context.event_trace)
    assert any(event["event_type"] == "expert_skill_executed" for event in context.event_trace)


def test_pending_questionnaire_does_not_lock_unrelated_expert_question_to_skill():
    class RouteClient:
        def __init__(self):
            self.calls = 0

        def complete(self, _messages, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({
                    "mode": "execute_skill",
                    "skill_id": "score_improve",
                    "candidate_skill_ids": ["score_improve"],
                    "confidence": 0.95,
                    "reason": "沿用当前挂起表单",
                })
            return json.dumps({
                "mode": "direct_reply",
                "skill_id": "",
                "candidate_skill_ids": ["score_improve"],
                "confidence": 0.96,
                "scope_decision": "out_of_scope",
                "skill_scope_basis": "该 Skill 只处理学习提分，不处理学校综合实力评价。",
                "direct_reply_reason": "用户改问学校综合实力",
                "reason": "当前问题已改变",
            })

        def last_request_metrics(self):
            return {}

    skill_registry = _runtime_registry()
    definition = ExpertDefinition(
        agent_id="score_expert_pending",
        name="提分专家",
        rules_markdown="需要专项提分计划时使用已授权 Skill。",
        skills=(LockedSkill("score_improve", "v1"),),
    )
    runtime = AgentScopeExpertRuntime(
        ExpertRegistry(definitions={definition.agent_id: definition}),
        skill_registry,
    )
    context = SessionContext()
    context.skill_states["skill_runtime"] = {
        "active_skill_id": "score_improve",
        "skill_facts": {
            "score_improve": {
                "_pending_questionnaire": {
                    "form_id": "score_context",
                    "question_ids": ["subject", "target_score"],
                },
            },
        },
    }

    decision = runtime._decide_authorized_skill(
        definition,
        "MIT 的综合实力如何？",
        context,
        RouteClient(),
        active_skill_id="score_improve",
    )

    assert decision["mode"] == "direct_reply"
    assert decision["reason"] == "当前问题已改变"
    assert any(
        event["event_type"] == "expert_skill_scope_inspection_requested"
        and event["payload"]["reason"] == "execute_skill_needs_skill_boundary_check"
        for event in context.event_trace
    )


def test_missing_requested_skill_falls_back_to_expert_and_records_event():
    class RouteClient:
        def complete(self, _messages, **_kwargs):
            return json.dumps({
                "mode": "execute_skill",
                "skill_id": "missing_skill",
                "candidate_skill_ids": ["missing_skill"],
                "confidence": 0.9,
                "reason": "模型误选了一个已不存在的 Skill",
            })

        def last_request_metrics(self):
            return {}

    definition = ExpertDefinition(
        agent_id="fallback_expert",
        name="兜底专家",
        rules_markdown="无法使用专项 Skill 时由专家直接回答。",
        skills=(LockedSkill("missing_skill", "v1"),),
    )
    runtime = AgentScopeExpertRuntime(
        ExpertRegistry(definitions={definition.agent_id: definition}),
        _runtime_registry(),
    )
    context = SessionContext()
    decision = runtime._decide_authorized_skill(
        definition, "请帮我分析这个问题", context, RouteClient(),
    )

    assert decision["mode"] == "direct_reply"
    assert decision["skill_id"] == ""
    assert any(
        event["event_type"] == "expert_skill_unavailable_fallback"
        and event["payload"]["skill_id"] == "missing_skill"
        for event in context.event_trace
    )


def test_expert_explicit_grade_is_saved_to_session_scope_without_overwriting_profile():
    runtime = AgentScopeExpertRuntime(ExpertRegistry(definitions={}), _runtime_registry())
    context = SessionContext(profile_id="profile_001")
    context.update_fact("grade", "高一", source_skill="profile", scope="profile")

    runtime._capture_explicit_user_facts(context, "孩子现在五年级了", source_turn_id="turn_001")

    assert context.profile_facts.get_value("grade") == "高一"
    assert context.session_facts.get_value("grade") == "五年级"
    assert context.known_facts.get_value("grade") == "五年级"
    assert any(event["event_type"] == "expert_explicit_fact_captured" for event in context.event_trace)


def test_high_relevance_direct_reply_without_agent_quote_is_scope_checked_not_forced():
    class RouteClient:
        def __init__(self):
            self.calls = 0

        def complete(self, _messages, **_kwargs):
            self.calls += 1
            return json.dumps({
                "mode": "direct_reply",
                "skill_id": "",
                "candidate_skill_ids": ["score_improve"],
                "confidence": 0.9,
                "agent_policy_basis": "没有这条规则",
                "direct_reply_reason": "专家自己回答",
                "reason": "错误直答",
            })

        def last_request_metrics(self):
            return {}

    skill_registry = _runtime_registry()
    definition = ExpertDefinition(
        agent_id="score_expert",
        name="提分专家",
        rules_markdown="需要专项提分计划时使用已授权 Skill。",
        skills=(LockedSkill("score_improve", "v1"),),
    )
    runtime = AgentScopeExpertRuntime(ExpertRegistry(definitions={definition.agent_id: definition}), skill_registry)
    context = SessionContext()
    state = runtime._state(context, definition)
    state["budget"] = {"max_iters": 4, "max_skill_calls": 3, "skill_calls": 0}

    runtime._route_answering_expert_turn(definition, "我想要一个数学提分计划", context, RouteClient(), state)

    assert "expert_requested_skill_id" not in context.session_meta
    assert any(event["event_type"] == "expert_skill_scope_inspection_requested" for event in context.event_trace)
    assert any(event["event_type"] == "expert_direct_reply_preserved" for event in context.event_trace)


def test_scope_check_allows_skill_only_when_full_skill_confirms_in_scope():
    class RouteClient:
        def __init__(self):
            self.calls = 0

        def complete(self, _messages, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({
                    "mode": "direct_reply", "skill_id": "", "candidate_skill_ids": ["SCORE_IMPROVE"],
                    "confidence": 0.8, "reason": "需要先复核 Skill 范围",
                })
            return json.dumps({
                "mode": "direct_reply", "skill_id": "", "candidate_skill_ids": ["SCORE_IMPROVE"],
                "confidence": 0.95, "scope_decision": "in_scope",
                "skill_scope_basis": "当前版本是占位版子场景 skill，用于验证 runtime 的多 skill 路由、facts 继承与状态切换能力。",
                "reason": "用户明确需要提分规划",
            })

        def last_request_metrics(self):
            return {}

    skill_registry = _runtime_registry()
    definition = ExpertDefinition(
        agent_id="score_expert_scope",
        name="提分专家",
        rules_markdown="需要专项提分计划时使用已授权 Skill。",
        skills=(LockedSkill("score_improve", "v1"),),
    )
    runtime = AgentScopeExpertRuntime(ExpertRegistry(definitions={definition.agent_id: definition}), skill_registry)
    context = SessionContext()
    state = runtime._state(context, definition)
    state["budget"] = {"max_iters": 4, "max_skill_calls": 3, "skill_calls": 0}

    runtime._route_answering_expert_turn(definition, "请帮我做数学提分规划", context, RouteClient(), state)

    assert context.session_meta["expert_requested_skill_id"] == "score_improve"
    selected = [e for e in context.event_trace if e["event_type"] == "expert_skill_route_selected"][-1]
    assert selected["payload"]["forced_skill_execution"] is True


def test_expert_does_not_bypass_agent_when_decision_client_is_unavailable():
    skill_registry = _runtime_registry()
    definition = ExpertDefinition(
        agent_id="single_skill_expert",
        name="单技能专家",
        rules_markdown="仅在必要时调用技能。",
        skills=(LockedSkill("score_improve", "v1"),),
    )
    runtime = AgentScopeExpertRuntime(ExpertRegistry(definitions={definition.agent_id: definition}), skill_registry)
    context = SessionContext()
    context.session_meta["active_expert_id"] = definition.agent_id
    runtime._available = True
    runtime.client_factory = lambda _context: None

    with pytest.raises(AgentScopeRuntimeUnavailable, match="专家决策暂不可用"):
        runtime.handle_message("我想提分", context, lambda *_args: pytest.fail("不得执行唯一 Skill"))

    assert any(event["event_type"] == "expert_decision_unavailable" for event in context.event_trace)
    assert not any(event["event_type"] == "expert_skill_executed" for event in context.event_trace)


def test_expert_history_and_reply_bounds_are_configurable():
    runtime = AgentScopeExpertRuntime(
        ExpertRegistry(definitions={}),
        _runtime_registry(),
        history_messages=2,
        history_message_chars=4,
        history_max_chars=10,
        reply_max_chars=2_000,
    )
    context = SessionContext()
    context.messages = [
        {"role": "user", "content": "第一轮用户消息"},
        {"role": "assistant", "content": "第一轮专家回复"},
        {"role": "user", "content": "第二轮用户消息"},
    ]

    history = runtime._expert_history_messages(context)

    assert history == [
        {"role": "assistant", "content": "第一轮专"},
        {"role": "user", "content": "第二轮用"},
    ]
    assert len(runtime._expert_conversation_history(context)) <= 10
    assert len(runtime._limit_reply("x" * 2_001)) == 2_000


def test_expert_reply_limit_records_confirmed_application_truncation():
    runtime = AgentScopeExpertRuntime(ExpertRegistry(definitions={}), _runtime_registry(), reply_max_chars=1_000)
    context = SessionContext()

    reply = runtime._limit_reply(
        "x" * 1_001,
        context=context,
        source="expert_agent_reply",
        expert_id="study_abroad_consultant",
        expert_turn_id="expert_turn_001",
    )

    assert len(reply) == 1_000
    event = context.event_trace[-1]
    assert event["event_type"] == "model_output_truncated"
    assert event["payload"]["truncation_reason_code"] == "application_reply_char_limit"
    assert event["payload"]["received_chars"] == 1_001


def test_expert_model_completion_records_upstream_length_reason():
    runtime = AgentScopeExpertRuntime(ExpertRegistry(definitions={}), _runtime_registry())
    context = SessionContext()

    runtime._record_model_completion(
        context,
        result=AssistantTurnResult(final_text="未完成的回答"),
        metrics={"finish_reason": "length", "configured_max_tokens": 384000, "output_tokens": 384000},
        source="expert_agent",
        expert_id="study_abroad_consultant",
        expert_turn_id="expert_turn_001",
    )

    assert [event["event_type"] for event in context.event_trace] == [
        "model_output_completion", "model_output_truncated",
    ]
    assert context.event_trace[-1]["payload"]["truncation_reason_code"] == "upstream_finish_reason_length"


def test_coordinator_can_propose_team_handoff_but_member_cannot_route():
    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    teams = load_local_expert_team_registry(ROOT / "runtime_agent_teams", experts)
    runtime = AgentScopeExpertRuntime(experts, skill_registry, team_registry=teams)
    context = SessionContext()
    team = teams.require("student_growth_expert_team")
    coordinator = experts.require(team.coordinator_expert_id)
    state = runtime._state(context, coordinator, team=team)
    handoff = runtime._propose_member_handoff(team, state, context, ["family_education_expert"], "更适合讨论亲子沟通")

    assert handoff["status"] == "awaiting_user_confirmation"
    assert handoff["candidates"][0]["mention_name"] == "家庭教育专家"
    assert context.session_meta["pending_team_handoff"]["team_id"] == team.team_id


def _controlled_student_team_runtime():
    skill_registry = _runtime_registry()
    skill = LockedSkill("score_improve", "v1")
    definitions = {
        "coordinator": ExpertDefinition("coordinator", "主协调专家", "负责澄清与分流。", (skill,)),
        "academic_coach": ExpertDefinition("academic_coach", "学习指导师", "负责学习提升。", (skill,)),
        "talent_dev_specialist": ExpertDefinition("talent_dev_specialist", "特长发展专家", "负责特长发展。", (skill,)),
        "career_explore_mentor": ExpertDefinition("career_explore_mentor", "职业探索导师", "负责职业探索。", (skill,)),
        "study_abroad_consultant": ExpertDefinition("study_abroad_consultant", "留学咨询师", "负责留学咨询。", (skill,)),
    }
    team = ExpertTeamDefinition(
        team_id="student_growth_expert_team",
        name="学生成长专家团",
        rules_markdown="主协调专家必须先确认转交。",
        coordinator_expert_id="coordinator",
        members=(
            ExpertTeamMember("coordinator", "主协调专家", "泛泛问题澄清"),
            ExpertTeamMember("academic_coach", "学习指导师", "学习提升、学习方法、学科问题"),
            ExpertTeamMember("talent_dev_specialist", "特长发展专家", "绘画、艺术、特长发展"),
            ExpertTeamMember("career_explore_mentor", "职业探索导师", "职业方向、生涯探索"),
            ExpertTeamMember("study_abroad_consultant", "留学咨询师", "留学、海外院校"),
        ),
    )
    runtime = AgentScopeExpertRuntime(
        ExpertRegistry(definitions=definitions),
        skill_registry,
        team_registry=ExpertTeamRegistry(definitions={team.team_id: team}),
        default_expert_id="coordinator",
    )
    runtime._available = True
    runtime.client_factory = lambda _context: object()
    runtime._is_supported_client = lambda _client: True
    return runtime, team


def test_coordinator_forces_single_specialist_handoff_card_when_react_replies_directly():
    runtime, team = _controlled_student_team_runtime()
    context = SessionContext()
    context.session_meta.update({"expert_team_id": team.team_id, "active_expert_id": "coordinator"})
    runtime._run_agent = lambda _definition, _message, _context, _client, state, **_kwargs: state.update(agent_reply="这里是未受控的专项结论")

    runtime.handle_message("高一数学提分怎么安排", context, lambda _message, received: received.add_message("assistant", received.session_meta["expert_direct_reply"]["reply"]) or "ok")

    handoff = context.messages[-1]["team_handoff"]
    assert [candidate["expert_id"] for candidate in handoff["candidates"]] == ["academic_coach"]
    assert "转交卡" in context.messages[-1]["content"]
    assert any(event["event_type"] == "team_handoff_controlled_selected" for event in context.event_trace)


def test_coordinator_proposes_multiple_specialists_for_explicit_competing_topics():
    runtime, team = _controlled_student_team_runtime()
    context = SessionContext()
    context.session_meta.update({"expert_team_id": team.team_id, "active_expert_id": "coordinator"})
    runtime._run_agent = lambda *_args, **_kwargs: None

    runtime.handle_message("我想同时了解职业方向和留学选择", context, lambda _message, received: received.add_message("assistant", received.session_meta["expert_direct_reply"]["reply"]) or "ok")

    candidates = context.messages[-1]["team_handoff"]["candidates"]
    assert {candidate["expert_id"] for candidate in candidates} == {"career_explore_mentor", "study_abroad_consultant"}


def test_handoff_tool_normalizes_exact_name_and_records_unknown_alias_rejection():
    runtime, team = _controlled_student_team_runtime()
    context = SessionContext()
    state = runtime._state(context, runtime.expert_registry.require("coordinator"), team=team)
    handoff = runtime._propose_member_handoff(team, state, context, ["@学习指导师"], "学习问题")
    assert handoff["candidates"][0]["expert_id"] == "academic_coach"

    with pytest.raises(ValueError, match="一至三位"):
        runtime._propose_member_handoff(team, state, context, ["不存在的专家"], "学习问题")
    rejected = [event for event in context.event_trace if event["event_type"] == "team_handoff_rejected"]
    assert rejected[-1]["payload"]["rejected"] == [{"candidate": "不存在的专家", "reason": "unknown_member_alias"}]


def test_handoff_candidate_is_resolved_from_current_snapshot_not_global_registry():
    runtime, team = _controlled_student_team_runtime()
    runtime.expert_registry.definitions.pop("career_explore_mentor")
    context = SessionContext()
    context.session_meta["configuration_snapshot"] = {
        "entries": [{
            "object_type": "expert",
            "object_key": "career_explore_mentor",
            "name": "职业探索导师",
            "payload": {"rules_markdown": "负责职业探索。"},
            "dependency_locks": [{"object_key": "score_improve", "release_no": 1}],
        }],
    }
    state = runtime._state(context, runtime.expert_registry.require("coordinator"), team=team)

    handoff = runtime._propose_member_handoff(team, state, context, ["职业探索导师"], "职业相关问题")

    assert handoff["candidates"] == [{
        "expert_id": "career_explore_mentor",
        "name": "职业探索导师",
        "mention_name": "职业探索导师",
        "brief": "职业方向、生涯探索",
    }]


def test_handoff_candidate_falls_back_to_the_expert_brief_when_routing_brief_is_empty():
    registry = ExpertRegistry(definitions={
        "coordinator": ExpertDefinition(
            agent_id="coordinator", name="协调专家", rules_markdown="协调规则", skills=(), brief="协调摘要",
        ),
        "study_abroad": ExpertDefinition(
            agent_id="study_abroad", name="留学咨询师", rules_markdown="留学规则", skills=(), brief="帮助用户了解留学相关事宜",
        ),
    })
    team = ExpertTeamDefinition(
        team_id="brief_team",
        name="摘要团队",
        rules_markdown="团队规则",
        coordinator_expert_id="coordinator",
        members=(
            ExpertTeamMember(expert_id="coordinator", mention_name="协调专家"),
            ExpertTeamMember(expert_id="study_abroad", mention_name="留学咨询师", routing_brief=""),
        ),
    )
    runtime = AgentScopeExpertRuntime(
        registry,
        SimpleNamespace(),
        team_registry=ExpertTeamRegistry(definitions={team.team_id: team}),
    )
    context = SessionContext()
    state = runtime._state(context, registry.require("coordinator"), team=team)

    handoff = runtime._propose_member_handoff(team, state, context, ["study_abroad"], "留学问题")

    assert handoff["candidates"][0]["brief"] == "帮助用户了解留学相关事宜"

    registry.definitions["study_abroad"] = ExpertDefinition(
        agent_id="study_abroad", name="留学咨询师", rules_markdown="留学规则", skills=(), brief="",
    )
    routing_only_team = ExpertTeamDefinition(
        team_id="routing_only_team", name="路由摘要团队", rules_markdown="团队规则",
        coordinator_expert_id="coordinator",
        members=(
            ExpertTeamMember(expert_id="coordinator", mention_name="协调专家"),
            ExpertTeamMember(expert_id="study_abroad", mention_name="留学咨询师", routing_brief="留学申请与院校选择"),
        ),
    )
    routing_handoff = runtime._propose_member_handoff(
        routing_only_team,
        runtime._state(SessionContext(), registry.require("coordinator"), team=routing_only_team),
        SessionContext(),
        ["study_abroad"],
        "留学问题",
    )
    assert routing_handoff["candidates"][0]["brief"] == "留学申请与院校选择"

    reason_only_team = ExpertTeamDefinition(
        team_id="reason_only_team", name="原因摘要团队", rules_markdown="团队规则",
        coordinator_expert_id="coordinator",
        members=(
            ExpertTeamMember(expert_id="coordinator", mention_name="协调专家"),
            ExpertTeamMember(expert_id="study_abroad", mention_name="留学咨询师", routing_brief=""),
        ),
    )
    reason_handoff = runtime._propose_member_handoff(
        reason_only_team,
        runtime._state(SessionContext(), registry.require("coordinator"), team=reason_only_team),
        SessionContext(),
        ["study_abroad"],
        "当前问题更适合由留学咨询师协助",
    )
    assert reason_handoff["candidates"][0]["brief"] == "当前问题更适合由留学咨询师协助"


def test_expert_history_keeps_previous_user_context_and_omits_handoff_confirmation():
    runtime, _team = _controlled_student_team_runtime()
    context = SessionContext()
    context.add_message("user", "我现在是高一")
    context.add_message("assistant", "想重点了解什么？")
    context.add_message("user", "@职业探索导师", metadata={"message_type": "team_handoff_confirmation"})

    history = runtime._expert_conversation_history(context)

    assert "我现在是高一" in history
    assert "想重点了解什么" in history
    assert "@职业探索导师" not in history


def test_coordinator_framework_limit_falls_back_to_clarification_not_generic_apology():
    runtime, team = _controlled_student_team_runtime()
    context = SessionContext()
    context.session_meta.update({"expert_team_id": team.team_id, "active_expert_id": "coordinator"})
    runtime._run_agent = lambda _definition, _message, _context, _client, state, **_kwargs: state.update(agent_reply="Maximum reasoning-acting iterations reached")
    runtime._generate_team_clarification_reply = lambda *_args, **_kwargs: "你提到自己正在读高一，目前更想探索哪一类职业？"

    runtime.handle_message("你好", context, lambda _message, received: received.add_message("assistant", received.session_meta["expert_direct_reply"]["reply"]) or "ok")

    assert "正在读高一" in context.messages[-1]["content"]
    assert "没有完成处理" not in context.messages[-1]["content"]
    assert any(event["event_type"] == "team_handoff_clarification" for event in context.event_trace)


def test_unstructured_at_text_does_not_switch_team_member():
    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    teams = load_local_expert_team_registry(ROOT / "runtime_agent_teams", experts)
    runtime = AgentScopeExpertRuntime(experts, skill_registry, team_registry=teams)
    context = SessionContext()
    team = teams.require("student_growth_expert_team")

    context.session_meta["active_expert_id"] = team.coordinator_expert_id
    content = runtime._apply_structured_team_switch(context, team, "@家庭教育专家 孩子沉迷手机怎么办")
    assert content == "@家庭教育专家 孩子沉迷手机怎么办"
    assert context.session_meta["active_expert_id"] == team.coordinator_expert_id


def test_confirmed_team_handoff_carries_source_question_and_keeps_visible_mention():
    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    teams = load_local_expert_team_registry(ROOT / "runtime_agent_teams", experts)
    runtime = AgentScopeExpertRuntime(experts, skill_registry, team_registry=teams)
    context = SessionContext()
    context.add_message("user", "我的孩子比较叛逆，怎么办")
    context.add_message("assistant", "建议家庭教育专家接管")
    context.session_meta["team_member_switch"] = {
        "source": "team_handoff",
        "target_expert_id": "family_education_expert",
        "visible_user_message": "@家庭教育专家",
        "source_user_message": "我的孩子比较叛逆，怎么办",
        "coordinator_reason": "更适合处理亲子沟通",
        "conversation_excerpt": "用户：我的孩子比较叛逆，怎么办",
    }

    content = runtime._apply_structured_team_switch(
        context,
        teams.require("student_growth_expert_team"),
        "专家接管",
    )

    assert "我的孩子比较叛逆，怎么办" in content
    assert "更适合处理亲子沟通" in content
    assert context.session_meta["team_handoff_visible_user_message"] == "@家庭教育专家"
    assert context.session_meta["team_handoff_visible_user_message_type"] == "team_handoff_confirmation"
    assert context.session_meta["team_handoff_visible_user_message_metadata"] == {
        "source_message_id": "",
        "target_expert_id": "family_education_expert",
        "expert_team_id": "student_growth_expert_team",
        "source": "team_handoff",
        "source_profile_id": None,
        "execution_profile_id": None,
        "cross_profile": False,
    }


def test_structured_toolbar_switch_uses_expert_id_and_keeps_content_separate():
    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    teams = load_local_expert_team_registry(ROOT / "runtime_agent_teams", experts)
    runtime = AgentScopeExpertRuntime(experts, skill_registry, team_registry=teams)
    context = SessionContext()
    team = teams.require("student_growth_expert_team")
    context.session_meta.update({
        "expert_team_id": team.team_id,
        "active_expert_id": team.coordinator_expert_id,
        "team_member_switch": {
            "source": "toolbar",
            "from_expert_id": team.coordinator_expert_id,
            "target_expert_id": "family_education_expert",
            "content": "孩子沉迷手机怎么办",
            "visible_user_message": "@家庭教育专家 孩子沉迷手机怎么办",
            "conversation_excerpt": "用户：孩子最近总熬夜",
        },
    })

    content = runtime._apply_structured_team_switch(context, team, "孩子沉迷手机怎么办")

    assert "当前问题：\n孩子沉迷手机怎么办" in content
    assert context.session_meta["active_expert_id"] == "family_education_expert"
    assert context.session_meta["team_handoff_visible_user_message"].startswith("@家庭教育专家")


def test_toolbar_switch_request_validates_team_member_by_id():
    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    teams = load_local_expert_team_registry(ROOT / "runtime_agent_teams", experts)
    team = teams.require("student_growth_expert_team")
    context = SessionContext()
    context.session_meta.update({
        "expert_team_id": team.team_id,
        "active_expert_id": team.coordinator_expert_id,
        "expert_id": team.coordinator_expert_id,
    })
    context.messages.append({
        "role": "assistant",
        "message_id": "msg_pending_form",
        "content": "请先填写这份表单。",
        "blocks": [{"type": "fact_form", "payload": {"form_id": "family_context", "fields": []}}],
    })
    context.skill_states["skill_runtime"] = {
        "active_skill_id": "family_education",
        "skill_facts": {"family_education": {"_pending_questionnaire": {"form_id": "family_context"}}},
        "status_flags": {"native_questionnaire_form": {"payload": {"form_id": "family_context"}}},
    }
    switch = _switch_team_member(
        context,
        SimpleNamespace(expert_team_registry=teams),
        SwitchTeamMemberInput(
            action="switch_team_member",
            context_scope="profile",
            source="toolbar",
            target_expert_id="family_education_expert",
            content="孩子沉迷手机怎么办",
            expert_context={
                "expert_team_id": team.team_id,
                "expert_id": team.coordinator_expert_id,
                "expected_branch_version": 0,
                "operation": "continue",
            },
        ),
    )

    assert switch["target_expert_id"] == "family_education_expert"
    assert switch["content"] == "孩子沉迷手机怎么办"
    assert switch["visible_user_message"] == "@家庭教育专家 孩子沉迷手机怎么办"
    assert ensure_message_interactions(context.messages[0])["fact_form:family_context"]["status"] == EXPIRED
    assert "_pending_questionnaire" not in context.skill_states["skill_runtime"]["skill_facts"]["family_education"]
    assert context.skill_states["skill_runtime"]["active_skill_id"] == ""
    assert "native_questionnaire_form" not in context.skill_states["skill_runtime"]["status_flags"]
    abandoned = [event for event in context.event_trace if event["event_type"] == "form_abandoned"]
    assert abandoned and abandoned[-1]["payload"]["reason"] == "switch_team_member"


def test_team_handoff_sse_state_is_direct_and_fixed_shape():
    from hailiang_skills.core.sse_protocol import SseEnvelopeBuilder

    builder = SseEnvelopeBuilder(run_id="run_team", session_id="session_team")
    handoff = {
        "team_id": "student_growth_expert_team",
        "reason": "更适合处理亲子沟通",
        "candidates": [{"expert_id": "family_education_expert", "mention_name": "家庭教育专家"}],
    }
    payload = builder.encode("team_handoff", handoff)

    assert payload is not None
    assert builder.snapshot()["team_handoff"] == handoff


def test_expert_sse_state_is_fixed_and_authoritative():
    from hailiang_skills.core.sse_protocol import SseEnvelopeBuilder

    builder = SseEnvelopeBuilder(run_id="run_expert", session_id="session_expert")
    assert builder.snapshot()["expert"] == {"mode": "none", "team": {}, "active": {}, "activation": {}, "transition": {}}
    builder.encode("expert_context", {
        "mode": "team",
        "team": {"team_id": "student_growth_expert_team", "coordinator_expert_id": "career_plan_expert"},
        "active": {"expert_id": "family_education_expert", "name": "家庭教育专家", "mention_name": "家庭教育专家", "is_coordinator": False},
        "activation": {"source": "explicit_or_restored", "is_default": False, "selection_source": "manual"},
        "transition": {"status": "completed", "source": "toolbar", "from_expert_id": "career_plan_expert", "to_expert_id": "family_education_expert", "source_message_id": None},
    })
    assert builder.snapshot()["expert"]["active"]["expert_id"] == "family_education_expert"
    assert builder.snapshot()["expert"]["activation"]["is_default"] is False
    assert builder.snapshot()["expert"]["transition"]["source"] == "toolbar"

    legacy_snapshot = builder.snapshot()
    legacy_snapshot["expert"].pop("activation")
    resumed = SseEnvelopeBuilder(run_id="run_expert", session_id="session_expert")
    resumed.restore(legacy_snapshot)
    assert resumed.snapshot()["expert"]["activation"] == {}


def test_team_default_coordinator_has_an_explicit_sse_activation_marker():
    from hailiang_skills.core.streaming_runner import _expert_state_payload

    skills = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skills)
    teams = load_local_expert_team_registry(ROOT / "runtime_agent_teams", experts)
    context = SessionContext()
    context.session_meta.update({
        "expert_team_id": "student_growth_expert_team",
        "expert_id": "career_plan_expert",
        "active_expert_id": "career_plan_expert",
        "expert_selection_source": "manual_team",
    })

    payload = _expert_state_payload(context, SimpleNamespace(expert_registry=experts, expert_team_registry=teams))

    assert payload["active"]["is_coordinator"] is True
    assert payload["activation"] == {
        "source": "team_default_coordinator",
        "is_default": True,
        "selection_source": "manual_team",
    }


def test_agentscope_react_agent_can_only_select_an_authorized_skill():
    class FakeOpenAICompatibleClient:
        _config = SimpleNamespace(api_key="test", base_url="http://example.invalid", model="test")

        def __init__(self) -> None:
            self.calls = 0

        def complete_with_tools(self, _messages, _specs, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return AssistantTurnResult(
                    tool_calls=(
                        ToolCallRequest(
                            id="call_1",
                            name="execute_skill",
                            arguments={"skill_id": "score_improve", "task": "给出提分建议"},
                        ),
                    )
                )
            return AssistantTurnResult(final_text="已选择提分 Skill。")

    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    runtime = AgentScopeExpertRuntime(experts, skill_registry)
    context = SessionContext()
    definition = experts.require("career_plan_expert")
    state = runtime._state(context, definition)
    state["budget"] = {"max_iters": 4, "max_skill_calls": 3, "skill_calls": 0}
    runtime._run_agent(definition, "怎么提分", context, FakeOpenAICompatibleClient(), state)
    assert context.session_meta["expert_requested_skill_id"] == "score_improve"
    assert state["budget"]["skill_calls"] == 1


def test_expert_prompt_includes_active_profile_facts_and_forbids_reasking_them():
    class FactCapturingClient:
        _config = SimpleNamespace(api_key="test", base_url="http://example.invalid", model="test")

        def __init__(self) -> None:
            self.messages = []

        def complete_with_tools(self, messages, _specs, **_kwargs):
            self.messages = messages
            return AssistantTurnResult(final_text="已知孩子目前是初中阶段。")

    skill_registry = _runtime_registry()
    experts = load_local_expert_registry(ROOT / "runtime_agents", skill_registry)
    runtime = AgentScopeExpertRuntime(experts, skill_registry)
    context = SessionContext(profile_id="profile_x", profile_name="许琳")
    context.profile_facts.set_fact("grade", "初中", source_skill="project_backend", scope="profile")
    context.profile_facts.set_fact(
        "profile_school_facts",
        [{"school_year": "2019", "grade": "初中"}],
        source_skill="project_backend",
        scope="profile",
    )
    definition = experts.require("career_plan_expert")
    state = runtime._state(context, definition)
    state["budget"] = {"max_iters": 4, "max_skill_calls": 3, "skill_calls": 0}
    client = FactCapturingClient()

    runtime._run_agent(definition, "你好", context, client, state)

    prompt = "\n".join(str(message) for message in client.messages)
    assert "当前孩子的有效事实" in prompt
    assert "不得重复询问" in prompt
    assert "初中" in prompt
