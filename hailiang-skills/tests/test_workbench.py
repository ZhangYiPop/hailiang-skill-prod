from __future__ import annotations

import io
import hashlib
import base64
import json
import threading
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from hailiang_skills.storage.database import Base, WorkbenchDebugSessionRow
from hailiang_skills.workbench.service import (
    WorkbenchConflict,
    WorkbenchError,
    WorkbenchService,
    _hash,
    _script_execution_succeeded,
)
from hailiang_skills.workbench.runtime_overlay import (
    configured_skill_bundle,
    skill_bundle_from_entry,
    skill_markdown_with_metadata,
)
from hailiang_skills.api.routes import workbench as workbench_routes
from hailiang_skills.api.routes.workbench import build_deployment_router, build_workbench_router
from hailiang_skills.core.context import SessionContext
from hailiang_skills.runtime_bridge.main_planner import MainPlannerOrchestrator
from hailiang_skills.skill_runtime.models import ChatMessage, SessionState, ToolCapability, ToolRegistry
from hailiang_skills.skill_runtime.tools import run_local_rag


@pytest.fixture()
def service() -> WorkbenchService:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return WorkbenchService(sessionmaker(bind=engine, expire_on_commit=False))


def _actor(service: WorkbenchService) -> str:
    return service.register_actor("业务测试员")["actor_id"]


def test_database_bundle_needs_no_business_template(tmp_path, monkeypatch):
    from hailiang_skills.workbench import runtime_overlay
    monkeypatch.setattr(runtime_overlay, "state_root", lambda: tmp_path)
    entry = {
        "object_key": "database_only_skill",
        "payload": {
            "prompt_markdown": "根据表单回答用户。",
            "source_metadata": {"name": "数据库表单"},
            "runtime_contract": {"questionnaire": {"enabled": True, "fields": []}},
        },
        "files": [
            _text_asset("references/guide.md", "规则资料", "text/markdown"),
            _text_asset("scripts/score.py", "def main(payload):\n    return payload\n", "text/x-python"),
        ],
    }
    bundle = skill_bundle_from_entry(entry)
    assert bundle.contract.skill_id == "database_only_skill"
    assert bundle.metadata["questionnaire"]["enabled"] is True
    assert bundle.references["references/guide.md"] == "规则资料"
    assert bundle.scripts["scripts/score.py"].is_file()


def test_workbench_asset_is_retrievable_by_skill_local_rag(tmp_path, monkeypatch):
    from hailiang_skills.workbench import runtime_overlay

    monkeypatch.setattr(runtime_overlay, "state_root", lambda: tmp_path)
    entry = {
        "object_key": "asset_retrieval_skill",
        "payload": {
            "prompt_markdown": "优先使用本地 asset 回答。",
            "source_metadata": {"name": "本地资产检索测试"},
            "runtime_contract": {},
        },
        "files": [
            _text_asset(
                "assets/policy.json",
                '{"workbench_asset_token":"只允许周一至周五预约"}',
                "application/json",
            ),
        ],
    }

    bundle = skill_bundle_from_entry(entry)
    bundle.tool_registry = ToolRegistry(
        capabilities=(ToolCapability(name="rag", description="本地资料检索", enabled=True),),
    )
    state = SessionState(
        session_id="sess_workbench_asset_rag",
        active_skill_id="asset_retrieval_skill",
        messages=[ChatMessage(role="user", content="workbench_asset_token 的预约规则是什么？")],
    )

    results = run_local_rag(bundle, state)

    assert bundle.runtime_metadata.assets.local_enabled is True
    assert bundle.local_assets["assets/policy.json"].startswith('{"workbench_asset_token"')
    assert any(
        result.source == "local_asset:assets/policy.json"
        and "只允许周一至周五预约" in result.snippet
        for result in results
    )


def test_candidate_single_skill_expert_requires_agent_authorization(monkeypatch, service):
    snapshot = {
        "root": {"object_id": "expert-1"},
        "entries": [
            {
                "object_id": "expert-1", "object_type": "expert", "object_key": "academic_coach",
                "dependency_locks": [{"object_type": "skill", "object_key": "score_improve"}],
            },
            {"object_id": "skill-1", "object_type": "skill", "object_key": "score_improve"},
        ],
    }
    context = SessionContext()
    service._configure_snapshot_context(context, snapshot)
    called: list[str] = []
    monkeypatch.setattr(
        service,
        "_execute_candidate_skill",
        lambda entry, message, _context: called.append(str(entry["object_key"])) or "由提分技能回答",
    )
    agent_calls: list[str] = []

    def handle_message(message, received_context):
        agent_calls.append(message)
        received_context.event_trace.append({
            "event_type": "expert_skill_executed",
            "payload": {"expert_id": "academic_coach", "skill_id": "score_improve"},
        })
        return SimpleNamespace(assistant_message="专家已选择提分技能")

    service.orchestrator = SimpleNamespace(handle_message=handle_message)

    result = service._execute_snapshot_message(snapshot, "我想提分", context)

    assert result == "由提分技能回答"
    assert called == ["score_improve"]
    assert agent_calls == ["我想提分"]
    assert context.interaction_state["active_skill"] == "score_improve"
    assert context.event_trace[-1]["event_type"] == "expert_skill_executed"


def test_candidate_context_uses_snapshot_names_for_unpublished_expert_and_team(service):
    context = SessionContext()
    context.session_meta.update({
        "expert_team_id": "study_abroad_team",
        "expert_id": "study_abroad_consultant",
        "active_expert_id": "study_abroad_consultant",
        "configuration_snapshot": {
            "entries": [
                {
                    "object_type": "expert_team",
                    "object_key": "study_abroad_team",
                    "name": "留学规划专家团",
                    "payload": {"coordinator_expert_id": "study_abroad_consultant"},
                },
                {
                    "object_type": "expert",
                    "object_key": "study_abroad_consultant",
                    "name": "留学咨询师",
                },
            ],
        },
    })
    service.orchestrator = SimpleNamespace(
        expert_registry=SimpleNamespace(get=lambda _expert_id: None),
        expert_team_registry=SimpleNamespace(get=lambda _team_id: None),
        runtime_registry=SimpleNamespace(get=lambda _skill_id: None),
    )

    expert, expert_context = service._candidate_expert_state(context)

    assert expert_context["expert_team_id"] == "study_abroad_team"
    assert expert["team"]["name"] == "留学规划专家团"
    assert expert["active"]["name"] == "留学咨询师"


def test_filesystem_migration_captures_root_data_and_requires_explicit_current(tmp_path, monkeypatch):
    from hailiang_skills.workbench import runtime_overlay
    monkeypatch.setattr(runtime_overlay, "state_root", lambda: tmp_path / "state")
    bundle = skill_bundle_from_entry({
        "object_key": "catalog_skill",
        "payload": {
            "prompt_markdown": "数据库迁移测试",
            "source_metadata": {"name": "目录技能"},
            "runtime_contract": {},
        },
        "files": [_text_asset("professions.json", '[{"name":"工程师"}]', "application/json")],
    })
    orchestrator = SimpleNamespace(
        runtime_registry=SimpleNamespace(bundles={"catalog_skill": bundle}),
        expert_registry=SimpleNamespace(definitions={}),
        expert_team_registry=SimpleNamespace(definitions={}),
    )
    migrated = _blank_service()
    migrated.orchestrator = orchestrator

    preview = migrated.preview_runtime_migration()
    assert preview["objects"][0]["files"] == ["professions.json"]
    first = migrated.migrate_runtime_catalog(actor_id="migration", make_current=False)
    assert first["created_objects"] == 1
    assert migrated.list_objects()[0]["current_release_id"] is None

    second = migrated.migrate_runtime_catalog(actor_id="migration", make_current=True)
    assert second["reused_objects"] == 1
    assert second["reused_releases"] == 1
    assert migrated.list_objects()[0]["current_release_id"] is not None


def test_filesystem_migration_can_be_limited_to_skills():
    migrated = _blank_service()
    migrated.orchestrator = SimpleNamespace(runtime_registry=SimpleNamespace(bundles={}))
    entries = [
        {
            "object_type": "skill", "object_key": "only_skill", "name": "仅技能",
            "release_id": "filesystem:skill:only_skill", "release_no": 1,
            "payload": {"prompt_markdown": "技能", "runtime_contract": {}},
            "dependency_locks": [], "files": [], "content_hash": "source-skill",
        },
        {
            "object_type": "expert", "object_key": "file_expert", "name": "文件专家",
            "release_id": "filesystem:expert:file_expert", "release_no": 1,
            "payload": {"rules_markdown": "专家", "budget": {}},
            "dependency_locks": [], "files": [], "content_hash": "source-expert",
        },
    ]
    migrated._runtime_migration_entries = lambda: entries

    preview = migrated.preview_runtime_migration(skills_only=True)
    assert preview["scope"] == "runtime_skills"
    assert [item["object_key"] for item in preview["objects"]] == ["only_skill"]

    result = migrated.migrate_runtime_catalog(actor_id="migration", make_current=True, skills_only=True)
    assert result["scope"] == "runtime_skills"
    assert [(item["object_type"], item["object_key"]) for item in migrated.list_objects()] == [
        ("skill", "only_skill")
    ]


def test_filesystem_migration_can_reuse_a_permanently_deleted_id():
    migrated = _blank_service()
    actor_id = _actor(migrated)
    original = migrated.create_object(
        object_type="skill", object_key="deleted_file_skill", name="旧技能", actor_id=actor_id,
    )
    migrated.delete_object(original["object_id"], confirmation_name="旧技能", actor_id=actor_id)
    migrated.orchestrator = SimpleNamespace(runtime_registry=SimpleNamespace(bundles={}))
    migrated._runtime_migration_entries = lambda: [{
        "object_type": "skill", "object_key": "deleted_file_skill", "name": "目录技能",
        "release_id": "filesystem:skill:deleted_file_skill", "release_no": 1,
        "payload": {"prompt_markdown": "技能", "runtime_contract": {}},
        "dependency_locks": [], "files": [], "content_hash": "source-skill",
    }]

    result = migrated.migrate_runtime_catalog(actor_id=actor_id, skills_only=True, make_current=True)
    assert result["created_objects"] == 1


def _text_asset(path: str, content: str, media_type: str) -> dict[str, str]:
    return {
        "relative_path": path,
        "media_type": media_type,
        "content_base64": base64.b64encode(content.encode()).decode(),
    }


class _PreviewClient:
    def complete(self, messages):
        return f"{messages[0].content} :: {messages[-1].content}"


class _PreviewOrchestrator:
    def _runtime_client_for_context(self, _context):
        return _PreviewClient()

    def handle_message(self, message, context):
        snapshot = context.session_meta["configuration_snapshot"]
        root = snapshot["root"]
        entry = next(
            item for item in snapshot["entries"] if item["object_id"] == root["object_id"]
        )
        return SimpleNamespace(
            assistant_message=f"{entry['object_type']}:{entry['object_key']} :: {message}",
        )


class _RedispatchPreviewOrchestrator(_PreviewOrchestrator):
    def handle_message(self, message, context):
        selected = "family_skill" if ("另一个技能" in message or "亲子沟通" in message) else "disc_skill"
        context.event_trace.append({
            "event_type": "expert_skill_executed",
            "payload": {"expert_id": "switching_expert", "skill_id": selected},
        })
        return SimpleNamespace(assistant_message=f"专家选择 {selected}")


class _TeamHandoffPreviewOrchestrator(_PreviewOrchestrator):
    def handle_message(self, message, context):
        context.add_message("user", message)
        context.add_message("assistant", "这个问题更适合由家庭教育专家继续处理。")
        assistant = context.messages[-1]
        assistant["team_handoff"] = {
            "handoff_id": "handoff_candidate_1",
            "status": "active",
            "team_id": str(context.session_meta["expert_team_id"]),
            "reason": "需要家庭教育专项建议",
            "candidates": [{"expert_id": "family_expert", "name": "家庭教育专家", "mention_name": "家庭教育专家"}],
        }
        assistant["metadata"]["team_handoff"] = assistant["team_handoff"]
        return SimpleNamespace(assistant_message=assistant["content"])


def _preview_service() -> WorkbenchService:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return WorkbenchService(
        sessionmaker(bind=engine, expire_on_commit=False),
        orchestrator=_PreviewOrchestrator(),
    )


def _blank_service() -> WorkbenchService:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return WorkbenchService(sessionmaker(bind=engine, expire_on_commit=False))


