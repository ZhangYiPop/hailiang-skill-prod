from __future__ import annotations

import json

from hailiang_skills.api.session_lifecycle import _initialize_general_chat_state, _normalize_legacy_default_skill
from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.skill_display import build_skill_catalog
from hailiang_skills.core.skill_lifecycle import build_finalized_payload, build_skill_invitation
from hailiang_skills.core.sse_protocol import SseEnvelopeBuilder
from hailiang_skills.core.skill_ids import CAREER_PLAN_SKILL_ID, GENERAL_CHAT_SKILL_ID, LEGACY_MAIN_PLANNER_SKILL_ID
from hailiang_skills.runtime_bridge.main_planner import PROJECT_RUNTIME_SKILLS_ROOT
from hailiang_skills.skill_runtime.intent_router import IntentRouter
from hailiang_skills.skill_runtime.models import SessionState
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry


class _SuggestionClient:
    def __init__(self, response: str, *, fail: bool = False) -> None:
        self.response = response
        self.fail = fail
        self.calls: list[str] = []

    def complete(self, messages, *, logger=None) -> str:
        del logger
        self.calls.append(str(messages[-1].content))
        if self.fail:
            raise RuntimeError("route decision unavailable")
        return self.response


def _registry():
    return load_local_skill_registry(PROJECT_RUNTIME_SKILLS_ROOT)


def _general_chat_context(*, grade: str = "高一") -> SessionContext:
    context = SessionContext(session_id="sess_general", user_id="u1", profile_id="p1")
    _initialize_general_chat_state(context)
    context.update_fact("grade", grade, source_skill="test")
    context.skill_states["skill_runtime"]["global_facts"] = {"grade": grade}
    context.skill_states["main_planner"]["intent_route"] = {
        "candidate_skills": [
            {
                "target_skill_id": "mock_admission",
                "confidence": 0.99,
                "reason": "分数和院校匹配问题",
                "scene_name": "模拟升学",
            }
        ]
    }
    return context


def test_career_plan_entity_is_the_public_selectable_replacement_for_main_planner() -> None:
    registry = _registry()

    assert registry.get(CAREER_PLAN_SKILL_ID) is not None
    assert registry.get(LEGACY_MAIN_PLANNER_SKILL_ID) is registry.get(CAREER_PLAN_SKILL_ID)
    catalog_ids = [item["skill_id"] for item in build_skill_catalog(registry)]
    assert CAREER_PLAN_SKILL_ID in catalog_ids
    assert GENERAL_CHAT_SKILL_ID not in catalog_ids
    assert LEGACY_MAIN_PLANNER_SKILL_ID not in catalog_ids
    assert catalog_ids.count(CAREER_PLAN_SKILL_ID) == 1


def test_general_chat_routes_career_planning_as_a_choice_without_switching() -> None:
    registry = _registry()
    router = IntentRouter(bundles=registry.bundles, main_skill_id=CAREER_PLAN_SKILL_ID)

    decision = router.route(
        "给孩子做一份生涯规划",
        SessionState(session_id="sess_router", active_skill_id=GENERAL_CHAT_SKILL_ID),
    )

    assert decision.candidate_skills
    assert decision.candidate_skills[0]["target_skill_id"] == CAREER_PLAN_SKILL_ID
    # The route layer only supplies evidence; MainPlanner keeps general_chat
    # active until the user clicks the independent transition button.
    assert decision.requires_user_choice is True
    assert decision.target_skill_id == GENERAL_CHAT_SKILL_ID


def test_junior_first_person_interest_query_routes_to_interest_explore() -> None:
    registry = _registry()
    router = IntentRouter(bundles=registry.bundles, main_skill_id=CAREER_PLAN_SKILL_ID)

    decision = router.route(
        "我还怎么找自己的兴趣爱好",
        SessionState(session_id="sess_junior_interest", active_skill_id=GENERAL_CHAT_SKILL_ID),
    )

    assert decision.target_skill_id == "interest_explore"
    assert decision.candidate_skills
    assert decision.candidate_skills[0]["target_skill_id"] == "interest_explore"
    assert decision.candidate_skills[0]["confidence"] >= 0.9


def test_high_confidence_career_candidate_creates_card_and_dynamic_invitation() -> None:
    context = _general_chat_context()
    context.skill_states["main_planner"]["intent_route"]["candidate_skills"] = [
        {
            "target_skill_id": CAREER_PLAN_SKILL_ID,
            "confidence": 0.95,
            "reason": "用户明确咨询生涯规划",
        }
    ]
    payload, _events = build_finalized_payload(
        context,
        assistant_message="可以先从孩子的兴趣和目标开始梳理。",
        runtime_registry=_registry(),
        route_suggestion_client=_SuggestionClient('{"suggestions":[]}'),
    )
    assert context.interaction_state["active_skill"] == GENERAL_CHAT_SKILL_ID
    assert payload["route_suggestions"][0]["target_skill_id"] == CAREER_PLAN_SKILL_ID
    assert "生涯规划" in build_skill_invitation(
        context,
        assistant_message="可以先从孩子的兴趣和目标开始梳理。",
        runtime_registry=_registry(),
    )


