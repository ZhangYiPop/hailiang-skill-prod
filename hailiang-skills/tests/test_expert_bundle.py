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
from hailiang_skills.core.skill_display import build_skill_display
from hailiang_skills.core.skill_ids import EXPERT_DIRECT_EXECUTION_ID
from hailiang_skills.runtime_bridge.agentscope_expert_runtime import AgentScopeExpertRuntime
from hailiang_skills.runtime_bridge.expert_bundle import ExpertBundleError, load_expert_bundle, load_local_expert_registry
from hailiang_skills.runtime_bridge.expert_team_bundle import (
    ExpertTeamBundleError,
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

    def capture_agent(_definition, _message, _context, _client, _state, **kwargs):
        instructions.append(str(kwargs.get("routing_instruction") or ""))

    runtime._run_agent = capture_agent
    runtime.handle_message("我更想解决亲子冲突", context, lambda _message, _context: "legacy-result")

    assert instructions
    assert "mbti_self_exploration" in instructions[0]
    assert "另一个授权 Skill" in instructions[0]
    assert "自动切换后由目标 Skill 作答" in instructions[0]


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
