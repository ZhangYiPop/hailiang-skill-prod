from hailiang_skills.api.configuration_sync import synchronize_configuration
from hailiang_skills.core.context import SessionContext
from hailiang_skills.schemas.facts import FactRecord


def _snapshot(deployment_id: str, package_hash: str, *, team_id: str = "team_new"):
    return {
        "deployment_id": deployment_id,
        "package_hash": package_hash,
        "entries": [{
            "object_type": "expert_team",
            "object_key": team_id,
            "payload": {"coordinator_expert_id": "obj_coordinator"},
            "dependency_locks": [
                {"object_id": "obj_coordinator", "object_key": "coordinator"},
                {"object_id": "obj_other", "object_key": "other"},
            ],
        }],
    }


def test_configuration_sync_preserves_history_and_facts_but_expires_live_state():
    old = _snapshot("dep_old", "hash_old", team_id="team_old")
    context = SessionContext(session_id="session", user_id="user")
    context.session_meta["configuration_snapshot"] = old
    context.session_meta["expert_team_id"] = "team_old"
    context.session_meta["active_expert_id"] = "removed_expert"
    context.messages = [{
        "role": "assistant",
        "content": "旧表单",
        "message_id": "message_1",
        "interaction_states": {"fact_form:form_1": {"status": "active"}},
    }]
    context.profile_facts.facts["grade"] = FactRecord(value="八年级", source_skill="user-confirmed")
    context.skill_states = {"skill_runtime": {"pending_form": {"id": "form_1"}}}
    context.candidate_paths = [{"id": "old"}]

    notice = synchronize_configuration(context, _snapshot("dep_new", "hash_new"))

    assert notice["code"] == "CONFIGURATION_UPDATED"
    assert context.messages[0]["content"] == "旧表单"
    assert context.messages[0]["interaction_states"]["fact_form:form_1"]["status"] == "expired"
    assert context.profile_facts.get_value("grade") == "八年级"
    assert context.skill_states == {}
    assert context.candidate_paths == []
    assert context.session_meta["expert_team_id"] == "team_new"
    assert context.session_meta["active_expert_id"] == "coordinator"


def test_deactivation_moves_existing_session_to_general_chat():
    context = SessionContext(session_id="session", user_id="user")
    context.session_meta["configuration_snapshot"] = _snapshot("dep_old", "hash_old")
    context.session_meta["expert_team_id"] = "team_new"
    context.session_meta["active_expert_id"] = "other"

    notice = synchronize_configuration(context, None)

    assert notice["deployment_id"] is None
    assert "configuration_snapshot" not in context.session_meta
    assert context.session_meta.get("expert_team_id") is None
    assert context.interaction_state["active_skill"] == "general_chat"