def _publish_skill(service: WorkbenchService, actor_id: str, *, key: str = "skill_a", prompt: str = "处理业务问题"):
    obj = service.create_object(object_type="skill", object_key=key, name=key, actor_id=actor_id)
    revision = service.save_revision(
        obj["object_id"],
        base_revision_id=None,
        payload={"prompt_markdown": prompt, "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[],
        assets=[],
        actor_id=actor_id,
    )
    debug = service.create_debug_session(revision["revision_id"], baseline_release_id=None, actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    release = service.publish_revision(
        revision["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True,
        confirmation_notes="人工确认", actor_id=actor_id,
    )
    return obj, revision, release


def test_revision_and_release_include_business_actor_display_name(service: WorkbenchService):
    creator_id = service.register_actor("初始创建人")["actor_id"]
    editor_id = service.register_actor("业务修改人")["actor_id"]
    obj = service.create_object(
        object_type="skill", object_key="actor_trace_skill", name="人员追溯 Skill", actor_id=creator_id,
    )
    revision = service.save_revision(
        obj["object_id"], base_revision_id=None,
        payload={"prompt_markdown": "用于验证版本变更人员", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[], assets=[], actor_id=editor_id,
    )
    assert revision["created_by_display_name"] == "业务修改人"

    debug = service.create_debug_session(revision["revision_id"], baseline_release_id=None, actor_id=editor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=editor_id)
    release = service.publish_revision(
        revision["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True,
        confirmation_notes="人工确认", actor_id=editor_id,
    )
    assert release["published_by_display_name"] == "业务修改人"

    detail = service.get_object(obj["object_id"])
    assert detail["revisions"][0]["created_by"] == editor_id
    assert detail["revisions"][0]["created_by_display_name"] == "业务修改人"
    assert detail["releases"][0]["published_by"] == editor_id
    assert detail["releases"][0]["published_by_display_name"] == "业务修改人"
    assert service.list_releases()[0]["published_by_display_name"] == "业务修改人"


def _publish_expert(
    service: WorkbenchService,
    actor_id: str,
    *,
    key: str,
    skill_release: dict[str, str],
    rules: str = "专家规则",
):
    expert = service.create_object(object_type="expert", object_key=key, name=key, actor_id=actor_id)
    revision = service.save_revision(
        expert["object_id"],
        base_revision_id=None,
        payload={
            "rules_markdown": rules,
            "brief": f"{key} 的专家服务摘要",
            "budget": {"max_iters": 4, "max_skill_calls": 3},
        },
        dependency_locks=[{"release_id": skill_release["release_id"]}],
        assets=[],
        actor_id=actor_id,
    )
    debug = service.create_debug_session(revision["revision_id"], baseline_release_id=None, actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    release = service.publish_revision(
        revision["revision_id"],
        evidence_id=debug["debug_session_id"],
        manual_confirmation=True,
        confirmation_notes="人工确认",
        actor_id=actor_id,
    )
    return expert, revision, release


def _publish_team(
    service: WorkbenchService,
    actor_id: str,
    *,
    key: str,
    expert_releases: list[dict[str, str]],
    rules: str = "团队规则",
):
    team = service.create_object(object_type="expert_team", object_key=key, name=key, actor_id=actor_id)
    revision = service.save_revision(
        team["object_id"],
        base_revision_id=None,
        payload={
            "rules_markdown": rules,
            "brief": f"{key} 的专家团服务摘要",
            "coordinator_expert_id": expert_releases[0]["object_id"],
            "members": [
                {"expert_id": item["object_key"], "mention_name": item["object_key"], "routing_brief": ""}
                for item in expert_releases
            ],
        },
        dependency_locks=[{"release_id": item["release_id"]} for item in expert_releases],
        assets=[],
        actor_id=actor_id,
    )
    debug = service.create_debug_session(revision["revision_id"], baseline_release_id=None, actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    release = service.publish_revision(
        revision["revision_id"],
        evidence_id=debug["debug_session_id"],
        manual_confirmation=True,
        confirmation_notes="人工确认",
        actor_id=actor_id,
    )
    return team, revision, release


def test_expert_brief_is_required_normalized_and_versioned(service: WorkbenchService):
    actor_id = _actor(service)
    _, _, skill_release = _publish_skill(service, actor_id, key="brief_skill")
    expert = service.create_object(
        object_type="expert", object_key="brief_expert", name="Brief 专家", actor_id=actor_id,
    )
    missing = service.save_revision(
        expert["object_id"],
        base_revision_id=None,
        payload={"rules_markdown": "专家规则", "budget": {"max_iters": 4, "max_skill_calls": 3}},
        dependency_locks=[{"release_id": skill_release["release_id"]}], assets=[], actor_id=actor_id,
    )
    assert missing["validation"]["valid"] is False
    assert "专家 Brief 不能为空" in missing["validation"]["errors"]

    saved = service.save_revision(
        expert["object_id"],
        base_revision_id=missing["revision_id"],
        payload={
            "rules_markdown": "专家规则",
            "brief": "  学习规划\n  与方法支持  ",
            "budget": {"max_iters": 4, "max_skill_calls": 3},
        },
        dependency_locks=[{"release_id": skill_release["release_id"]}], assets=[], actor_id=actor_id,
    )
    assert saved["validation"]["valid"] is True
    assert saved["payload"]["brief"] == "学习规划 与方法支持"
    assert service.get_object(expert["object_id"])["brief"] == "学习规划 与方法支持"


def test_immutable_revision_conflict_and_release_sequence(service: WorkbenchService):
    actor_id = _actor(service)
    obj, first, first_release = _publish_skill(service, actor_id)

    with pytest.raises(WorkbenchConflict, match="最新版本"):
        service.save_revision(
            obj["object_id"], base_revision_id=None,
            payload={"prompt_markdown": "过期编辑", "runtime_contract": {}}, dependency_locks=[], assets=[], actor_id=actor_id,
        )

    second = service.save_revision(
        obj["object_id"], base_revision_id=first["revision_id"],
        payload={"prompt_markdown": "第二版", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[], assets=[], actor_id=actor_id,
    )
    debug = service.create_debug_session(second["revision_id"], baseline_release_id=first_release["release_id"], actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    second_release = service.publish_revision(
        second["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True,
        confirmation_notes="通过", actor_id=actor_id,
    )

    assert first["revision_no"] == 1
    assert second["revision_no"] == 2
    assert first_release["version"] == "v1"
    assert second_release["version"] == "v2"
    reverted = service.select_current_release(
        first_release["release_id"], expected_current_release_id=second_release["release_id"], actor_id=actor_id,
    )
    assert reverted["is_current"] is True
    assert service.get_object(obj["object_id"])["current_release_id"] == first_release["release_id"]
    with pytest.raises(WorkbenchConflict):
        service.select_current_release(second_release["release_id"], expected_current_release_id=second_release["release_id"], actor_id=actor_id)
    draft = service.draft_from_release(first_release["release_id"], actor_id=actor_id)
    assert draft["payload"] == first["payload"]
    assert draft["revision_no"] == 3
    assert len(service.get_object(obj["object_id"])["releases"]) == 2


def test_expert_and_team_lock_exact_published_dependencies(service: WorkbenchService):
    actor_id = _actor(service)
    _, _, skill_a = _publish_skill(service, actor_id, key="skill_a")
    _, _, skill_b = _publish_skill(service, actor_id, key="skill_b")

    expert_releases = []
    for key, skill_release in (("expert_a", skill_a), ("expert_b", skill_b)):
        expert = service.create_object(object_type="expert", object_key=key, name=key, actor_id=actor_id)
        revision = service.save_revision(
            expert["object_id"], base_revision_id=None,
            payload={"rules_markdown": "专家规则", "brief": f"{key} 的摘要", "budget": {"max_iters": 4, "max_skill_calls": 3}},
            dependency_locks=[{"release_id": skill_release["release_id"]}], assets=[], actor_id=actor_id,
        )
        debug = service.create_debug_session(revision["revision_id"], baseline_release_id=None, actor_id=actor_id)
        service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
        expert_releases.append(service.publish_revision(
            revision["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True,
            confirmation_notes="通过", actor_id=actor_id,
        ))

    team = service.create_object(object_type="expert_team", object_key="team_a", name="专家团", actor_id=actor_id)
    team_revision = service.save_revision(
        team["object_id"], base_revision_id=None,
        payload={"rules_markdown": "团队规则", "brief": "专家团服务摘要", "coordinator_expert_id": expert_releases[0]["object_id"]},
        dependency_locks=[{"release_id": item["release_id"]} for item in expert_releases], assets=[], actor_id=actor_id,
    )

    assert team_revision["validation"]["valid"] is True
    assert [item["release_id"] for item in team_revision["dependency_locks"]] == [item["release_id"] for item in expert_releases]
    snapshot = service.create_debug_session(team_revision["revision_id"], baseline_release_id=None, actor_id=actor_id)["snapshot"]
    assert {entry["object_type"] for entry in snapshot["entries"]} == {"skill", "expert", "expert_team"}


def test_dependency_locks_reject_multiple_versions_of_the_same_object(service: WorkbenchService):
    actor_id = _actor(service)
    skill, first_revision, first_release = _publish_skill(service, actor_id, key="versioned_skill")
    second_revision = service.save_revision(
        skill["object_id"],
        base_revision_id=first_revision["revision_id"],
        payload={"prompt_markdown": "第二版规则", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[],
        assets=[],
        actor_id=actor_id,
    )
    debug = service.create_debug_session(second_revision["revision_id"], baseline_release_id=first_release["release_id"], actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    second_release = service.publish_revision(
        second_revision["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True,
        confirmation_notes="通过", actor_id=actor_id,
    )
    expert = service.create_object(object_type="expert", object_key="single_version_expert", name="单版本专家", actor_id=actor_id)

    with pytest.raises(WorkbenchError) as error:
        service.save_revision(
            expert["object_id"],
            base_revision_id=None,
            payload={"rules_markdown": "专家规则", "brief": "验证同对象版本锁定", "budget": {}},
            dependency_locks=[{"release_id": first_release["release_id"]}, {"release_id": second_release["release_id"]}],
            assets=[],
            actor_id=actor_id,
        )

    assert error.value.code == "DEPENDENCY_OBJECT_VERSION_CONFLICT"


def test_team_rejects_missing_members_and_invalid_coordinator(service: WorkbenchService):
    actor_id = _actor(service)
    team = service.create_object(object_type="expert_team", object_key="bad_team", name="错误团队", actor_id=actor_id)
    revision = service.save_revision(
        team["object_id"], base_revision_id=None,
        payload={"rules_markdown": "团队规则", "coordinator_expert_id": "missing"},
        dependency_locks=[], assets=[], actor_id=actor_id,
    )
    assert revision["validation"]["valid"] is False
    assert any("至少两个" in item for item in revision["validation"]["errors"])


def test_object_permanent_delete_removes_local_history_and_reserves_deleted_key(service: WorkbenchService):
    actor_id = _actor(service)
    obj = service.create_object(object_type="skill", object_key="delete_me", name="待删除能力", actor_id=actor_id)
    revision = service.save_revision(
        obj["object_id"], base_revision_id=None,
        payload={"prompt_markdown": "临时 Prompt", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[],
        assets=[_text_asset("references/temp.md", "临时资料", "text/markdown")],
        actor_id=actor_id,
    )
    debug = service.create_debug_session(revision["revision_id"], baseline_release_id=None, actor_id=actor_id)
    suite = service.create_evaluation_suite(
        object_id=obj["object_id"], name="删除测试", cases=[{"case_id": "case_1", "input": "测试"}], actor_id=actor_id,
    )
    service.create_evaluation_run(
        suite_id=suite["suite_id"], revision_id=revision["revision_id"], baseline_release_id=None,
        results=[{"case_id": "case_1", "status": "completed"}], actor_id=actor_id,
    )

    with pytest.raises(WorkbenchError, match="确认名称"):
        service.delete_object(obj["object_id"], confirmation_name="错误名称", actor_id=actor_id)

    deleted = service.delete_object(obj["object_id"], confirmation_name="待删除能力", actor_id=actor_id)

    assert deleted == {
        "object_id": obj["object_id"],
        "object_type": "skill",
        "object_key": "delete_me",
        "name": "待删除能力",
        "revision_count": 1,
        "release_count": 0,
        "asset_count": 1,
        "reference_policy": "latest_unarchived_release_only",
        "permanent": True,
    }
    assert service.list_objects(include_archived=True) == []
    assert any(event["action"] == "object.deleted" for event in service.list_audit_events())
    recreated = service.create_object(object_type="skill", object_key="delete_me", name="重新创建", actor_id=actor_id)
    assert recreated["object_id"] != obj["object_id"]


def test_deleted_id_can_be_rebuilt_normally(service: WorkbenchService):
    actor_id = _actor(service)
    original = service.create_object(
        object_type="expert_team", object_key="restored_team", name="恢复团队", actor_id=actor_id,
    )
    service.delete_object(original["object_id"], confirmation_name="恢复团队", actor_id=actor_id)

    restored = service.create_object(
        object_type="expert_team", object_key="restored_team", name="恢复团队", description="新对象", actor_id=actor_id,
    )

    assert restored["object_key"] == "restored_team"
    assert restored["object_id"] != original["object_id"]


def test_archived_object_can_be_listed_and_restored(service: WorkbenchService):
    actor_id = _actor(service)
    obj = service.create_object(
        object_type="expert", object_key="archive_me", name="待归档专家", actor_id=actor_id,
    )

    service.archive_object(obj["object_id"], actor_id=actor_id)
    assert service.list_objects() == []
    assert service.list_objects(include_archived=True)[0]["archived"] is True

    service.unarchive_object(obj["object_id"], actor_id=actor_id)
    assert service.list_objects()[0]["archived"] is False
    assert any(event["action"] == "object.unarchived" for event in service.list_audit_events())


def test_object_delete_is_blocked_when_an_upper_revision_depends_on_it(service: WorkbenchService):
    actor_id = _actor(service)
    skill_obj, _revision, skill_release = _publish_skill(service, actor_id, key="depended_skill")
    expert = service.create_object(object_type="expert", object_key="dependent_expert", name="依赖专家", actor_id=actor_id)
    expert_revision = service.save_revision(
        expert["object_id"], base_revision_id=None,
        payload={"rules_markdown": "使用依赖能力", "brief": "依赖能力专家", "budget": {}},
        dependency_locks=[{"release_id": skill_release["release_id"]}], assets=[], actor_id=actor_id,
    )
    debug = service.create_debug_session(expert_revision["revision_id"], baseline_release_id=None, actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    expert_release = service.publish_revision(
        expert_revision["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True,
        confirmation_notes="通过", actor_id=actor_id,
    )

    with pytest.raises(WorkbenchConflict) as exc_info:
        service.delete_object(skill_obj["object_id"], confirmation_name=skill_obj["name"], actor_id=actor_id)

    assert exc_info.value.code == "OBJECT_DELETE_BLOCKED"
    assert exc_info.value.details["blockers"][0]["object_key"] == "dependent_expert"
    assert exc_info.value.details["blockers"][0]["release_id"] == expert_release["release_id"]
    assert service.get_object(skill_obj["object_id"])["name"] == skill_obj["name"]


def test_delete_ignores_superseded_releases_and_all_drafts(service: WorkbenchService):
    actor_id = _actor(service)
    skill_a, _, skill_a_release = _publish_skill(service, actor_id, key="delete_old_skill")
    _, _, skill_b_release = _publish_skill(service, actor_id, key="keep_current_skill")
    expert = service.create_object(object_type="expert", object_key="delete_history_expert", name="历史专家", actor_id=actor_id)
    first = service.save_revision(
        expert["object_id"], base_revision_id=None, payload={"rules_markdown": "r1", "brief": "历史技能引用专家", "budget": {}},
        dependency_locks=[{"release_id": skill_a_release["release_id"]}], assets=[], actor_id=actor_id,
    )
    debug = service.create_debug_session(first["revision_id"], baseline_release_id=None, actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    first_release = service.publish_revision(first["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True, confirmation_notes="通过", actor_id=actor_id)
    second = service.save_revision(
        expert["object_id"], base_revision_id=first["revision_id"], payload={"rules_markdown": "r2", "brief": "历史技能引用专家", "budget": {}},
        dependency_locks=[{"release_id": skill_b_release["release_id"]}], assets=[], actor_id=actor_id,
    )
    debug = service.create_debug_session(second["revision_id"], baseline_release_id=first_release["release_id"], actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    service.publish_revision(second["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True, confirmation_notes="通过", actor_id=actor_id)
    # A later draft can still mention the old Skill; only v2 is live for deletion.
    service.save_revision(
        expert["object_id"], base_revision_id=second["revision_id"], payload={"rules_markdown": "r3 草稿", "brief": "历史技能引用专家", "budget": {}},
        dependency_locks=[{"release_id": skill_a_release["release_id"]}], assets=[], actor_id=actor_id,
    )

    deleted = service.delete_object(skill_a["object_id"], confirmation_name=skill_a["name"], actor_id=actor_id)
    assert deleted["object_key"] == "delete_old_skill"


def test_delete_is_blocked_by_latest_release_even_when_latest_draft_removed_reference(service: WorkbenchService):
    actor_id = _actor(service)
    skill_a, _, skill_a_release = _publish_skill(service, actor_id, key="latest_release_skill")
    _, _, skill_b_release = _publish_skill(service, actor_id, key="draft_only_skill")
    expert = service.create_object(object_type="expert", object_key="published_reference_expert", name="当前发布专家", actor_id=actor_id)
    published = service.save_revision(
        expert["object_id"], base_revision_id=None, payload={"rules_markdown": "发布版本", "brief": "当前发布引用专家", "budget": {}},
        dependency_locks=[{"release_id": skill_a_release["release_id"]}], assets=[], actor_id=actor_id,
    )
    debug = service.create_debug_session(published["revision_id"], baseline_release_id=None, actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    service.publish_revision(published["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True, confirmation_notes="通过", actor_id=actor_id)
    service.save_revision(
        expert["object_id"], base_revision_id=published["revision_id"], payload={"rules_markdown": "未发布草稿", "brief": "当前发布引用专家", "budget": {}},
        dependency_locks=[{"release_id": skill_b_release["release_id"]}], assets=[], actor_id=actor_id,
    )

    with pytest.raises(WorkbenchConflict) as exc_info:
        service.delete_object(skill_a["object_id"], confirmation_name=skill_a["name"], actor_id=actor_id)
    assert exc_info.value.details["blockers"][0]["release_no"] == 1


def test_delete_object_api_requires_server_side_name_confirmation(service: WorkbenchService):
    actor_id = _actor(service)
    obj = service.create_object(object_type="expert_team", object_key="api_delete_team", name="接口删除专家团", actor_id=actor_id)
    app = FastAPI()
    app.include_router(build_workbench_router(service), prefix="/workbench/v1")

    with TestClient(app) as client:
        mismatch = client.request(
            "DELETE", f"/workbench/v1/objects/{obj['object_id']}",
            json={"confirmation_name": "不匹配", "actor_id": actor_id},
        )
        deleted = client.request(
            "DELETE", f"/workbench/v1/objects/{obj['object_id']}",
            json={"confirmation_name": "接口删除专家团", "actor_id": actor_id},
        )

    assert mismatch.status_code == 422
    assert deleted.status_code == 200
    assert deleted.json()["permanent"] is True


def test_id_migration_creates_successor_draft_without_changing_source_history(service: WorkbenchService):
    actor_id = _actor(service)
    source = service.create_object(object_type="skill", object_key="old_skill", name="旧能力", actor_id=actor_id)
    revision = service.save_revision(
        source["object_id"], base_revision_id=None,
        payload={"prompt_markdown": "按资料回答", "runtime_contract": {}, "capability_ids": []}, dependency_locks=[],
        assets=[_text_asset("references/guide.md", "保留资料", "text/markdown"), _text_asset("scripts/helper.py", "def helper():\n    return 'ok'\n", "text/x-python")], actor_id=actor_id,
    )
    migration = service.migrate_object_id(
        source["object_id"], new_object_key="new_skill", confirmation_name="旧能力", actor_id=actor_id,
    )

    successor = migration["successor"]
    assert successor["object_key"] == "new_skill"
    assert successor["latest_release_no"] == 0
    assert service.get_object(source["object_id"])["object_key"] == "old_skill"
    copied = service.get_object(successor["object_id"])["revisions"][0]
    assert copied["payload"]["prompt_markdown"] == revision["payload"]["prompt_markdown"]
    assert {item["relative_path"] for item in service.list_revision_assets(copied["revision_id"])} == {"references/guide.md", "scripts/helper.py"}
    assert any(event["action"] == "object.id_migrated" for event in service.list_audit_events())


def test_id_migration_requires_confirmation_and_valid_available_key(service: WorkbenchService):
    actor_id = _actor(service)
    source, _, _ = _publish_skill(service, actor_id, key="source_skill")
    with pytest.raises(WorkbenchError) as mismatch:
        service.migrate_object_id(source["object_id"], new_object_key="target_skill", confirmation_name="错误", actor_id=actor_id)
    assert mismatch.value.code == "ID_MIGRATION_CONFIRMATION_MISMATCH"
    with pytest.raises(WorkbenchError) as invalid:
        service.migrate_object_id(source["object_id"], new_object_key="not valid", confirmation_name=source["name"], actor_id=actor_id)
    assert invalid.value.code == "ID_MIGRATION_KEY_INVALID"
    with pytest.raises(WorkbenchConflict) as duplicate:
        service.migrate_object_id(source["object_id"], new_object_key="source_skill", confirmation_name=source["name"], actor_id=actor_id)
    assert duplicate.value.code == "OBJECT_KEY_CONFLICT"


def test_id_migration_api_returns_canonical_confirmation_error(service: WorkbenchService):
    actor_id = _actor(service)
    source, _, _ = _publish_skill(service, actor_id, key="api_migration_source")
    app = FastAPI()
    app.include_router(build_workbench_router(service), prefix="/workbench/v1")
    with TestClient(app) as client:
        mismatch = client.post(
            f"/workbench/v1/objects/{source['object_id']}/id-migrations",
            json={"new_object_key": "api_migration_target", "confirmation_name": "错误", "actor_id": actor_id},
        )
        created = client.post(
            f"/workbench/v1/objects/{source['object_id']}/id-migrations",
            json={"new_object_key": "api_migration_target", "confirmation_name": source["name"], "actor_id": actor_id},
        )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["code"] == "ID_MIGRATION_CONFIRMATION_MISMATCH"
    assert created.status_code == 201
    assert created.json()["successor"]["object_key"] == "api_migration_target"


def test_expert_and_team_id_migrations_copy_their_latest_drafts(service: WorkbenchService):
    actor_id = _actor(service)
    _, _, skill_a = _publish_skill(service, actor_id, key="migration_skill_a")
    _, _, skill_b = _publish_skill(service, actor_id, key="migration_skill_b")
    expert_a, _, expert_a_release = _publish_expert(service, actor_id, key="migration_expert_a", skill_release=skill_a)
    _, _, expert_b_release = _publish_expert(service, actor_id, key="migration_expert_b", skill_release=skill_b)
    team, _, _ = _publish_team(service, actor_id, key="migration_team", expert_releases=[expert_a_release, expert_b_release])

    expert_successor = service.migrate_object_id(expert_a["object_id"], new_object_key="migration_expert_a_new", confirmation_name=expert_a["name"], actor_id=actor_id)
    team_successor = service.migrate_object_id(team["object_id"], new_object_key="migration_team_new", confirmation_name=team["name"], actor_id=actor_id)

    assert expert_successor["revision"]["dependency_locks"][0]["release_id"] == skill_a["release_id"]
    assert team_successor["revision"]["dependency_locks"][0]["release_id"] == expert_a_release["release_id"]
    assert team_successor["successor"]["latest_release_no"] == 0


def test_reference_migration_creates_next_draft_and_rewrites_team_members(service: WorkbenchService):
    actor_id = _actor(service)
    skill_one, _, skill_one_release = _publish_skill(service, actor_id, key="skill_one")
    _, _, skill_two_release = _publish_skill(service, actor_id, key="skill_two")
    expert_one, _, expert_one_release = _publish_expert(service, actor_id, key="expert_one", skill_release=skill_one_release)
    _, _, expert_two_release = _publish_expert(service, actor_id, key="expert_two", skill_release=skill_two_release)
    team, _, _ = _publish_team(service, actor_id, key="team_one", expert_releases=[expert_one_release, expert_two_release])

    replacement = service.migrate_object_id(skill_one["object_id"], new_object_key="skill_one_new", confirmation_name=skill_one["name"], actor_id=actor_id)
    replacement_revision = replacement["revision"]
    debug = service.create_debug_session(replacement_revision["revision_id"], baseline_release_id=None, actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    replacement_release = service.publish_revision(replacement_revision["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True, confirmation_notes="通过", actor_id=actor_id)
    migrated = service.migrate_references(
        from_release_id=skill_one_release["release_id"], to_release_id=replacement_release["release_id"],
        confirmation_name=skill_one["name"], actor_id=actor_id,
    )
    expert_draft = next(item for item in migrated["created"] if item["object"]["object_id"] == expert_one["object_id"])["revision"]
    assert expert_draft["dependency_locks"][0]["object_key"] == "skill_one_new"

    debug = service.create_debug_session(expert_draft["revision_id"], baseline_release_id=expert_one_release["release_id"], actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    expert_new_release = service.publish_revision(expert_draft["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True, confirmation_notes="通过", actor_id=actor_id)
    team_migration = service.migrate_references(
        from_release_id=expert_one_release["release_id"], to_release_id=expert_new_release["release_id"],
        confirmation_name=expert_one["name"], actor_id=actor_id,
    )
    team_draft = next(item for item in team_migration["created"] if item["object"]["object_id"] == team["object_id"])["revision"]
    assert any(lock["release_id"] == expert_new_release["release_id"] for lock in team_draft["dependency_locks"])
    assert team_draft["payload"]["members"][0]["expert_id"] == "expert_one"

    # Renaming an expert itself changes both the team lock and the member ID.
    team_for_renamed_expert, _, _ = _publish_team(
        service, actor_id, key="team_for_renamed_expert", expert_releases=[expert_one_release, expert_two_release],
    )
    expert_successor = service.migrate_object_id(
        expert_one["object_id"], new_object_key="expert_one_new", confirmation_name=expert_one["name"], actor_id=actor_id,
    )
    debug = service.create_debug_session(expert_successor["revision"]["revision_id"], baseline_release_id=None, actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    expert_successor_release = service.publish_revision(
        expert_successor["revision"]["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True, confirmation_notes="通过", actor_id=actor_id,
    )
    renamed_team_migration = service.migrate_references(
        from_release_id=expert_one_release["release_id"], to_release_id=expert_successor_release["release_id"],
        confirmation_name=expert_one["name"], actor_id=actor_id,
    )
    renamed_team_draft = next(item for item in renamed_team_migration["created"] if item["object"]["object_id"] == team_for_renamed_expert["object_id"])["revision"]
    assert renamed_team_draft["payload"]["members"][0]["expert_id"] == "expert_one_new"
    assert renamed_team_draft["payload"]["coordinator_expert_id"] == expert_successor["successor"]["object_id"]


def test_skill_revision_preserves_editable_reference_and_safe_python_script(service: WorkbenchService):
    actor_id = _actor(service)
    obj = service.create_object(object_type="skill", object_key="editable_skill", name="可编辑 Skill", actor_id=actor_id)
    revision = service.save_revision(
        obj["object_id"], base_revision_id=None,
        payload={"prompt_markdown": "按资料回答", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[],
        assets=[
            _text_asset("references/rules.md", "# 规则\n仅使用已知事实。", "text/markdown"),
            _text_asset("assets/lookup.json", '{"城市":"杭州"}\n', "application/json"),
            _text_asset("scripts/score.py", "def main(payload: dict) -> dict:\n    return {'score': 1}\n", "text/x-python"),
        ],
        actor_id=actor_id,
    )

    assets = service.list_revision_assets(revision["revision_id"])
    debug = service.create_debug_session(revision["revision_id"], baseline_release_id=None, actor_id=actor_id)
    snapshot = debug["snapshot"]
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    release = service.publish_revision(
        revision["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True,
        confirmation_notes="脚本已审查", actor_id=actor_id,
    )
    package, _manifest = service.export_release(release["release_id"], actor_id=actor_id)
    imported = service.import_package(package, environment="prod", actor_id=actor_id)

    assert revision["validation"]["valid"] is True
    assert {item["relative_path"] for item in assets} == {"references/rules.md", "assets/lookup.json", "scripts/score.py"}
    assert snapshot["entries"][0]["runtime_root"]
    assert "content_base64" not in snapshot["entries"][0]["files"][0]
    assert imported["status"] == "staged"
    assert any(path.endswith("/assets/lookup.json") for path in service._read_zip(package))
    assert any(path.endswith("/scripts/score.py") for path in service._read_zip(package))
    bundle = SimpleNamespace(
        _skill_markdown="filesystem", _skill_markdown_loader=None,
        root_dir=None, skill_file=None, references={}, local_assets={}, scripts={}, _references={}, _references_loader=None,
        _local_assets={}, _local_assets_loader=None, _scripts={},
    )
    configured = configured_skill_bundle(bundle, snapshot["entries"][0])
    assert configured._references["references/rules.md"].startswith("# 规则")
    assert configured._local_assets["assets/lookup.json"] == '{"城市":"杭州"}\n'
    assert configured._scripts["scripts/score.py"].is_file()
    loaded = skill_bundle_from_entry(snapshot["entries"][0])
    assert loaded.local_assets["assets/lookup.json"] == '{"城市":"杭州"}\n'
    assert loaded.runtime_metadata.assets.local_enabled is True


def test_team_package_can_import_recursively_into_workbench_objects():
    source = _blank_service()
    target = _blank_service()
    source_actor = _actor(source)
    target_actor = _actor(target)
    _, _, skill_a = _publish_skill(source, source_actor, key="skill_a", prompt="A")
    _, _, skill_b = _publish_skill(source, source_actor, key="skill_b", prompt="B")
    _, _, expert_a = _publish_expert(source, source_actor, key="expert_a", skill_release=skill_a)
    _, _, expert_b = _publish_expert(source, source_actor, key="expert_b", skill_release=skill_b)
    _team, _revision, team_release = _publish_team(
        source,
        source_actor,
        key="team_a",
        expert_releases=[expert_a, expert_b],
    )

    package, _manifest = source.export_release(team_release["release_id"], actor_id=source_actor)
    exported = source._validated_package_contents(package, materialize_entries=False)
    exported_briefs = {
        (entry["object_type"], entry["object_key"]): entry["payload"].get("brief", "")
        for entry in exported["entries"]
    }
    imported = target.import_object_package(package, actor_id=target_actor)

    objects = target.list_objects()
    assert imported["created_objects"] == 5
    assert imported["created_releases"] == 5
    assert {item["object_type"] for item in objects} == {"skill", "expert", "expert_team"}
    assert {item["object_key"] for item in objects} == {"skill_a", "skill_b", "expert_a", "expert_b", "team_a"}
    assert exported_briefs[("expert", "expert_a")] == "expert_a 的专家服务摘要"
    assert exported_briefs[("expert_team", "team_a")] == "team_a 的专家团服务摘要"
    team = next(item for item in objects if item["object_type"] == "expert_team")
    team_detail = target.get_object(team["object_id"])
    assert len(team_detail["releases"]) == 1
    assert team_detail["brief"] == "team_a 的专家团服务摘要"
    assert {item["object_key"] for item in team_detail["releases"][0]["dependency_locks"]} == {"expert_a", "expert_b"}


def test_legacy_package_without_brief_imports_but_requires_a_new_brief_to_publish():
    source = _blank_service()
    target = _blank_service()
    source_actor = _actor(source)
    target_actor = _actor(target)
    _, _, skill_a = _publish_skill(source, source_actor, key="legacy_brief_skill_a")
    _, _, skill_b = _publish_skill(source, source_actor, key="legacy_brief_skill_b")
    _, _, expert_a = _publish_expert(source, source_actor, key="legacy_brief_expert_a", skill_release=skill_a)
    _, _, expert_b = _publish_expert(source, source_actor, key="legacy_brief_expert_b", skill_release=skill_b)
    _, _, team_release = _publish_team(
        source, source_actor, key="legacy_brief_team", expert_releases=[expert_a, expert_b],
    )
    package, _ = source.export_release(team_release["release_id"], actor_id=source_actor)
    files = source._read_zip(package)
    manifest = json.loads(files["manifest.json"])
    for item in manifest["objects"]:
        if item["object_type"] not in {"expert", "expert_team"}:
            continue
        object_path = next(file["path"] for file in item["files"] if file["path"].endswith("/object.json"))
        declaration = json.loads(files[object_path])
        declaration["payload"].pop("brief", None)
        files[object_path] = json.dumps(declaration, ensure_ascii=False).encode()
        file_item = next(file for file in item["files"] if file["path"] == object_path)
        file_item["sha256"] = hashlib.sha256(files[object_path]).hexdigest()
        file_item["size_bytes"] = len(files[object_path])
    manifest.pop("manifest_hash")
    manifest["manifest_hash"] = _hash(manifest)
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()

    target.import_object_package(target._zip(files), actor_id=target_actor)

    imported_expert = next(item for item in target.list_objects() if item["object_key"] == "legacy_brief_expert_a")
    legacy_revision = target.get_object(imported_expert["object_id"])["revisions"][0]
    assert legacy_revision["validation"]["valid"] is True
    assert "历史专家包缺少 Brief，请补齐后再发布" in legacy_revision["validation"]["warnings"]
    repaired = target.save_revision(
        imported_expert["object_id"],
        base_revision_id=legacy_revision["revision_id"],
        payload={**legacy_revision["payload"], "brief": "已补齐的历史专家摘要"},
        dependency_locks=[{"release_id": lock["release_id"]} for lock in legacy_revision["dependency_locks"]],
        assets=[],
        actor_id=target_actor,
    )
    assert repaired["validation"]["valid"] is True


def test_recursive_import_unarchives_reused_dependency_objects():
    source = _blank_service()
    target = _blank_service()
    source_actor = _actor(source)
    target_actor = _actor(target)
    _, _, source_release = _publish_skill(source, source_actor, key="archived_dependency", prompt="导入后恢复")
    package, _manifest = source.export_release(source_release["release_id"], actor_id=source_actor)
    archived = target.create_object(
        object_type="skill", object_key="archived_dependency", name="旧归档技能", actor_id=target_actor,
    )
    target.archive_object(archived["object_id"], actor_id=target_actor)

    imported = target.import_object_package(package, actor_id=target_actor)

    visible = target.list_objects()
    assert imported["reused_objects"] == 1
    assert imported["unarchived_objects"] == 1
    assert visible[0]["object_key"] == "archived_dependency"
    assert visible[0]["archived"] is False
    assert imported["entries"][0]["unarchived"] is True


def test_recursive_object_import_api_returns_the_import_summary(service: WorkbenchService):
    source = _blank_service()
    source_actor = _actor(source)
    target_actor = _actor(service)
    _, _, skill_a = _publish_skill(source, source_actor, key="api_skill_a")
    _, _, skill_b = _publish_skill(source, source_actor, key="api_skill_b")
    _, _, expert_a = _publish_expert(source, source_actor, key="api_expert_a", skill_release=skill_a)
    _, _, expert_b = _publish_expert(source, source_actor, key="api_expert_b", skill_release=skill_b)
    _, _, team_release = _publish_team(
        source,
        source_actor,
        key="api_team",
        expert_releases=[expert_a, expert_b],
    )
    package, _ = source.export_release(team_release["release_id"], actor_id=source_actor)
    app = FastAPI()
    app.include_router(build_workbench_router(service), prefix="/workbench/v1")

    with TestClient(app) as client:
        response = client.post(
            f"/workbench/v1/imports?actor_id={target_actor}",
            content=package,
            headers={"Content-Type": "application/zip"},
        )

    assert response.status_code == 201
    assert response.json()["created_objects"] == 5
    assert response.json()["created_releases"] == 5


def test_deployment_router_uses_the_running_service_environment(monkeypatch, service: WorkbenchService):
    monkeypatch.setenv("HAILIANG_DEPLOY_ENV", "test")
    app = FastAPI()
    app.include_router(build_deployment_router(service), prefix="/deployment/v1")

    with TestClient(app) as client:
        default_environment = client.get("/deployment/v1/deployments")
        mismatched_environment = client.get("/deployment/v1/deployments?environment=prod")

    assert default_environment.status_code == 200
    assert mismatched_environment.status_code == 409
    assert mismatched_environment.json()["detail"]["code"] == "DEPLOYMENT_ENVIRONMENT_MISMATCH"


def test_recursive_object_import_rejects_malformed_manifest_with_a_structured_error(service: WorkbenchService):
    actor_id = _actor(service)
    malformed_package = service._zip({"manifest.json": b"[]"})
    app = FastAPI()
    app.include_router(build_workbench_router(service), prefix="/workbench/v1")

    with TestClient(app) as client:
        response = client.post(
            f"/workbench/v1/imports?actor_id={actor_id}",
            content=malformed_package,
            headers={"Content-Type": "application/zip"},
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_PACKAGE"
    assert response.json()["detail"]["message"] == "manifest.json 必须是对象"


def test_recursive_object_import_reuses_existing_same_key_and_same_content():
    source = _blank_service()
    target = _blank_service()
    source_actor = _actor(source)
    target_actor = _actor(target)
    _, _, skill_release = _publish_skill(source, source_actor, key="skill_same", prompt="同一内容")
    package, _manifest = source.export_release(skill_release["release_id"], actor_id=source_actor)

    first = target.import_object_package(package, actor_id=target_actor)
    second = target.import_object_package(package, actor_id=target_actor)

    objects = target.list_objects()
    detail = target.get_object(objects[0]["object_id"])
    assert first["created_objects"] == 1
    assert second["created_objects"] == 0
    assert second["reused_releases"] == 1
    assert len(objects) == 1
    assert len(detail["revisions"]) == 1
    assert len(detail["releases"]) == 1


def test_recursive_object_import_appends_new_version_when_same_key_but_content_differs():
    source = _blank_service()
    target = _blank_service()
    source_actor = _actor(source)
    target_actor = _actor(target)
    _, _, source_release = _publish_skill(source, source_actor, key="shared_skill", prompt="源版本")
    _publish_skill(target, target_actor, key="shared_skill", prompt="本地版本")
    package, _manifest = source.export_release(source_release["release_id"], actor_id=source_actor)

    imported = target.import_object_package(package, actor_id=target_actor)

    objects = target.list_objects()
    assert len(objects) == 1
    detail = target.get_object(objects[0]["object_id"])
    assert imported["created_objects"] == 0
    assert imported["created_revisions"] == 1
    assert imported["created_releases"] == 1
    assert len(detail["revisions"]) == 2
    assert len(detail["releases"]) == 2


def test_recursive_object_import_preserves_skill_assets_and_script_validation():
    source = _blank_service()
    target = _blank_service()
    source_actor = _actor(source)
    target_actor = _actor(target)
    obj = source.create_object(object_type="skill", object_key="asset_skill", name="资产技能", actor_id=source_actor)
    revision = source.save_revision(
        obj["object_id"],
        base_revision_id=None,
        payload={"prompt_markdown": "按资料回答", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[],
        assets=[
            _text_asset("references/rules.md", "# 规则\n仅使用已知事实。", "text/markdown"),
            _text_asset("scripts/score.py", "def main(payload: dict) -> dict:\n    return {'score': 2}\n", "text/x-python"),
        ],
        actor_id=source_actor,
    )
    debug = source.create_debug_session(revision["revision_id"], baseline_release_id=None, actor_id=source_actor)
    source.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=source_actor)
    release = source.publish_revision(
        revision["revision_id"],
        evidence_id=debug["debug_session_id"],
        manual_confirmation=True,
        confirmation_notes="通过",
        actor_id=source_actor,
    )
    package, _manifest = source.export_release(release["release_id"], actor_id=source_actor)

    imported = target.import_object_package(package, actor_id=target_actor)

    imported_skill = next(item for item in target.list_objects() if item["object_key"] == "asset_skill")
    detail = target.get_object(imported_skill["object_id"])
    assets = target.list_revision_assets(detail["revisions"][0]["revision_id"])
    assert imported["created_objects"] == 1
    assert {item["relative_path"] for item in assets} == {"references/rules.md", "scripts/score.py"}
    assert detail["revisions"][0]["validation"]["valid"] is True


def test_skill_revision_with_assets_flushes_parent_before_assets_on_postgres_like_database():
    """Foreign-key enforcement reproduces the PostgreSQL insert ordering rule."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys = ON")

    Base.metadata.create_all(engine)
    strict_service = WorkbenchService(sessionmaker(bind=engine, expire_on_commit=False))
    actor_id = _actor(strict_service)
    obj = strict_service.create_object(
        object_type="skill",
        object_key="fk_safe_assets",
        name="外键安全文件保存",
        actor_id=actor_id,
    )
    first = strict_service.save_revision(
        obj["object_id"],
        base_revision_id=None,
        payload={"prompt_markdown": "初版", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[],
        assets=[_text_asset("references/initial.md", "初版资料", "text/markdown")],
        actor_id=actor_id,
    )

    second = strict_service.save_revision(
        obj["object_id"],
        base_revision_id=first["revision_id"],
        payload={"prompt_markdown": "第二版", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[],
        assets=[_text_asset("references/updated.md", "更新资料", "text/markdown")],
        actor_id=actor_id,
    )

    assert second["revision_no"] == 2
    assert [item["relative_path"] for item in strict_service.list_revision_assets(second["revision_id"])] == [
        "references/updated.md"
    ]


def test_revision_test_runs_the_manually_selected_non_latest_skill_revision():
    preview_service = _preview_service()
    actor_id = _actor(preview_service)
    obj = preview_service.create_object(
        object_type="skill", object_key="parallel_skill", name="并行修订 Skill", actor_id=actor_id,
    )
    first = preview_service.save_revision(
        obj["object_id"], base_revision_id=None,
        payload={"prompt_markdown": "第一份候选规则", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[], assets=[], actor_id=actor_id,
    )
    preview_service.save_revision(
        obj["object_id"], base_revision_id=first["revision_id"],
        payload={"prompt_markdown": "第二份最新规则", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[], assets=[], actor_id=actor_id,
    )

    session = preview_service.create_revision_test_session(first["revision_id"], actor_id=actor_id)
    result = preview_service.run_revision_test_turn(
        session["debug_session_id"], user_message="测试 r1", actor_id=actor_id,
    )

    assert result["debug_session"]["revision_id"] == first["revision_id"]
    assert "第一份候选规则" in result["assistant_message"]
    assert "第二份最新规则" not in result["assistant_message"]


def test_revision_test_evidence_keeps_a_per_turn_execution_debug_summary():
    preview_service = _preview_service()
    actor_id = _actor(preview_service)
    obj = preview_service.create_object(
        object_type="skill", object_key="auditable_skill", name="可审计 Skill", actor_id=actor_id,
    )
    revision = preview_service.save_revision(
        obj["object_id"],
        base_revision_id=None,
        payload={"prompt_markdown": "回答业务问题", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[],
        assets=[
            _text_asset("references/guide.md", "测试资料", "text/markdown"),
            _text_asset("scripts/calculate.py", "print('ok')", "text/x-python"),
        ],
        actor_id=actor_id,
    )
    session = preview_service.create_revision_test_session(revision["revision_id"], actor_id=actor_id)
    result = preview_service.run_revision_test_turn(
        session["debug_session_id"], user_message="开始测试", actor_id=actor_id,
    )

    trace = result["debug_session"]["trace"]
    assert len(trace) == 1
    assert trace[0]["turn"] == 1
    assert trace[0]["debug"]["skill"]["object_key"] == "auditable_skill"
    assert trace[0]["debug"]["references"]["used"] == []
    assert trace[0]["debug"]["references"]["available_but_not_used"][0]["path"] == "references/guide.md"
    script = trace[0]["debug"]["scripts"][0]
    assert script["path"] == "scripts/calculate.py"
    assert script["status"] == "not_executed"
    assert any(event["event_type"] == "candidate_turn_started" for event in trace[0]["events"])


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ({"ok": True}, True),
        ({"ok": False, "exit_code": "success"}, False),
        ({"json_output": {"ok": True}, "exit_code": "success"}, True),
        ({"return_value": {"ok": True}, "exit_code": 0}, True),
        ({"exit_code": "success"}, True),
        ({"exit_code": 1}, False),
    ],
)
def test_candidate_script_execution_status_normalizes_sandbox_shapes(output, expected):
    assert _script_execution_succeeded(output) is expected


def test_candidate_stream_assigns_a_generation_before_runtime_execution(monkeypatch):
    preview_service = _preview_service()
    actor_id = _actor(preview_service)
    obj = preview_service.create_object(
        object_type="skill", object_key="streaming_candidate", name="流式候选 Skill", actor_id=actor_id,
    )
    revision = preview_service.save_revision(
        obj["object_id"],
        base_revision_id=None,
        payload={"prompt_markdown": "候选规则", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[],
        assets=[],
        actor_id=actor_id,
    )
    session = preview_service.create_revision_test_session(revision["revision_id"], actor_id=actor_id)
    observed: dict[str, object] = {}
    emitted: list[tuple[str, dict[str, object]]] = []

    def execute(_snapshot, message, context):
        observed["generation"] = context.session_meta.get("active_stream_generation")
        observed["cancelled"] = context.session_meta.get("cancelled_stream_generation")
        observed["cancel_check"] = context.session_meta["stream_cancel_check"]()
        context.add_message("user", message)
        context.add_message("assistant", "已流式执行")
        return "已流式执行"

    monkeypatch.setattr(preview_service, "_execute_snapshot_message", execute)
    preview_service.run_revision_test_turn(
        session["debug_session_id"],
        user_message="开始测试",
        actor_id=actor_id,
        on_event=lambda event, payload: emitted.append((event, payload)),
    )

    assert str(observed["generation"]).startswith("candidate_stream_")
    assert observed["cancelled"] is None
    assert observed["cancel_check"] is False
    with preview_service.session_factory() as db:
        row = db.get(WorkbenchDebugSessionRow, session["debug_session_id"])
        assert row is not None
        assert "active_stream_generation" not in row.runtime_context["session_meta"]
        assert "stream_cancel_check" not in row.runtime_context["session_meta"]
    states = [payload for event, payload in emitted if event == "state"]
    assert states
    assert all(state["protocol"] == "hailiang.sse.v2" for state in states)
    assert states[-1]["status"] == "completed"
    assert states[-1]["context_scope"] == "unbound"
    assert states[-1]["profile_id"] is None
    assert states[-1]["expert_context"]["branch_version"] == 1


def test_candidate_model_timeout_returns_a_structured_workbench_error(monkeypatch):
    preview_service = _preview_service()
    actor_id = _actor(preview_service)
    obj = preview_service.create_object(
        object_type="skill", object_key="timeout_candidate", name="超时候选 Skill", actor_id=actor_id,
    )
    revision = preview_service.save_revision(
        obj["object_id"],
        base_revision_id=None,
        payload={"prompt_markdown": "候选规则", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[], assets=[], actor_id=actor_id,
    )
    session = preview_service.create_revision_test_session(revision["revision_id"], actor_id=actor_id)
    monkeypatch.setattr(
        preview_service,
        "_execute_snapshot_message",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("model request timed out")),
    )

    with pytest.raises(WorkbenchError) as error:
        preview_service.run_revision_test_turn(
            session["debug_session_id"], user_message="开始测试", actor_id=actor_id, on_event=lambda *_args: None,
        )

    assert error.value.code == "MODEL_TIMEOUT"
    assert error.value.details["timeout_s"] > 0


def test_candidate_stream_has_an_end_to_end_deadline(monkeypatch):
    preview_service = _preview_service()
    actor_id = _actor(preview_service)
    obj = preview_service.create_object(
        object_type="skill", object_key="deadline_candidate", name="整轮超时 Skill", actor_id=actor_id,
    )
    revision = preview_service.save_revision(
        obj["object_id"], base_revision_id=None,
        payload={"prompt_markdown": "候选规则", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[], assets=[], actor_id=actor_id,
    )
    session = preview_service.create_revision_test_session(revision["revision_id"], actor_id=actor_id)
    blocked = threading.Event()
    monkeypatch.setattr(workbench_routes, "WORKBENCH_CANDIDATE_LLM_TIMEOUT_S", 0.05)
    monkeypatch.setattr(preview_service, "_execute_snapshot_message", lambda *_args: blocked.wait(1) or "不应完成")
    app = FastAPI()
    app.include_router(build_workbench_router(preview_service), prefix="/workbench/v1")

    with TestClient(app) as client:
        response = client.post(
            f"/workbench/v1/revision-tests/{session['debug_session_id']}/turns/stream",
            json={"user_message": "开始测试", "actor_id": actor_id},
        )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "REVISION_TEST_TIMEOUT" in response.text
    assert "\"status\": \"failed\"" in response.text


def test_stopped_candidate_worker_cannot_overwrite_a_newer_turn(monkeypatch):
    preview_service = _preview_service()
    actor_id = _actor(preview_service)
    obj = preview_service.create_object(
        object_type="skill", object_key="stopped_candidate", name="停止候选 Skill", actor_id=actor_id,
    )
    revision = preview_service.save_revision(
        obj["object_id"],
        base_revision_id=None,
        payload={"prompt_markdown": "候选规则", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[], assets=[], actor_id=actor_id,
    )
    session = preview_service.create_revision_test_session(revision["revision_id"], actor_id=actor_id)

    def execute(_snapshot, _message, context):
        generation = str(context.session_meta["active_stream_generation"])
        context.session_meta["cancelled_stream_generation"] = generation
        with preview_service._active_candidate_streams_lock:
            preview_service._active_candidate_streams[session["debug_session_id"]] = {"run_id": "candidate_stream_newer"}
        return "不应覆盖新一轮"

    monkeypatch.setattr(preview_service, "_execute_snapshot_message", execute)
    result = preview_service.run_revision_test_turn(
        session["debug_session_id"], user_message="停止后旧响应", actor_id=actor_id, on_event=lambda *_args: None,
    )

    assert result["trace"] == []
    with preview_service.session_factory() as db:
        row = db.get(WorkbenchDebugSessionRow, session["debug_session_id"])
        assert row is not None
        assert row.transcript == []
    with preview_service._active_candidate_streams_lock:
        assert preview_service._active_candidate_streams[session["debug_session_id"]]["run_id"] == "candidate_stream_newer"


def test_candidate_stop_marks_live_runtime_and_emits_stopped_snapshot():
    preview_service = _preview_service()
    context = SessionContext(session_id="revision_test_dbg_stop", user_id="workbench-candidate-dbg_stop")
    context.session_meta["active_stream_generation"] = "candidate_stream_stop"
    emitted: list[str] = []

    def emit_state(status: str) -> None:
        emitted.append(status)
        with preview_service._active_candidate_streams_lock:
            preview_service._active_candidate_streams["dbg_stop"]["last_state"] = {
                "protocol": "hailiang.sse.v2",
                "status": status,
                "assistant": {"content": "已生成的部分内容", "status": status},
            }

    with preview_service._active_candidate_streams_lock:
        preview_service._active_candidate_streams["dbg_stop"] = {
            "run_id": "candidate_stream_stop",
            "context": context,
            "emit_state": emit_state,
            "last_state": None,
        }

    result = preview_service.stop_revision_test_turn(
        "dbg_stop",
        run_id="candidate_stream_stop",
        actor_id="actor_test",
    )

    assert context.session_meta["cancelled_stream_generation"] == "candidate_stream_stop"
    assert emitted == ["stopped"]
    assert result["status"] == "stopped"
    assert result["state"]["assistant"]["content"] == "已生成的部分内容"


def test_candidate_expert_can_redispatch_to_another_locked_skill():
    preview_service = _preview_service()
    preview_service.orchestrator = None
    actor_id = _actor(preview_service)
    _, _, disc_release = _publish_skill(preview_service, actor_id, key="disc_skill", prompt="DISC 候选规则")
    _, _, family_release = _publish_skill(preview_service, actor_id, key="family_skill", prompt="家庭关系候选规则")
    expert = preview_service.create_object(
        object_type="expert", object_key="switching_expert", name="可切换专家", actor_id=actor_id,
    )
    expert_revision = preview_service.save_revision(
        expert["object_id"], base_revision_id=None,
        payload={"rules_markdown": "根据用户意图选择锁定 Skill", "brief": "候选修订专家", "budget": {}},
        dependency_locks=[{"release_id": disc_release["release_id"]}, {"release_id": family_release["release_id"]}],
        assets=[], actor_id=actor_id,
    )
    preview_service.orchestrator = _RedispatchPreviewOrchestrator()
    session = preview_service.create_revision_test_session(expert_revision["revision_id"], actor_id=actor_id)
    first = preview_service.run_revision_test_turn(session["debug_session_id"], user_message="做性格测试", actor_id=actor_id)
    switched = preview_service.run_revision_test_turn(
        session["debug_session_id"], user_message="用另一个技能来解答吧", actor_id=actor_id,
    )

    assert "DISC 候选规则" in first["assistant_message"]
    assert "家庭关系候选规则" in switched["assistant_message"]
    latest_events = switched["debug_session"]["trace"][-1]["events"]
    assert any(event["event_type"] == "expert_skill_executed" for event in latest_events)
    assert switched["debug_session"]["trace"][-1]["debug"]["skill"]["object_key"] == "family_skill"


def test_candidate_expert_semantically_redispatches_a_new_task_to_locked_skill():
    preview_service = _preview_service()
    preview_service.orchestrator = None
    actor_id = _actor(preview_service)
    _, _, disc_release = _publish_skill(preview_service, actor_id, key="disc_skill", prompt="DISC 候选规则")
    _, _, family_release = _publish_skill(preview_service, actor_id, key="family_skill", prompt="家庭关系候选规则")
    expert = preview_service.create_object(
        object_type="expert", object_key="switching_expert", name="语义切换专家", actor_id=actor_id,
    )
    expert_revision = preview_service.save_revision(
        expert["object_id"], base_revision_id=None,
        payload={"rules_markdown": "根据用户意图选择锁定 Skill", "brief": "候选修订专家", "budget": {}},
        dependency_locks=[{"release_id": disc_release["release_id"]}, {"release_id": family_release["release_id"]}],
        assets=[], actor_id=actor_id,
    )
    preview_service.orchestrator = _RedispatchPreviewOrchestrator()
    session = preview_service.create_revision_test_session(expert_revision["revision_id"], actor_id=actor_id)
    preview_service.run_revision_test_turn(session["debug_session_id"], user_message="做性格测试", actor_id=actor_id)
    switched = preview_service.run_revision_test_turn(
        session["debug_session_id"], user_message="我现在更想解决亲子沟通冲突", actor_id=actor_id,
    )

    assert "家庭关系候选规则" in switched["assistant_message"]
    latest_events = switched["debug_session"]["trace"][-1]["events"]
    assert any(event["event_type"] == "expert_skill_executed" for event in latest_events)


def test_candidate_team_handoff_is_persisted_as_an_interactive_block_and_can_be_confirmed():
    context = SessionContext()
    context.session_meta.update({
        "expert_team_id": "team_a",
        "active_expert_id": "coordinator_expert",
        "configuration_snapshot": {
            "entries": [
                {
                    "object_type": "expert_team",
                    "object_key": "team_a",
                    "dependency_locks": [{"object_type": "expert", "object_key": "family_expert"}],
                },
            ],
        },
    })
    context.messages = [
        {"role": "user", "content": "孩子最近总是顶嘴"},
            {
                "role": "assistant",
                "message_id": "msg_handoff_1",
                "content": "建议转交家庭教育专家。",
            "blocks": [{"type": "fact_form", "payload": {"form_id": "handoff_context", "fields": []}}],
            "team_handoff": {
                "handoff_id": "handoff_1",
                "status": "active",
                "team_id": "team_a",
                "candidates": [{"expert_id": "family_expert", "mention_name": "家庭教育专家"}],
                "reason": "更适合亲子沟通问题",
            },
        },
    ]
    context.skill_states["skill_runtime"] = {
        "active_skill_id": "team_skill",
        "skill_facts": {"team_skill": {"_pending_questionnaire": {"form_id": "handoff_context"}}},
    }

    blocks = WorkbenchService._latest_assistant_blocks(context)
    assert blocks[-1]["type"] == "team_handoff"
    message = WorkbenchService._revision_test_input_message(
        context,
        "",
        None,
            {
                "source_message_id": "msg_handoff_1",
                "handoff_id": "handoff_1",
                "target_expert_id": "family_expert",
            },
    )

    assert "孩子最近总是顶嘴" in message
    assert context.session_meta["active_expert_id"] == "family_expert"
    assert context.messages[-1]["team_handoff"]["status"] == "selected"
    assert context.session_meta["team_member_switch"]["source"] == "team_handoff"
    assert context.session_meta["team_member_switch"]["target_expert_id"] == "family_expert"
    assert context.messages[-1]["interaction_states"]["fact_form:handoff_context"]["status"] == "expired"
    assert "_pending_questionnaire" not in context.skill_states["skill_runtime"]["skill_facts"]["team_skill"]
    assert any(event["event_type"] == "form_abandoned" for event in context.event_trace)
    assert "family_expert" not in message


def test_candidate_team_manual_at_expert_uses_a_snapshot_locked_structured_switch():
    context = SessionContext(
        session_id="revision_test_dbg_at",
        user_id="workbench-candidate-dbg_at",
        profile_id="anonymous-candidate",
    )
    context.session_meta.update({
        "expert_team_id": "team_a",
        "active_expert_id": "coordinator_expert",
        "configuration_snapshot": {
            "entries": [
                {
                    "object_type": "expert_team",
                    "object_key": "team_a",
                    "payload": {"members": [{"expert_id": "family_expert", "mention_name": "家庭教育专家"}]},
                    "dependency_locks": [
                        {"object_type": "expert", "object_key": "coordinator_expert"},
                        {"object_type": "expert", "object_key": "family_expert"},
                    ],
                },
                {"object_type": "expert", "object_key": "family_expert", "name": "家庭教育专家"},
            ],
        },
    })
    context.messages.append({
        "role": "assistant",
        "message_id": "msg_candidate_form",
        "content": "请补充当前家庭情况。",
        "blocks": [{"type": "fact_form", "payload": {"form_id": "candidate_family_context", "fields": []}}],
    })
    context.skill_states["skill_runtime"] = {
        "active_skill_id": "family_skill",
        "skill_facts": {"family_skill": {"_pending_questionnaire": {"form_id": "candidate_family_context"}}},
    }

    message = WorkbenchService._revision_test_input_message(
        context,
        "孩子最近不愿意写作业，怎么沟通？",
        None,
        expert_selection={"target_expert_id": "family_expert"},
    )

    assert message == "孩子最近不愿意写作业，怎么沟通？"
    assert context.session_meta["active_expert_id"] == "family_expert"
    assert context.session_meta["expert_selection_source"] == "candidate_manual_at"
    assert context.session_meta["team_member_switch"] == {
        "source": "toolbar",
        "team_id": "team_a",
        "from_expert_id": "coordinator_expert",
        "target_expert_id": "family_expert",
        "mention_name": "家庭教育专家",
        "content": "孩子最近不愿意写作业，怎么沟通？",
        "visible_user_message": "@家庭教育专家 孩子最近不愿意写作业，怎么沟通？",
    }
    assert context.messages[0]["interaction_states"]["fact_form:candidate_family_context"]["status"] == "expired"
    assert "_pending_questionnaire" not in context.skill_states["skill_runtime"]["skill_facts"]["family_skill"]
    abandoned = [event for event in context.event_trace if event["event_type"] == "form_abandoned"]
    assert abandoned and abandoned[-1]["payload"]["reason"] == "candidate_manual_expert_selection"
    assert context.user_id == "workbench-candidate-dbg_at"
    assert context.profile_id == "anonymous-candidate"


def test_candidate_manual_at_expert_rejects_an_expert_outside_the_candidate_team():
    context = SessionContext()
    context.session_meta.update({
        "expert_team_id": "team_a",
        "configuration_snapshot": {
            "entries": [{
                "object_type": "expert_team",
                "object_key": "team_a",
                "payload": {},
                "dependency_locks": [{"object_type": "expert", "object_key": "family_expert"}],
            }],
        },
    })

    with pytest.raises(WorkbenchError, match="不属于当前候选专家团"):
        WorkbenchService._revision_test_input_message(
            context,
            "测试问题",
            None,
            expert_selection={"target_expert_id": "outside_expert"},
        )


def test_candidate_team_handoff_turn_persists_the_formal_message_envelope():
    preview_service = _preview_service()
    actor_id = _actor(preview_service)
    team = preview_service.create_object(
        object_type="expert_team", object_key="team_a", name="测试专家团", actor_id=actor_id,
    )
    revision = preview_service.save_revision(
        team["object_id"],
        base_revision_id=None,
        payload={"rules_markdown": "需要时建议转交", "coordinator_expert_id": ""},
        dependency_locks=[],
        assets=[],
        actor_id=actor_id,
    )
    preview_service.orchestrator = _TeamHandoffPreviewOrchestrator()
    session = preview_service.create_revision_test_session(revision["revision_id"], actor_id=actor_id)

    result = preview_service.run_revision_test_turn(
        session["debug_session_id"], user_message="孩子总是顶嘴", actor_id=actor_id,
    )

    assistant = result["debug_session"]["transcript"][-1]
    assert assistant["message_id"]
    assert assistant["team_handoff"]["handoff_id"] == "handoff_candidate_1"
    assert assistant["interaction_states"]["team_handoff"]["status"] == "active"
    assert assistant["blocks"][-1]["type"] == "team_handoff"
    assert result["assistant_message_record"]["message_id"] == assistant["message_id"]


def test_revision_test_supports_experts_and_teams_and_formal_chat_requires_published_team():
    preview_service = _preview_service()
    # Publishing updates runtime registries in production.  Build the fixture
    # releases without those registries, then attach the lightweight executor
    # used only by the candidate-test assertions below.
    preview_service.orchestrator = None
    actor_id = _actor(preview_service)
    _skill, _skill_revision, skill_release = _publish_skill(preview_service, actor_id, key="test_skill")
    expert_releases = []
    for key in ("expert_one", "expert_two"):
        expert = preview_service.create_object(object_type="expert", object_key=key, name=key, actor_id=actor_id)
        revision = preview_service.save_revision(
            expert["object_id"], base_revision_id=None,
            payload={"rules_markdown": "专家候选规则", "brief": f"{key} 的候选摘要", "budget": {}},
            dependency_locks=[{"release_id": skill_release["release_id"]}], assets=[], actor_id=actor_id,
        )
        debug = preview_service.create_debug_session(revision["revision_id"], baseline_release_id=None, actor_id=actor_id)
        preview_service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
        expert_releases.append(preview_service.publish_revision(
            revision["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True,
            confirmation_notes="通过", actor_id=actor_id,
        ))

    team = preview_service.create_object(object_type="expert_team", object_key="test_team", name="测试专家团", actor_id=actor_id)
    team_revision = preview_service.save_revision(
        team["object_id"], base_revision_id=None,
        payload={"rules_markdown": "专家团候选规则", "brief": "候选专家团摘要", "coordinator_expert_id": expert_releases[0]["object_id"]},
        dependency_locks=[{"release_id": item["release_id"]} for item in expert_releases], assets=[], actor_id=actor_id,
    )
    preview_service.orchestrator = _PreviewOrchestrator()
    for key, release in zip(("expert_one", "expert_two"), expert_releases, strict=True):
        expert_session = preview_service.create_revision_test_session(release["revision_id"], actor_id=actor_id)
        expert_turn = preview_service.run_revision_test_turn(
            expert_session["debug_session_id"], user_message="测试专家", actor_id=actor_id,
        )
        assert expert_turn["assistant_message"] == f"expert:{key} :: 测试专家"
    team_session = preview_service.create_revision_test_session(team_revision["revision_id"], actor_id=actor_id)
    team_turn = preview_service.run_revision_test_turn(
        team_session["debug_session_id"], user_message="测试专家团", actor_id=actor_id,
    )
    assert team_turn["assistant_message"] == "expert_team:test_team :: 测试专家团"
    with pytest.raises(WorkbenchError, match="先发布"):
        preview_service.create_formal_team_chat_session(team_revision["revision_id"], actor_id=actor_id)

    preview_service.orchestrator = None
    debug = preview_service.create_debug_session(team_revision["revision_id"], baseline_release_id=None, actor_id=actor_id)
    preview_service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    team_release = preview_service.publish_revision(
        team_revision["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True,
        confirmation_notes="通过", actor_id=actor_id,
    )
    preview_service.orchestrator = _PreviewOrchestrator()
    formal = preview_service.create_formal_team_chat_session(team_revision["revision_id"], actor_id=actor_id)
    assert formal["baseline_release_id"] == team_release["release_id"]
    generic_formal = preview_service.create_formal_chat_session(
        object_id=expert_releases[0]["object_id"],
        revision_id=expert_releases[0]["revision_id"],
        release_id=expert_releases[0]["release_id"],
        actor_id=actor_id,
    )
    assert generic_formal["baseline_release_id"] == expert_releases[0]["release_id"]
    with pytest.raises(WorkbenchError, match="修订不属于"):
        preview_service.create_formal_chat_session(
            object_id=team["object_id"],
            revision_id=expert_releases[0]["revision_id"],
            release_id=expert_releases[0]["release_id"],
            actor_id=actor_id,
        )


def test_debug_and_evaluation_history_filter_by_exact_object_and_revision(service: WorkbenchService):
    actor_id = _actor(service)
    obj = service.create_object(object_type="skill", object_key="history_target", name="历史目标", actor_id=actor_id)
    revision = service.save_revision(
        obj["object_id"], base_revision_id=None,
        payload={"prompt_markdown": "历史测试", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[], assets=[], actor_id=actor_id,
    )
    debug = service.create_revision_test_session(revision["revision_id"], actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="手动通过", actor_id=actor_id)
    suite = service.create_evaluation_suite(
        object_id=obj["object_id"], name="历史用例", cases=[{"case_id": "case_1", "input": "测试"}], actor_id=actor_id,
    )
    run = service.create_evaluation_run(
        suite_id=suite["suite_id"], revision_id=revision["revision_id"], baseline_release_id=None,
        results=[{"case_id": "case_1", "status": "completed"}], actor_id=actor_id,
    )
    service.complete_evaluation_run(
        run["run_id"], results=run["results"], manual_result="accepted", manual_notes="通过", actor_id=actor_id,
    )

    debug_history = service.list_debug_sessions(object_id=obj["object_id"], revision_id=revision["revision_id"])
    evaluation_history = service.list_evaluation_runs(object_id=obj["object_id"], revision_id=revision["revision_id"])
    assert debug_history[0]["object_name"] == "历史目标"
    assert debug_history[0]["revision_no"] == 1
    assert evaluation_history[0]["suite_name"] == "历史用例"
    assert evaluation_history[0]["manual_result"] == "accepted"


def test_candidate_snapshot_binds_selected_or_disabled_soul_revision(service: WorkbenchService):
    actor_id = _actor(service)
    soul = service.save_soul_revision("回答应当温和。", actor_id=actor_id)
    obj = service.create_object(object_type="skill", object_key="soul_target", name="Soul 测试", actor_id=actor_id)
    revision = service.save_revision(
        obj["object_id"], base_revision_id=None,
        payload={"prompt_markdown": "Skill 规则", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[], assets=[], actor_id=actor_id,
    )
    with_soul = service.create_revision_test_session(
        revision["revision_id"], soul_revision_id=soul["soul_revision_id"], actor_id=actor_id,
    )
    without_soul = service.create_revision_test_session(revision["revision_id"], actor_id=actor_id)

    assert with_soul["snapshot"]["soul_context"]["content_hash"] == soul["content_hash"]
    assert without_soul["snapshot"]["soul_context"]["enabled"] is False


def test_standard_agent_skill_zip_preview_and_manual_commit(service: WorkbenchService):
    actor_id = _actor(service)
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", "---\nname: 导入测试\nskill_id: imported_demo\ndescription: 标准协议导入\n---\n# 导入规则\n")
        archive.writestr("runtime_contract.json", json.dumps({"questionnaire": {"fields": [{"fact_key": "grade", "label": "年级", "input_type": "single_select", "options": [{"label": "高一", "value": "高一"}]}]}}, ensure_ascii=False))
        archive.writestr("references/guide.md", "# 引用资料")
    preview = service.preview_standard_skill_package(output.getvalue(), actor_id=actor_id)

    assert preview["draft"]["object_type"] == "skill"
    assert preview["draft"]["object_key"] == "imported_demo"
    assert preview["form_preview"][0]["fact_key"] == "grade"
    saved = service.commit_standard_skill_conversion(preview["draft"], actor_id=actor_id)

    detail = service.get_object(saved["object_id"])
    assert detail["revisions"][0]["revision_no"] == 1
    assert service.list_revision_assets(saved["revision"]["revision_id"])[0]["relative_path"] == "references/guide.md"


def test_standard_expert_package_preview_reads_brief_from_agent_yaml(service: WorkbenchService):
    actor_id = _actor(service)
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("AGENT.md", "---\nname: 导入专家\nagent_id: imported_expert\ndesc: 学习规划支持\n---\n# 专家规则\n")
        archive.writestr(
            "agent.yaml",
            "schema_version: 1\nname: 导入专家\nbrief: 面向学生的学习规划支持\n",
        )

    preview = service.preview_standard_skill_package(output.getvalue(), actor_id=actor_id)

    assert preview["draft"]["object_type"] == "expert"
    assert preview["draft"]["object_key"] == "imported_expert"
    assert preview["draft"]["name"] == "导入专家"
    assert preview["draft"]["payload"]["brief"] == "面向学生的学习规划支持"


def test_standard_expert_team_preview_reads_expert_team_id_from_team_frontmatter(service: WorkbenchService):
    actor_id = _actor(service)
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "TEAM.md",
            "---\nname: 学生成长专家团\nexpert_team_id: student_growth_expert_team\ndesc: 覆盖学生成长相关问题\n---\n# 团队规则\n",
        )
        archive.writestr(
            "team.yaml",
            "schema_version: 1\nname: 学生成长专家团\nbrief: 覆盖学习、升学与职业成长问题\n",
        )

    preview = service.preview_standard_skill_package(output.getvalue(), actor_id=actor_id)

    assert preview["draft"]["object_type"] == "expert_team"
    assert preview["draft"]["object_key"] == "student_growth_expert_team"
    assert preview["draft"]["payload"]["brief"] == "覆盖学习、升学与职业成长问题"


def test_standard_zip_strips_macos_wrapper_and_maps_questions_asset(service: WorkbenchService):
    actor_id = _actor(service)
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("wrapped/SKILL.md", "---\nname: DISC\nslug: disc_demo\n---\n# DISC\n")
        archive.writestr("wrapped/references/rules.md", "规则")
        archive.writestr("wrapped/scripts/score.py", "def main(payload):\n    return payload\n")
        archive.writestr("wrapped/assets/questions.json", json.dumps([{"id": 1, "title": "问题", "options": [{"text": "选项 A", "dimension": "A"}]}], ensure_ascii=False))
        archive.writestr("__MACOSX/wrapped/._SKILL.md", b"metadata")
    preview = service.preview_standard_skill_package(output.getvalue(), actor_id=actor_id)

    paths = {item["relative_path"] for item in preview["draft"]["assets"]}
    assert paths == {"references/rules.md", "scripts/score.py", "assets/questions.json"}
    assert preview["form_preview"][0]["input_type"] == "single_select"


def test_standard_skill_import_preserves_assets_scripts_and_questionnaire_path(service: WorkbenchService):
    actor_id = _actor(service)
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: 旅行推荐\nskill_id: travel_demo\nquestionnaire:\n  enabled: true\n  config_path: assets/questionnaire.json\n---\n# 旅行推荐\n",
        )
        archive.writestr("assets/questionnaire.json", json.dumps({"schema_version": 1, "questions": [{"id": "age", "label": "年龄", "input_type": "integer"}]}, ensure_ascii=False))
        archive.writestr("scripts/score.py", "def main(payload):\n    return payload\n")
        archive.writestr("references/cities.md", "城市资料")

    preview = service.preview_standard_skill_package(output.getvalue(), actor_id=actor_id)
    paths = {item["relative_path"] for item in preview["draft"]["assets"]}
    assert paths == {"assets/questionnaire.json", "scripts/score.py", "references/cities.md"}
    assert preview["draft"]["payload"]["configuration"]["questionnaire"]["config_path"] == "assets/questionnaire.json"
    assert "questionnaire" not in preview["draft"]["payload"]["runtime_contract"]


def test_questionnaire_metadata_is_written_back_to_skill_frontmatter() -> None:
    markdown = "---\nname: 旅行推荐\n---\n# 旅行推荐\n"

    rendered = skill_markdown_with_metadata(markdown, {
        "questionnaire": {"enabled": True, "config_path": "assets/questionnaire.json"},
    })

    assert "name: 旅行推荐" in rendered
    assert "questionnaire:" in rendered
    assert "config_path: assets/questionnaire.json" in rendered
    assert rendered.endswith("# 旅行推荐\n")


def test_standard_skill_import_preserves_safe_root_data_files(service: WorkbenchService):
    actor_id = _actor(service)
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "SKILL.md",
            "---\nname: 猜职业\nskill_id: guess_profession_contract\n---\n# 猜职业\n",
        )
        archive.writestr(
            "scripts/pick_profession.py",
            "import json, sys\npayload = json.load(sys.stdin)\nprint(json.dumps({'ok': True}))\n",
        )
        archive.writestr("professions.json", json.dumps({"professions": ["教师"]}, ensure_ascii=False))

    preview = service.preview_standard_skill_package(output.getvalue(), actor_id=actor_id)

    paths = {item["relative_path"] for item in preview["draft"]["assets"]}
    assert paths == {"scripts/pick_profession.py", "professions.json"}
    assert preview["warnings"] == []


def test_unsafe_python_script_is_saved_for_correction_but_cannot_publish(service: WorkbenchService):
    actor_id = _actor(service)
    obj = service.create_object(object_type="skill", object_key="unsafe_skill", name="待修复 Skill", actor_id=actor_id)
    revision = service.save_revision(
        obj["object_id"], base_revision_id=None,
        payload={"prompt_markdown": "测试", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[],
        assets=[_text_asset("scripts/run.py", "import subprocess\n", "text/x-python")],
        actor_id=actor_id,
    )
    debug = service.create_debug_session(revision["revision_id"], baseline_release_id=None, actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="待修复", actor_id=actor_id)

    assert revision["validation"]["valid"] is False
    assert any("blocked module" in error for error in revision["validation"]["errors"])
    with pytest.raises(WorkbenchError, match="校验未通过"):
        service.publish_revision(
            revision["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True,
            confirmation_notes="不应发布", actor_id=actor_id,
        )


def test_export_import_activation_and_rollback(service: WorkbenchService):
    actor_id = _actor(service)
    obj, first_revision, first_release = _publish_skill(service, actor_id)
    first_package, first_manifest = service.export_release(first_release["release_id"], actor_id=actor_id)
    first_deployment = service.import_package(first_package, environment="prod", actor_id=actor_id)
    active_first = service.activate_deployment(first_deployment["deployment_id"], actor_id=actor_id)
    assert active_first["status"] == "active"
    assert first_manifest["root"]["release_id"] == first_release["release_id"]

    second_revision = service.save_revision(
        obj["object_id"], base_revision_id=first_revision["revision_id"],
        payload={"prompt_markdown": "第二版", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[], assets=[], actor_id=actor_id,
    )
    debug = service.create_debug_session(second_revision["revision_id"], baseline_release_id=first_release["release_id"], actor_id=actor_id)
    service.complete_debug_session(debug["debug_session_id"], conclusion="通过", actor_id=actor_id)
    second_release = service.publish_revision(
        second_revision["revision_id"], evidence_id=debug["debug_session_id"], manual_confirmation=True,
        confirmation_notes="通过", actor_id=actor_id,
    )
    second_package, _ = service.export_release(second_release["release_id"], actor_id=actor_id)
    second_deployment = service.import_package(second_package, environment="prod", actor_id=actor_id)
    active_second = service.activate_deployment(second_deployment["deployment_id"], actor_id=actor_id)
    assert active_second["previous_deployment_id"] == first_deployment["deployment_id"]
    restored = service.rollback_deployment(second_deployment["deployment_id"], actor_id=actor_id)
    assert restored["deployment_id"] == first_deployment["deployment_id"]
    assert restored["status"] == "active"


def test_import_rejects_zip_traversal_but_allows_different_kernel_fingerprint(service: WorkbenchService):
    actor_id = _actor(service)
    with io.BytesIO() as output:
        with ZipFile(output, "w", ZIP_DEFLATED) as archive:
            archive.writestr("../escape.txt", "bad")
        with pytest.raises(WorkbenchError, match="路径"):
            service.import_package(output.getvalue(), environment="prod", actor_id=actor_id)

    _, _, release = _publish_skill(service, actor_id)
    package, _ = service.export_release(release["release_id"], actor_id=actor_id)
    files = service._read_zip(package)
    manifest = json.loads(files["manifest.json"])
    manifest["kernel_fingerprint"] = "different"
    manifest.pop("manifest_hash")
    manifest["manifest_hash"] = _hash(manifest)
    files["manifest.json"] = json.dumps(manifest).encode()
    mutated = service._zip(files)
    imported = service.import_package(mutated, environment="prod", actor_id=actor_id)
    assert imported["manifest"]["kernel_fingerprint"] == "different"


def test_import_rejects_unknown_capability_and_undeclared_file(service: WorkbenchService):
    actor_id = _actor(service)
    _, _, release = _publish_skill(service, actor_id)
    package, _ = service.export_release(release["release_id"], actor_id=actor_id)

    files = service._read_zip(package)
    manifest = json.loads(files["manifest.json"])
    object_item = manifest["objects"][0]
    object_path = next(item["path"] for item in object_item["files"] if item["path"].endswith("/object.json"))
    declaration = json.loads(files[object_path])
    declaration["payload"]["capability_ids"] = ["unknown:capability"]
    files[object_path] = json.dumps(declaration, ensure_ascii=False).encode()
    file_item = next(item for item in object_item["files"] if item["path"] == object_path)
    file_item["sha256"] = hashlib.sha256(files[object_path]).hexdigest()
    file_item["size_bytes"] = len(files[object_path])
    manifest.pop("manifest_hash")
    manifest["manifest_hash"] = _hash(manifest)
    files["manifest.json"] = json.dumps(manifest).encode()
    with pytest.raises(WorkbenchError, match="未知底层能力"):
        service.import_package(service._zip(files), environment="prod", actor_id=actor_id)

    clean_files = service._read_zip(package)
    clean_files["extra.txt"] = b"not declared"
    with pytest.raises(WorkbenchError, match="未在 manifest"):
        service.import_package(service._zip(clean_files), environment="prod", actor_id=actor_id)


def test_active_deployment_snapshot_contains_declarative_payload(service: WorkbenchService):
    actor_id = _actor(service)
    _, _, skill_a = _publish_skill(service, actor_id, key="frozen_team_skill_a", prompt="固定快照 Prompt")
    _, _, skill_b = _publish_skill(service, actor_id, key="frozen_team_skill_b")
    _, _, expert_a = _publish_expert(service, actor_id, key="frozen_team_expert_a", skill_release=skill_a)
    _, _, expert_b = _publish_expert(service, actor_id, key="frozen_team_expert_b", skill_release=skill_b)
    obj, _, release = _publish_team(service, actor_id, key="frozen_team", expert_releases=[expert_a, expert_b], rules="固定快照 Prompt")
    package, _ = service.export_release(release["release_id"], actor_id=actor_id)
    deployment = service.import_package(package, environment="prod", actor_id=actor_id)
    service.activate_deployment(deployment["deployment_id"], actor_id=actor_id)

    snapshot = service.active_deployment_snapshot("prod")

    assert snapshot is not None
    assert snapshot["root_release_id"] == release["release_id"]
    deployed_team = next(item for item in snapshot["entries"] if item["object_type"] == "expert_team")
    assert deployed_team["payload"]["rules_markdown"] == "固定快照 Prompt"
    assert deployed_team["release_no"] == 1
    assert deployed_team["revision_no"] == 1
    assert deployed_team["revision_created_by"] == actor_id
    assert snapshot["deployment"]["deployment_id"] == deployment["deployment_id"]
    assert snapshot["deployment"]["activated_by"] == actor_id
    service.delete_object(obj["object_id"], confirmation_name=obj["name"], actor_id=actor_id)
    frozen_snapshot = service.active_deployment_snapshot("prod")
    assert frozen_snapshot is not None
    assert next(item for item in frozen_snapshot["entries"] if item["object_type"] == "expert_team")["payload"]["rules_markdown"] == "固定快照 Prompt"


def test_global_expert_team_deployment_can_switch_deactivate_and_restore(service: WorkbenchService):
    actor_id = _actor(service)
    _, _, skill_a = _publish_skill(service, actor_id, key="deploy_team_skill_a")
    _, _, skill_b = _publish_skill(service, actor_id, key="deploy_team_skill_b")
    _, _, expert_a = _publish_expert(service, actor_id, key="deploy_team_expert_a", skill_release=skill_a)
    _, _, expert_b = _publish_expert(service, actor_id, key="deploy_team_expert_b", skill_release=skill_b)
    team_one, _, team_one_release = _publish_team(service, actor_id, key="global_team_one", expert_releases=[expert_a, expert_b])
    team_two, _, team_two_release = _publish_team(service, actor_id, key="global_team_two", expert_releases=[expert_a, expert_b])

    package_one, _ = service.export_release(team_one_release["release_id"], actor_id=actor_id)
    deployment_one = service.import_package(package_one, environment="prod", actor_id=actor_id)
    active_one = service.activate_deployment(deployment_one["deployment_id"], actor_id=actor_id)
    assert active_one["expert_team_id"] == "global_team_one"
    assert service.active_deployment_snapshot("prod")["root"]["object_key"] == "global_team_one"

    package_two, _ = service.export_release(team_two_release["release_id"], actor_id=actor_id)
    deployment_two = service.import_package(package_two, environment="prod", actor_id=actor_id)
    active_two = service.activate_deployment(deployment_two["deployment_id"], actor_id=actor_id)
    assert active_two["previous_deployment_id"] == deployment_one["deployment_id"]
    assert next(item for item in service.list_deployments() if item["deployment_id"] == deployment_one["deployment_id"])["status"] == "superseded"
    assert service.active_deployment_snapshot("prod")["root"]["object_key"] == "global_team_two"

    with pytest.raises(WorkbenchError) as mismatch:
        service.deactivate_deployment(deployment_two["deployment_id"], expert_team_id="wrong_team", actor_id=actor_id)
    assert mismatch.value.code == "DEPLOYMENT_CONFIRMATION_MISMATCH"
    deactivated = service.deactivate_deployment(
        deployment_two["deployment_id"], expert_team_id="global_team_two", actor_id=actor_id,
    )
    assert deactivated["status"] == "deactivated"
    assert service.active_deployment_snapshot("prod") is None

    restored = service.restore_deployment(deployment_one["deployment_id"], actor_id=actor_id)
    assert restored["status"] == "active"
    assert service.active_deployment_snapshot("prod")["root"]["object_key"] == "global_team_one"
    actions = {item["action"] for item in service.list_audit_events(limit=20)}
    assert {"deployment.activated", "deployment.deactivated", "deployment.restored"} <= actions


def test_debug_session_exposes_the_bound_candidate_snapshot(service: WorkbenchService):
    actor_id = _actor(service)
    obj = service.create_object(object_type="skill", object_key="candidate", name="候选技能", actor_id=actor_id)
    revision = service.save_revision(
        obj["object_id"], base_revision_id=None,
        payload={"prompt_markdown": "仅用于候选调试", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[], assets=[], actor_id=actor_id,
    )
    debug = service.create_debug_session(revision["revision_id"], baseline_release_id=None, actor_id=actor_id)

    snapshot = service.debug_configuration_snapshot(debug["debug_session_id"])

    assert snapshot["root"]["revision_id"] == revision["revision_id"]
    assert snapshot["entries"][0]["payload"]["prompt_markdown"] == "仅用于候选调试"


def test_configuration_snapshot_stays_global_across_profile_branches():
    context = SessionContext(session_id="snapshot-session", profile_id="p1", profile_name="孩子一")
    snapshot = {"deployment_id": "deploy_1", "entries": []}
    context.session_meta["configuration_snapshot"] = snapshot

    context.activate_profile_branch("p2", profile_name="孩子二")
    context.activate_profile_branch("p1", profile_name="孩子一")

    assert context.session_meta["configuration_snapshot"] == snapshot


def test_skill_prompt_overlay_is_session_scoped():
    bundle = SimpleNamespace(
        contract=SimpleNamespace(skill_id="skill_a"),
        root_name="skill_a",
        metadata={},
        _skill_markdown="filesystem prompt",
        _skill_markdown_loader=None,
    )
    context = SessionContext()
    context.session_meta["configuration_snapshot"] = {
        "entries": [{
            "object_type": "skill",
            "object_key": "skill_a",
            "payload": {
                "prompt_markdown": "snapshot prompt",
                "runtime_contract": {"questionnaire": {"enabled": True, "fields": []}},
            },
        }]
    }

    configured = MainPlannerOrchestrator._configured_bundle(context, bundle)

    assert configured._skill_markdown == "snapshot prompt"
    assert bundle._skill_markdown == "filesystem prompt"
    assert configured.metadata["questionnaire"]["enabled"] is True