def test_low_confidence_candidate_does_not_create_card() -> None:
    context = _general_chat_context()
    context.skill_states["main_planner"]["intent_route"]["candidate_skills"][0]["confidence"] = 0.71
    payload, _events = build_finalized_payload(
        context,
        assistant_message="先结合当前成绩看看具体情况。",
        runtime_registry=_registry(),
        route_suggestion_client=_SuggestionClient('{"suggestions":[]}'),
    )
    assert payload["route_suggestions"] == []


def test_skill_transition_entry_reply_does_not_offer_another_skill() -> None:
    context = _general_chat_context()
    context.interaction_state["active_skill"] = CAREER_PLAN_SKILL_ID
    context.skill_states["skill_runtime"]["active_skill_id"] = CAREER_PLAN_SKILL_ID
    context.session_meta["include_internal_transition_turn"] = True

    payload, events = build_finalized_payload(
        context,
        assistant_message="我会先结合孩子的情况，梳理下一步的生涯规划重点。",
        runtime_registry=_registry(),
        route_suggestion_client=_SuggestionClient(
            '{"analysis_reason":"可继续深入","suggestions":['
            '{"target_skill_id":"multi_path_planning","reason":"可进一步规划","confidence":0.95}]}'
        ),
        monitor_route_suggestions_every_turn=True,
    )

    assert payload["route_suggestions"] == []
    analysis_event = next(event for event in events if event.get("event_type") == "route_suggestions_analyzed")
    assert analysis_event["payload"]["skipped_reason"] == "skill_transition_entry_turn"


def test_new_and_legacy_default_sessions_are_general_chat() -> None:
    context = SessionContext(session_id="sess_new", user_id="u1", profile_id="p1")
    _initialize_general_chat_state(context)

    assert context.interaction_state["active_skill"] == "general_chat"
    assert context.skill_states["skill_runtime"]["active_skill_id"] == "general_chat"

    legacy = SessionContext(session_id="sess_old", user_id="u1", profile_id="p1")
    legacy.interaction_state = {"active_skill": "main_planner"}
    legacy.skill_states = {"skill_runtime": {"active_skill_id": "main_planner"}}
    assert _normalize_legacy_default_skill(legacy) is True
    assert legacy.interaction_state["active_skill"] == "general_chat"


def test_general_chat_merges_deterministic_cards_with_model_suggestions() -> None:
    context = _general_chat_context()
    client = _SuggestionClient(
        json.dumps(
            {
                "analysis_reason": "正文建议先梳理提分方案，适合提供进一步入口。",
                "suggestions": [
                    {
                        "target_skill_id": "score_improve",
                        "agent_label": "提分规划",
                        "reason": "可继续制定各科学习提升方案。",
                        "confidence": 0.95,
                    }
                ],
            },
            ensure_ascii=False,
        )
    )

    payload, _events = build_finalized_payload(
        context,
        assistant_message="我先给你梳理了分数提升思路，后续可以继续制定学科计划。",
        runtime_registry=_registry(),
        route_suggestion_client=client,
        monitor_route_suggestions_every_turn=True,
    )

    assert context.interaction_state["active_skill"] == "general_chat"
    assert [item["target_skill_id"] for item in payload["route_suggestions"]] == ["mock_admission", "score_improve"]
    decision_prompt = json.loads(client.calls[0])
    assert decision_prompt["router_candidates"][0]["target_skill_id"] == "mock_admission"
    assert payload["route_suggestions"][0]["suggestion_source"] == "intent_router"


def test_high_confidence_router_candidates_create_cards_without_model_decision() -> None:
    context = _general_chat_context()
    client = _SuggestionClient('{"analysis_reason":"正文只是正常回答。","suggestions":[]}')

    payload, _events = build_finalized_payload(
        context,
        assistant_message="这个分数需要再结合位次判断。",
        runtime_registry=_registry(),
        route_suggestion_client=client,
        monitor_route_suggestions_every_turn=True,
    )

    assert [item["target_skill_id"] for item in payload["route_suggestions"]] == ["mock_admission"]
    assert context.skill_states["main_planner"]["intent_route"]["candidate_skills"]


def test_specialist_cross_skill_intent_creates_card_without_switching() -> None:
    context = _general_chat_context()
    context.interaction_state["active_skill"] = "multi_path_planning"
    context.skill_states["skill_runtime"]["active_skill_id"] = "multi_path_planning"
    context.skill_states["main_planner"]["intent_route"] = {
        "route_mode": "recommend_switch",
        "target_skill_id": "interest_explore",
        "confidence": 0.98,
        "reason": "用户的问题更适合进入兴趣探索，但需要用户确认。",
        "requires_user_choice": True,
    }

    payload, _events = build_finalized_payload(
        context,
        assistant_message="孩子喜欢体育，可以先梳理兴趣投入和持续性。",
        runtime_registry=_registry(),
        route_suggestion_client=None,
    )

    assert context.interaction_state["active_skill"] == "multi_path_planning"
    assert context.skill_states["skill_runtime"]["active_skill_id"] == "multi_path_planning"
    assert [item["target_skill_id"] for item in payload["route_suggestions"]] == ["interest_explore"]
    assert payload["route_suggestions"][0]["suggestion_source"] == "intent_router"


