from __future__ import annotations

import io
import hashlib
import base64
import json
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from hailiang_skills.storage.database import Base, WorkbenchDebugSessionRow
from hailiang_skills.workbench.service import WorkbenchConflict, WorkbenchError, WorkbenchService, _hash
from hailiang_skills.workbench.runtime_overlay import configured_skill_bundle
from hailiang_skills.api.routes.workbench import build_workbench_router
from hailiang_skills.core.context import SessionContext
from hailiang_skills.runtime_bridge.main_planner import MainPlannerOrchestrator


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
        selected = "family_skill" if "另一个技能" in message else "disc_skill"
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


class _SemanticSwitchChooser:
    def select_candidate_skill_switch(self, message, _context, *, current_skill_id):
        if current_skill_id == "disc_skill" and "亲子沟通" in message:
            return "family_skill"
        return None


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


def test_expert_and_team_lock_exact_published_dependencies(service: WorkbenchService):
    actor_id = _actor(service)
    _, _, skill_a = _publish_skill(service, actor_id, key="skill_a")
    _, _, skill_b = _publish_skill(service, actor_id, key="skill_b")

    expert_releases = []
    for key, skill_release in (("expert_a", skill_a), ("expert_b", skill_b)):
        expert = service.create_object(object_type="expert", object_key=key, name=key, actor_id=actor_id)
        revision = service.save_revision(
            expert["object_id"], base_revision_id=None,
            payload={"rules_markdown": "专家规则", "budget": {"max_iters": 4, "max_skill_calls": 3}},
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
        payload={"rules_markdown": "团队规则", "coordinator_expert_id": expert_releases[0]["object_id"]},
        dependency_locks=[{"release_id": item["release_id"]} for item in expert_releases], assets=[], actor_id=actor_id,
    )

    assert team_revision["validation"]["valid"] is True
    assert [item["release_id"] for item in team_revision["dependency_locks"]] == [item["release_id"] for item in expert_releases]
    snapshot = service.create_debug_session(team_revision["revision_id"], baseline_release_id=None, actor_id=actor_id)["snapshot"]
    assert {entry["object_type"] for entry in snapshot["entries"]} == {"skill", "expert", "expert_team"}


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
        "permanent": True,
    }
    assert service.list_objects(include_archived=True) == []
    assert any(event["action"] == "object.deleted" for event in service.list_audit_events())
    with pytest.raises(WorkbenchConflict, match="曾被永久删除"):
        service.create_object(object_type="skill", object_key="delete_me", name="试图恢复", actor_id=actor_id)


def test_object_delete_is_blocked_when_an_upper_revision_depends_on_it(service: WorkbenchService):
    actor_id = _actor(service)
    skill_obj, _revision, skill_release = _publish_skill(service, actor_id, key="depended_skill")
    expert = service.create_object(object_type="expert", object_key="dependent_expert", name="依赖专家", actor_id=actor_id)
    service.save_revision(
        expert["object_id"], base_revision_id=None,
        payload={"rules_markdown": "使用依赖能力", "budget": {}},
        dependency_locks=[{"release_id": skill_release["release_id"]}], assets=[], actor_id=actor_id,
    )

    with pytest.raises(WorkbenchConflict) as exc_info:
        service.delete_object(skill_obj["object_id"], confirmation_name=skill_obj["name"], actor_id=actor_id)

    assert exc_info.value.code == "OBJECT_DELETE_BLOCKED"
    assert exc_info.value.details["blockers"][0]["object_key"] == "dependent_expert"
    assert service.get_object(skill_obj["object_id"])["name"] == skill_obj["name"]


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


