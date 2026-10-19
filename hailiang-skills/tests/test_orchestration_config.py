from pathlib import Path

import yaml

from hailiang_skills.api.configuration_sync import synchronize_configuration
from hailiang_skills.core.context import SessionContext
from hailiang_skills.harness import policy
from hailiang_skills.runtime_bridge.consultation_policy import parse_consultation_policy
from hailiang_skills.runtime_bridge.expert_bundle import load_local_expert_registry
from hailiang_skills.runtime_bridge.expert_team_bundle import (
    ExpertTeamBundleError,
    load_expert_team_bundle,
    load_local_expert_team_registry,
)
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry
from hailiang_skills.workbench.domain import ConsultationPolicy
from hailiang_skills.workbench.orchestration import (
    load_seed_rules,
    preserve_orchestrator_binding,
    validate_expert_team_orchestration,
)


ROOT = Path(__file__).resolve().parents[1]


def _experts():
    return load_local_expert_registry(ROOT / "runtime_agents", load_local_skill_registry(ROOT / "runtime_skills"))


def test_student_growth_team_publishes_consultation_policy():
    team = load_local_expert_team_registry(ROOT / "runtime_agent_teams", _experts()).require("student_growth_expert_team")

    assert team.consultation_policy == ConsultationPolicy(max_subtasks=3, max_parallel=3, max_depth=1)
    assert team.fallback_expert_id == "career_plan_expert"
    assert "depends_on" in team.collaboration_rules
    assert team.member_for_expert("family_education_expert").applicable_stages == ("小学", "初中", "高中")


def test_parse_consultation_policy_migrates_allow_parallel():
    assert parse_consultation_policy(
        {"max_subtasks": 4, "allow_parallel": True, "max_depth": 1}
    ) == ConsultationPolicy(max_subtasks=4, max_parallel=4, max_depth=1)
    assert parse_consultation_policy(
        {"max_subtasks": 4, "allow_parallel": False, "max_depth": 2}
    ) == ConsultationPolicy(max_subtasks=4, max_parallel=1, max_depth=2)
    assert parse_consultation_policy(
        {"max_subtasks": 3, "max_parallel": 2, "max_depth": 1}
    ) == ConsultationPolicy(max_subtasks=3, max_parallel=2, max_depth=1)