def test_mock_admission_path_question_uses_router_card_without_auxiliary_model() -> None:
    registry = _registry()
    decision = IntentRouter(
        bundles=registry.bundles,
        main_skill_id=CAREER_PLAN_SKILL_ID,
    ).route(
        "介绍下综合评价招生",
        SessionState(session_id="sess_mock_route", active_skill_id="mock_admission"),
    )
    assert decision.route_mode == "recommend_switch"
    assert decision.target_skill_id == "multi_path_planning"

    context = _general_chat_context()
    context.interaction_state["active_skill"] = "mock_admission"
    context.skill_states["skill_runtime"]["active_skill_id"] = "mock_admission"
    context.skill_states["planner"] = {"target_skill": "mock_admission"}
    context.skill_states["main_planner"]["intent_route"] = decision.as_dict()
    client = _SuggestionClient("", fail=True)
    errors: list[tuple[str, bool]] = []
    context.session_meta["model_error_callback"] = lambda exc, terminal: errors.append((str(exc), terminal))

    payload, _events = build_finalized_payload(
        context,
        assistant_message="综合评价招生的详细条件由多元路径规划板块承接，请点击下方按钮进入。",
        runtime_registry=registry,
        route_suggestion_client=client,
        monitor_route_suggestions_every_turn=True,
    )

    assert payload["active_skill"] == "mock_admission"
    assert context.skill_states["skill_runtime"]["active_skill_id"] == "mock_admission"
    assert [item["target_skill_id"] for item in payload["route_suggestions"]] == ["multi_path_planning"]
    assert client.calls == []
    assert errors == []


def test_restricted_skill_is_hidden_from_model_catalog_and_result_validation() -> None:
    context = _general_chat_context(grade="高一")
    client = _SuggestionClient(
        '{"analysis_reason":"建议兴趣探索。","suggestions":['
        '{"target_skill_id":"interest_explore","confidence":0.97,"reason":"不应向高中生展示。"}'
        ']}'
    )

    payload, _events = build_finalized_payload(
        context,
        assistant_message="我先回答当前问题。",
        runtime_registry=_registry(),
        route_suggestion_client=client,
        monitor_route_suggestions_every_turn=True,
    )

    prompt = json.loads(client.calls[0])
    assert "interest_explore" not in {item["skill_id"] for item in prompt["skill_catalog"]}
    assert [item["target_skill_id"] for item in payload["route_suggestions"]] == ["mock_admission"]


def test_suggestion_model_failure_keeps_text_without_a_client_error() -> None:
    context = _general_chat_context()
    errors: list[tuple[str, bool]] = []
    context.session_meta["model_error_callback"] = lambda exc, terminal: errors.append((str(exc), terminal))
    client = _SuggestionClient("", fail=True)

    payload, _events = build_finalized_payload(
        context,
        assistant_message="已生成的正文不会因为卡片判定失败而丢失。",
        runtime_registry=_registry(),
        route_suggestion_client=client,
        monitor_route_suggestions_every_turn=True,
    )

    assert [item["target_skill_id"] for item in payload["route_suggestions"]] == ["mock_admission"]
    assert errors == []


def test_toolbar_and_sse_card_update_are_not_stage_filtered() -> None:
    registry = _registry()
    senior_toolbar = build_skill_catalog(registry, grade="高一")
    unknown_toolbar = build_skill_catalog(registry, grade="")

    assert "mock_admission" in {item["skill_id"] for item in senior_toolbar}
    assert "interest_explore" in {item["skill_id"] for item in senior_toolbar}
    assert {item["skill_id"] for item in senior_toolbar} == {item["skill_id"] for item in unknown_toolbar}

    builder = SseEnvelopeBuilder(run_id="run_1", session_id="sess_1")
    builder.encode("run_started", {"risk_stage": "input"})
    builder.encode("main_content_end", {"message_id": "msg_1", "assistant_message": "正文"})
    raw = builder.encode(
        "skill_action",
        {
            "active_skill": "general_chat",
            "route_suggestions": [
                {
                    "target_skill_id": "score_improve",
                    "agent_label": "提分",
                    "reason": "继续制定计划",
                    "confidence": 0.95,
                }
            ],
        },
    )
    assert raw is not None and raw.startswith("event: state\n")
    assert builder.snapshot()["assistant"]["content"] == "正文"
    assert builder.snapshot()["skill_rooms"][0]["skill_id"] == "score_improve"