def test_skill_revision_preserves_editable_reference_and_safe_python_script(service: WorkbenchService):
    actor_id = _actor(service)
    obj = service.create_object(object_type="skill", object_key="editable_skill", name="可编辑 Skill", actor_id=actor_id)
    revision = service.save_revision(
        obj["object_id"], base_revision_id=None,
        payload={"prompt_markdown": "按资料回答", "runtime_contract": {}, "capability_ids": []},
        dependency_locks=[],
        assets=[
            _text_asset("references/rules.md", "# 规则\n仅使用已知事实。", "text/markdown"),
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
    assert {item["relative_path"] for item in assets} == {"references/rules.md", "scripts/score.py"}
    assert snapshot["entries"][0]["runtime_root"]
    assert "content_base64" not in snapshot["entries"][0]["files"][0]
    assert imported["status"] == "staged"
    assert any(path.endswith("/scripts/score.py") for path in service._read_zip(package))
    bundle = SimpleNamespace(
        _skill_markdown="filesystem", _skill_markdown_loader=None,
        root_dir=None, skill_file=None, references={}, scripts={}, _references={}, _references_loader=None, _scripts={},
    )
    configured = configured_skill_bundle(bundle, snapshot["entries"][0])
    assert configured._references["references/rules.md"].startswith("# 规则")
    assert configured._scripts["scripts/score.py"].is_file()


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
        payload={"rules_markdown": "根据用户意图选择锁定 Skill", "budget": {}},
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
    assert any(event["event_type"] == "candidate_skill_redispatch_requested" for event in latest_events)
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
        payload={"rules_markdown": "根据用户意图选择锁定 Skill", "budget": {}},
        dependency_locks=[{"release_id": disc_release["release_id"]}, {"release_id": family_release["release_id"]}],
        assets=[], actor_id=actor_id,
    )
    orchestrator = _RedispatchPreviewOrchestrator()
    orchestrator.expert_runtime = _SemanticSwitchChooser()
    preview_service.orchestrator = orchestrator
    session = preview_service.create_revision_test_session(expert_revision["revision_id"], actor_id=actor_id)
    preview_service.run_revision_test_turn(session["debug_session_id"], user_message="做性格测试", actor_id=actor_id)
    switched = preview_service.run_revision_test_turn(
        session["debug_session_id"], user_message="我现在更想解决亲子沟通冲突", actor_id=actor_id,
    )

    assert "家庭关系候选规则" in switched["assistant_message"]
    latest_events = switched["debug_session"]["trace"][-1]["events"]
    assert any(event["event_type"] == "candidate_skill_semantic_redispatch" for event in latest_events)


def test_candidate_team_handoff_is_persisted_as_an_interactive_block_and_can_be_confirmed():
    context = SessionContext()
    context.session_meta.update({
        "expert_team_id": "team_a",
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
            "team_handoff": {
                "handoff_id": "handoff_1",
                "status": "active",
                "team_id": "team_a",
                "candidates": [{"expert_id": "family_expert", "mention_name": "家庭教育专家"}],
                "reason": "更适合亲子沟通问题",
            },
        },
    ]

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
            payload={"rules_markdown": "专家候选规则", "budget": {}},
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
        payload={"rules_markdown": "专家团候选规则", "coordinator_expert_id": expert_releases[0]["object_id"]},
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
    assert paths == {"references/rules.md", "scripts/score.py", "references/assets/questions.json"}
    assert preview["form_preview"][0]["input_type"] == "single_select"


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


def test_import_rejects_zip_traversal_and_kernel_mismatch(service: WorkbenchService):
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
    with pytest.raises(WorkbenchError, match="内核版本"):
        service.import_package(mutated, environment="prod", actor_id=actor_id)


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
    _, _, release = _publish_skill(service, actor_id, prompt="固定快照 Prompt")
    package, _ = service.export_release(release["release_id"], actor_id=actor_id)
    deployment = service.import_package(package, environment="prod", actor_id=actor_id)
    service.activate_deployment(deployment["deployment_id"], actor_id=actor_id)

    snapshot = service.active_deployment_snapshot("prod")

    assert snapshot is not None
    assert snapshot["root_release_id"] == release["release_id"]
    assert snapshot["entries"][0]["payload"]["prompt_markdown"] == "固定快照 Prompt"


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