def test_team_yaml_rejects_foreign_fallback_and_unknown_stage(tmp_path: Path):
    experts = _experts()
    source = ROOT / "runtime_agent_teams" / "student_growth_expert_team"
    target = tmp_path / "team"
    target.mkdir()
    (target / "TEAM.md").write_text((source / "TEAM.md").read_text(encoding="utf-8"), encoding="utf-8")
    raw = yaml.safe_load((source / "team.yaml").read_text(encoding="utf-8"))
    raw["fallback_expert_id"] = "missing_expert"
    (target / "team.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    try:
        load_expert_team_bundle(target, experts)
        raise AssertionError("foreign fallback should be rejected")
    except ExpertTeamBundleError as exc:
        assert "fallback_expert_id" in str(exc)

    raw["fallback_expert_id"] = "career_plan_expert"
    raw["members"][0]["applicable_stages"] = ["大学"]
    (target / "team.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    try:
        load_expert_team_bundle(target, experts)
        raise AssertionError("unknown stage should be rejected")
    except ExpertTeamBundleError as exc:
        assert "适用学段" in str(exc)


def test_workbench_validation_requires_published_fallback_member():
    locks = [
        {"object_id": "obj_career", "object_key": "career_plan_expert"},
        {"object_id": "obj_family", "object_key": "family_education_expert"},
    ]
    errors, warnings = validate_expert_team_orchestration(
        {
            "consultation_policy": {"max_subtasks": 3, "max_parallel": 1, "max_depth": 1},
            "fallback_expert_id": "obj_career",
            "collaboration_rules": "无依赖可并行",
            "members": [{"expert_id": "career_plan_expert", "applicable_stages": ["高中"]}],
        },
        locks,
    )
    assert errors == []
    assert warnings == []

    errors, _warnings = validate_expert_team_orchestration(
        {"fallback_expert_id": "obj_other", "consultation_policy": {"max_subtasks": 9, "max_parallel": 1, "max_depth": 1}},
        locks,
    )
    assert any("兜底专家" in item for item in errors)
    assert any("max_subtasks" in item for item in errors)


def test_existing_session_keeps_its_orchestration_binding_when_deployment_changes():
    previous = {
        "deployment_id": "old",
        "package_hash": "old-hash",
        "orchestration_rules": {"head_rules": "旧编排首", "recommendations": [{"recommendation_id": "old"}]},
        "entries": [{
            "object_type": "expert_team",
            "object_key": "student_growth_expert_team",
            "payload": {
                "consultation_policy": {"max_subtasks": 2, "max_parallel": 1, "max_depth": 1},
                "fallback_expert_id": "career_plan_expert",
                "collaboration_rules": "旧协作",
                "members": [{"expert_id": "career_plan_expert", "applicable_stages": ["高中"]}],
            },
        }],
    }
    nxt = {
        "deployment_id": "new",
        "package_hash": "new-hash",
        "orchestration_rules": {"head_rules": "新编排首", "recommendations": [{"recommendation_id": "new"}]},
        "entries": [{
            "object_type": "expert_team",
            "object_key": "student_growth_expert_team",
            "payload": {
                "consultation_policy": {"max_subtasks": 5, "max_parallel": 5, "max_depth": 2},
                "fallback_expert_id": "family_education_expert",
                "collaboration_rules": "新协作",
                "members": [{"expert_id": "career_plan_expert", "routing_brief": "新职责", "applicable_stages": ["小学"]}],
            },
        }],
    }
    context = SessionContext(session_id="s", user_id="u", session_meta={"configuration_snapshot": previous, "expert_team_id": "student_growth_expert_team"})
    synchronize_configuration(context, nxt)
    frozen = context.session_meta["configuration_snapshot"]
    assert frozen["orchestration_rules"]["head_rules"] == "旧编排首"
    payload = frozen["entries"][0]["payload"]
    assert payload["consultation_policy"]["max_subtasks"] == 2
    assert payload["consultation_policy"]["max_parallel"] == 1
    assert payload["fallback_expert_id"] == "career_plan_expert"
    assert payload["collaboration_rules"] == "旧协作"
    assert payload["members"][0]["applicable_stages"] == ["高中"]
    assert payload["members"][0]["routing_brief"] == "新职责"


def test_new_session_snapshot_can_see_published_rules_without_changing_old_sessions(monkeypatch):
    from hailiang_skills.workbench.catalog import build_runtime_registries

    team_entry = {
        "object_type": "expert_team",
        "object_key": "student_growth_expert_team",
        "name": "学生成长专家团",
        "payload": {
            "rules_markdown": "规则",
            "coordinator_expert_id": "career_plan_expert",
            "consultation_policy": {"max_subtasks": 2, "max_parallel": 1, "max_depth": 1},
            "fallback_expert_id": "career_plan_expert",
            "collaboration_rules": "无依赖并行",
            "members": [{
                "expert_id": "career_plan_expert",
                "mention_name": "升学规划专家",
                "routing_brief": "升学",
                "applicable_stages": ["高中"],
            }],
        },
        "dependency_locks": [{"object_id": "career_plan_expert", "object_key": "career_plan_expert"}],
    }
    _skills, _experts, teams = build_runtime_registries([team_entry])
    monkeypatch.setattr(policy, "load_settings", lambda: {"independent_skill_ids": []})
    orchestrator = type("Orchestrator", (), {})()
    orchestrator.runtime_registry = _skills
    orchestrator.expert_registry = type("Experts", (), {"definitions": {}, "get": lambda *_: None})()
    orchestrator.expert_team_registry = teams
    orchestrator._configured_bundle = lambda _context, bundle: bundle
    orchestrator._soul_context = lambda _context: {"content": ""}
    orchestrator._runtime_client_for_context = lambda _context: None

    fresh = SessionContext(session_id="new", user_id="u", session_meta={
        "orchestrator_v2": True,
        "expert_team_id": "student_growth_expert_team",
        "configuration_snapshot": {
            "entries": [team_entry],
            "orchestration_rules": {
                "head_rules": "编排首",
                "tail_rules": "编排尾",
                "recommendation_rules": "推荐",
                "recommendations": [{"id": "assessment.generic", "intent": "assessment", "object_type": "generic", "title": "通用测评"}],
                "recommend_catalog": {
                    "schema_version": 1,
                    "items": [{"id": "assessment.generic", "intent": "assessment", "object_type": "generic", "title": "通用测评"}],
                },
                "system_skills": {
                    "orchestrate_head": {"purpose": "orchestrate_head", "body": "编排首正文", "skill_id": "orchestrate_head_default"},
                    "recommend": {
                        "purpose": "recommend",
                        "body": "推荐正文",
                        "catalog": {"schema_version": 1, "items": [{"id": "assessment.generic", "intent": "assessment", "object_type": "generic"}]},
                    },
                },
            },
        },
    })
    captured = policy.capture_policy(orchestrator, fresh)
    assert captured["orchestrator"]["enabled"] is True
    assert captured["orchestrator"]["consultation_policy"]["max_subtasks"] == 2
    assert captured["orchestrator"]["consultation_policy"]["max_parallel"] == 1
    assert captured["orchestrator"]["fallback_expert_id"] == "career_plan_expert"
    assert captured["orchestrator"]["collaboration_rules"] == "无依赖并行"
    assert captured["orchestrator"]["members"][0]["applicable_stages"] == ["高中"]
    assert captured["orchestrator"]["head_rules"] == "编排首"
    assert captured["orchestrator"]["system_skills"]["orchestrate_head"]["body"] == "编排首正文"
    assert captured["orchestrator"]["recommend_catalog"]["items"][0]["object_type"] == "generic"

    old = SessionContext(session_id="old", user_id="u", session_meta={
        "expert_team_id": "student_growth_expert_team",
        "configuration_snapshot": {"entries": [{**team_entry, "payload": {**team_entry["payload"], "consultation_policy": {"max_subtasks": 3, "allow_parallel": False, "max_depth": 1}, "fallback_expert_id": "career_plan_expert"}}]},
    })
    # An old session has no v2 flag and no orchestration documents.
    old.session_meta["configuration_snapshot"]["entries"][0]["payload"].pop("consultation_policy")
    old.session_meta["configuration_snapshot"]["entries"][0]["payload"].pop("fallback_expert_id")
    old_view = policy.capture_policy(orchestrator, old)["orchestrator"]
    assert old_view["enabled"] is False
    assert old_view["head_rules"] == ""
    assert old_view["fallback_expert_id"] == ""


def test_preserve_does_not_grant_rules_to_a_session_that_never_had_them():
    preserved = preserve_orchestrator_binding(
        {"entries": []},
        {"orchestration_rules": {"head_rules": "新规则"}, "entries": []},
    )
    assert "orchestration_rules" not in preserved


def test_seed_rules_include_system_skill_bodies_and_catalog():
    seed = load_seed_rules()
    assert "职责" in seed["head_rules"]
    assert "jump_cards" in seed["recommendation_rules"]
    assert seed["recommend_catalog"]["items"]
    assert "orchestrate_head" in seed["system_skills"]
    assert seed["system_skills"]["recommend"]["catalog"]["items"]
