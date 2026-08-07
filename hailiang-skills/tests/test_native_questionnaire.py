from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from hailiang_skills.core.context import SessionContext
from hailiang_skills.core.registry import SkillRegistry
from hailiang_skills.core.skill_display import build_skill_catalog
from hailiang_skills.llm.config import load_llm_config
from hailiang_skills.runtime_bridge.main_planner import MainPlannerOrchestrator
from hailiang_skills.runtime_bridge.native_questionnaire import (
    available_question_specs,
    build_questionnaire_protocol,
    consume_pending_questionnaire_answer,
    decode_questionnaire_reply,
    flush_deferred_questionnaire_promotions,
    questionnaire_continuation_context,
    question_specs,
    resolve_questionnaire_continuation,
    stage_questionnaire_form,
)
from hailiang_skills.runtime_bridge.native_path_options import resolve_native_path_options
from hailiang_skills.runtime_bridge.runtime_config import load_runtime_bridge_config
from hailiang_skills.skill_runtime.models import SessionState
from hailiang_skills.skill_runtime.skill_loader import load_skill_bundle_from_directory
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry
from hailiang_skills.skill_runtime.tools import build_tool_specs


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "runtime_skills"


def _bundle(skill_id: str):
    bundle = load_local_skill_registry(SKILLS_ROOT).get(skill_id)
    assert bundle is not None
    return bundle


def test_multi_path_question_rules_produce_canonical_form_metadata():
    bundle = _bundle("multi_path_planning")
    specs = {item["question_id"]: item for item in question_specs(bundle)}

    subject = specs["选科科目"]
    assert subject["input_type"] == "multi_select"
    assert subject["max_selections"] == 3
    assert {item["value"] for item in subject["options"]} == {
        "物理", "化学", "生物", "政治", "历史", "地理", "技术"
    }

    state = SessionState(session_id="sess_question", active_skill_id="multi_path_planning")
    text, block = decode_questionnaire_reply(
        bundle,
        state,
        '{"assistant_message":"请选择选科。","state_patch":{"mode":"recommend","stage":"profile"},"question":{"question_id":"选科科目"}}',
    )

    assert text == "请选择选科。"
    assert state.stage == "profile"
    assert state.skill_facts["multi_path_planning"]["stage"] == "profile"
    assert block is not None
    field = block["payload"]["fields"][0]
    assert field["input_type"] == "multi_select"
    assert field["max_selections"] == 3
    assert field["scope"] == "skill_session"


def test_multi_path_native_protocol_exposes_path_catalog_and_structured_output_contract():
    bundle = _bundle("multi_path_planning")
    state = SessionState(session_id="sess_path_protocol", active_skill_id="multi_path_planning")

    protocol = build_questionnaire_protocol(bundle, state)

    assert "path_catalog=" in protocol
    assert "state_patch.matched_paths" in protocol
    assert '"path_id": "0101"' in protocol


def test_native_path_options_resolve_structured_ids_and_text_fallback_from_catalog():
    structured = resolve_native_path_options(
        "multi_path_planning",
        "output",
        "无关正文",
        ["0401", "综合评价", "not-a-path"],
    )
    assert [item["path_id"] for item in structured] == ["0401", "0501"]

    text = (
        "本轮建议关注：普通高考、边防军人子女预科班、强基计划、综合评价、"
        "省属三位一体（浙江）、港澳升学、海外升学、三大专项（高校专项/地方专项）、艺术体育。"
    )
    fallback = resolve_native_path_options("multi_path_planning", "output", text, [])
    assert [item["primary_category"] for item in fallback] == [
        "普通高考",
        "边防军人子女预科班",
        "强基计划",
        "综合评价",
        "省属三位一体（浙江）",
        "港澳升学",
        "海外升学",
        "三大专项",
        "艺术升学",
        "体育升学",
    ]
    assert resolve_native_path_options("multi_path_planning", "matching", text, []) == []


def test_multi_path_conditions_and_options_are_driven_by_the_rule_table():
    bundle = _bundle("multi_path_planning")
    state = SessionState(session_id="sess_rules", active_skill_id="multi_path_planning")

    initial = {item["question_id"]: item for item in available_question_specs(bundle, state)}
    assert "学考成绩" not in initial
    assert "竞赛情况" not in initial
    assert "升学诉求" not in initial
    assert "技术" not in {item["value"] for item in initial["选科科目"]["options"]}

    state.skill_facts["multi_path_planning"] = {
        "answers": {"高考省份": "浙江", "预估高考总分": 600, "选科科目": ["物理", "化学", "技术"]}
    }
    scoped = {item["question_id"]: item for item in available_question_specs(bundle, state)}
    assert "技术" in {item["value"] for item in scoped["选科科目"]["options"]}
    assert "学考成绩" in scoped
    assert "竞赛情况" in scoped
    assert {item["value"] for item in scoped["升学诉求"]["options"]} == {"冲院校", "稳就业", "无明确需求"}

    state.skill_facts["multi_path_planning"]["answers"]["升学诉求"] = ["稳就业"]
    with_work = {item["question_id"]: item for item in available_question_specs(bundle, state)}
    assert {item["value"] for item in with_work["稳就业方向"]["options"]} == {"军警", "师范", "农科", "医学", "飞行员"}


def test_question_answers_are_session_private_by_default():
    bundle = _bundle("multi_path_planning")
    state = SessionState(session_id="sess_private", active_skill_id="multi_path_planning")
    _, block = decode_questionnaire_reply(
        bundle,
        state,
        '{"assistant_message":"请选择。","question":{"question_id":"学生类型"}}',
    )
    assert block is not None
    stage_questionnaire_form(state, bundle, block)
    context = SessionContext(session_id="sess_private", user_id="user")

    result = consume_pending_questionnaire_answer(state, context, bundle, "学生类型：内地考生")

    assert result is not None
    assert state.skill_facts["multi_path_planning"]["answers"]["学生类型"] == "内地考生"
    assert context.profile_facts.facts == {}
    assert context.shared_facts.facts == {}
    assert context.session_facts.facts == {}


def test_batch_questionnaire_uses_existing_multi_field_form_and_consumes_all_answers():
    bundle = _bundle("multi_path_planning")
    state = SessionState(session_id="sess_batch", active_skill_id="multi_path_planning")
    text, block = decode_questionnaire_reply(
        bundle,
        state,
        '{"assistant_message":"请补充匹配信息。","state_patch":{"mode":"match_single","stage":"profile"},'
        '"question_ids":["选科科目","预估高考总分","性别","身高"]}',
    )

    assert text == "请补充匹配信息。"
    assert block is not None
    fields = block["payload"]["fields"]
    assert [field["question_id"] for field in fields] == ["选科科目", "预估高考总分", "性别", "身高"]
    assert fields[0]["input_type"] == "multi_select"
    assert fields[0]["max_selections"] == 3
    assert fields[2]["input_type"] == "single_select"
    stage_questionnaire_form(state, bundle, block)
    context = SessionContext(session_id="sess_batch", user_id="user")

    result = consume_pending_questionnaire_answer(
        state,
        context,
        bundle,
        "选科科目：物理、化学、生物；预估高考总分：580；性别：男；身高（cm）：175",
    )

    assert result is not None
    assert result["question_ids"] == ["选科科目", "预估高考总分", "性别", "身高"]
    assert state.skill_facts["multi_path_planning"]["answers"] == {
        "选科科目": ["物理", "化学", "生物"],
        "预估高考总分": 580,
        "性别": "男",
        "身高": 175,
    }
    assert context.profile_facts.facts == {}
    assert context.shared_facts.facts == {}
    assert context.session_facts.facts == {}


def test_slash_separated_options_are_trimmed_and_multi_select_answers_use_same_normalization():
    bundle = _bundle("multi_path_planning")
    specs = {item["question_id"]: item for item in question_specs(bundle)}
    direction = specs["方向（艺术）"]
    assert [item["value"] for item in direction["options"]][:3] == ["美术", "音乐", "播音与主持"]
    assert all(item["value"] == item["value"].strip() for item in direction["options"])

    state = SessionState(session_id="sess_art_direction", active_skill_id="multi_path_planning")
    _, block = decode_questionnaire_reply(
        bundle,
        state,
        '{"assistant_message":"请选择方向。","question_ids":["方向（艺术）"]}',
    )
    assert block is not None
    stage_questionnaire_form(state, bundle, block)
    context = SessionContext(session_id="sess_art_direction", user_id="user")

    result = consume_pending_questionnaire_answer(
        state,
        context,
        bundle,
        "以下哪类艺术升学方向感兴趣？：美术 、 音乐",
    )

    assert result is not None
    assert state.skill_facts["multi_path_planning"]["answers"]["方向（艺术）"] == ["美术", "音乐"]


def test_lightweight_questionnaire_rejects_invented_ids_without_retrying_model_logic():
    bundle = _bundle("multi_path_planning")
    state = SessionState(session_id="sess_lightweight_guard", active_skill_id="multi_path_planning")
    continuation = questionnaire_continuation_context(bundle, state)
    assert continuation is not None

    text, block, decision = resolve_questionnaire_continuation(
        bundle,
        state,
        '{"assistant_message":"我们继续补充信息。","question_ids":["虚构问题"],"collection_complete":true}',
    )

    assert text == "我们继续补充信息。"
    assert decision["fallback_used"] is True
    assert block is not None
    selected = [field["question_id"] for field in block["payload"]["fields"]]
    assert selected
    assert set(selected) <= {item["question_id"] for item in continuation["question_catalog"]}


def test_first_questionnaire_turn_reuses_safe_facts_and_explicit_user_answers():
    bundle = _bundle("multi_path_planning")
    state = SessionState(session_id="sess_reconcile", active_skill_id="multi_path_planning")
    state.global_facts = {"grade": "高一"}
    state.conversation_memory = {
        "recent_messages": [
            {"role": "user", "content": "孩子目前高一，对美术和音乐都感兴趣。"},
        ],
    }
    continuation = questionnaire_continuation_context(bundle, state)
    assert continuation is not None
    reconciliation = continuation["answer_reconciliation"]
    assert reconciliation["enabled"] is True
    assert reconciliation["fact_sources"] == [
        {
            "source_id": "runtime_fact:grade",
            "source_type": "fact",
            "fact_key": "grade",
            "value": "高一",
            "eligible_question_ids": ["当前年级"],
        }
    ]

    text, block, decision = resolve_questionnaire_continuation(
        bundle,
        state,
        '{"assistant_message":"已经了解到孩子目前高一，并且对美术和音乐感兴趣。",'
        '"resolved_answers":['
        '{"question_id":"当前年级","value":"高一","source_id":"runtime_fact:grade",'
        '"evidence":"","confidence":0.99},'
        '{"question_id":"方向（艺术）","value":["美术","音乐"],'
        '"source_id":"user_message:1","evidence":"对美术和音乐都感兴趣","confidence":0.96}],'
        '"question_ids":["高考省份"],"collection_complete":false}',
    )

    assert text.startswith("已经了解到")
    assert state.skill_facts["multi_path_planning"]["answers"] == {
        "当前年级": "高一",
        "方向（艺术）": ["美术", "音乐"],
    }
    assert [item["question_id"] for item in decision["resolved_answers"]] == [
        "当前年级", "方向（艺术）"
    ]
    assert block is not None
    assert [field["question_id"] for field in block["payload"]["fields"]] == ["高考省份"]
    next_context = questionnaire_continuation_context(bundle, state)
    assert next_context is not None
    assert next_context["answer_reconciliation"]["enabled"] is False


def test_first_questionnaire_turn_reuses_summarized_english_exam_answer():
    bundle = _bundle("multi_path_planning")
    state = SessionState(session_id="sess_reconcile_english", active_skill_id="multi_path_planning")
    state.global_facts = {
        "grade": "高一",
        "foreign_language": "英语",
        "english_exam_score": 120,
    }
    state.conversation_memory = {
        "summary": "用户是高一新生，英语大考成绩为 120 分。",
        "recent_messages": [],
        "questionnaire_evidence_messages": [
            {
                "role": "user",
                "content": "我是高一新生，我的选科是英语，英语大考能考 120。",
                "source_skill_id": "general_chat",
            },
        ],
    }

    continuation = questionnaire_continuation_context(bundle, state)
    assert continuation is not None
    assert continuation["answer_reconciliation"]["message_sources"] == [
        {
            "source_id": "user_message:1",
            "source_type": "user_message",
            "content": "我是高一新生，我的选科是英语，英语大考能考 120。",
        }
    ]

    _text, block, decision = resolve_questionnaire_continuation(
        bundle,
        state,
        '{"assistant_message":"已经了解到你是高一新生，外语为英语，大考约 120 分。",'
        '"resolved_answers":['
        '{"question_id":"当前年级","value":"高一","source_id":"runtime_fact:grade",'
        '"evidence":"","confidence":0.99},'
        '{"question_id":"外语科目","value":"英语","source_id":"runtime_fact:foreign_language",'
        '"evidence":"","confidence":0.99},'
        '{"question_id":"英语水平","value":"120","source_id":"runtime_fact:english_exam_score",'
        '"evidence":"","confidence":0.99}],'
        '"question_ids":["学生类型"],"collection_complete":false}',
    )

    answers = state.skill_facts["multi_path_planning"]["answers"]
    assert answers["当前年级"] == "高一"
    assert answers["外语科目"] == "英语"
    assert answers["英语水平"] == 120
    assert {item["question_id"] for item in decision["resolved_answers"]} == {
        "当前年级", "外语科目", "英语水平"
    }
    assert block is not None
    displayed = {field["question_id"] for field in block["payload"]["fields"]}
    assert "英语水平" not in displayed
    assert displayed == {"学生类型"}


def test_first_questionnaire_turn_reuses_multiple_answers_from_session_evidence():
    bundle = _bundle("multi_path_planning")
    state = SessionState(session_id="sess_reconcile_session_history", active_skill_id="multi_path_planning")
    state.global_facts = {"grade": "高一"}
    state.conversation_memory = {
        "summary": "",
        "facts": {},
        "recent_messages": [],
        "questionnaire_evidence_messages": [
            {
                "role": "user",
                "content": "我是高一新生，我的选科是英语，英语大考能考 120，家在北京，应该在北京高考",
                "source_skill_id": "session_history",
            },
        ],
    }

    _text, block, decision = resolve_questionnaire_continuation(
        bundle,
        state,
        '{"assistant_message":"已了解你的年级、外语、英语成绩和高考省份。",'
        '"resolved_answers":['
        '{"question_id":"当前年级","value":"高一","source_id":"runtime_fact:grade",'
        '"evidence":"","confidence":0.99},'
        '{"question_id":"高考省份","value":"北京","source_id":"user_message:1",'
        '"evidence":"应该在北京高考","confidence":0.99},'
        '{"question_id":"外语科目","value":"英语","source_id":"user_message:1",'
        '"evidence":"我的选科是英语","confidence":0.99},'
        '{"question_id":"英语水平","value":"120","source_id":"user_message:1",'
        '"evidence":"英语大考能考 120","confidence":0.99}],'
        '"question_ids":["学生类型"],"collection_complete":false}',
    )

    answers = state.skill_facts["multi_path_planning"]["answers"]
    assert answers == {
        "当前年级": "高一",
        "高考省份": "北京",
        "外语科目": "英语",
        "英语水平": 120,
    }
    assert {item["question_id"] for item in decision["resolved_answers"]} == set(answers)
    assert block is not None
    displayed = {field["question_id"] for field in block["payload"]["fields"]}
    assert displayed == {"学生类型"}


def test_questionnaire_reconciliation_rejects_wrong_fact_binding_and_unverifiable_quote():
    bundle = _bundle("multi_path_planning")
    state = SessionState(session_id="sess_reconcile_guard", active_skill_id="multi_path_planning")
    state.global_facts = {"grade": "高一"}
    state.conversation_memory = {
        "recent_messages": [{"role": "user", "content": "孩子喜欢画画。"}],
    }

    _text, block, decision = resolve_questionnaire_continuation(
        bundle,
        state,
        '{"assistant_message":"请继续补充。","resolved_answers":['
        '{"question_id":"高考省份","value":"浙江","source_id":"runtime_fact:grade",'
        '"evidence":"","confidence":0.99},'
        '{"question_id":"方向（艺术）","value":["美术"],"source_id":"user_message:1",'
        '"evidence":"用户明确选择了美术","confidence":0.99}],'
        '"question_ids":["当前年级"],"collection_complete":false}',
    )

    assert state.skill_facts["multi_path_planning"].get("answers", {}) == {}
    assert set(decision["rejected_resolved_answers"]) == {"高考省份", "方向（艺术）"}
    assert block is not None
    assert [field["question_id"] for field in block["payload"]["fields"]] == ["当前年级"]


def test_questionnaire_reconciliation_does_not_treat_birthplace_as_exam_province():
    bundle = _bundle("multi_path_planning")
    state = SessionState(session_id="sess_province_guard", active_skill_id="multi_path_planning")
    state.conversation_memory = {
        "questionnaire_evidence_messages": [
            {"role": "user", "content": "我是北京的，目前读高一。"},
        ],
    }

    _text, block, decision = resolve_questionnaire_continuation(
        bundle,
        state,
        '{"assistant_message":"请继续补充。","resolved_answers":['
        '{"question_id":"高考省份","value":"北京","source_id":"user_message:1",'
        '"evidence":"我是北京的","confidence":0.99}],'
        '"question_ids":["当前年级"],"collection_complete":false}',
    )

    assert "高考省份" not in state.skill_facts["multi_path_planning"].get("answers", {})
    assert decision["rejected_resolved_answers"] == ["高考省份"]
    assert block is not None


def test_question_persistence_mapping_is_explicitly_allowlisted():
    bundle = _bundle("mock_admission")
    bundle.metadata["questionnaire"]["persistence"]["mappings"] = {
        "高考省份": {"target": "profile_fact", "fact_key": "student_province"},
        "当前成绩": {"target": "profile_fact", "fact_key": "not_allowed"},
    }
    state = SessionState(session_id="sess_promote", active_skill_id="mock_admission")
    _, block = decode_questionnaire_reply(
        bundle,
        state,
        '{"assistant_message":"请输入省份。","question":{"question_id":"高考省份"}}',
    )
    assert block is not None
    stage_questionnaire_form(state, bundle, block)
    context = SessionContext(session_id="sess_promote", user_id="user")

    result = consume_pending_questionnaire_answer(state, context, bundle, "高考省份：浙江")

    assert result is not None
    assert result["promotions"] == [{"fact_key": "student_province", "scope": "profile", "value": "浙江"}]
    assert context.profile_facts.get_value("student_province") == "浙江"


def test_question_persistence_rejects_wrong_types_and_unknown_target_fields():
    bundle = _bundle("mock_admission")
    bundle.metadata["questionnaire"]["persistence"]["mappings"] = {
        "当前成绩": {"target": "session_fact", "fact_key": "score_total", "value_type": "string"},
        "高考省份": {"target": "shared_fact", "fact_key": "not_allowed", "value_type": "string"},
    }
    context = SessionContext(session_id="sess_rejected", user_id="user")
    state = SessionState(session_id="sess_rejected", active_skill_id="mock_admission")

    _, score_block = decode_questionnaire_reply(
        bundle,
        state,
        '{"assistant_message":"请输入成绩。","question":{"question_id":"当前成绩"}}',
    )
    assert score_block is not None
    stage_questionnaire_form(state, bundle, score_block)
    result = consume_pending_questionnaire_answer(state, context, bundle, "最近一次大考成绩：512")

    assert result is not None
    assert result["promotions"] == []
    assert context.session_facts.facts == {}


def test_question_persistence_can_defer_an_allowlisted_write_until_skill_completion():
    bundle = _bundle("mock_admission")
    bundle.metadata["questionnaire"]["persistence"]["mappings"] = {
        "高考省份": {
            "target": "profile_fact",
            "fact_key": "student_province",
            "value_type": "string",
            "submit_timing": "on_completion",
        }
    }
    state = SessionState(session_id="sess_deferred", active_skill_id="mock_admission")
    _, block = decode_questionnaire_reply(
        bundle,
        state,
        '{"assistant_message":"请输入省份。","question":{"question_id":"高考省份"}}',
    )
    assert block is not None
    stage_questionnaire_form(state, bundle, block)
    context = SessionContext(session_id="sess_deferred", user_id="user")

    result = consume_pending_questionnaire_answer(state, context, bundle, "高考省份：浙江")

    assert result is not None
    assert result["promotions"] == []
    assert context.profile_facts.facts == {}
    assert flush_deferred_questionnaire_promotions(state, context, bundle) == [
        {"fact_key": "student_province", "scope": "profile", "value": "浙江"}
    ]


def test_runtime_config_keeps_native_default_and_allows_manual_rollback(tmp_path):
    config_path = tmp_path / "runtime.yml"
    config_path.write_text("legacy_bridge_skill_ids: [mock_admission]\n", encoding="utf-8")

    rollback = load_runtime_bridge_config(config_path)
    default = load_runtime_bridge_config(tmp_path / "missing.yml")

    assert rollback.legacy_bridge_skill_ids == {"mock_admission"}
    assert default.legacy_bridge_skill_ids == set()


def test_public_catalog_uses_skill_markdown_display_metadata():
    registry = load_local_skill_registry(SKILLS_ROOT)
    catalog = build_skill_catalog(registry)
    labels = {item["skill_id"]: item["label"] for item in catalog}

    assert labels["mock_admission"] == "升学潜力评估"
    assert labels["subject_advisor"] == "选科参谋"
    assert labels["multi_path_planning"] == "多元路径推荐"
    assert labels["interest_explore"] == "特长培养规划"
    assert labels["career_plan_entity"] == "升学规划顾问"

    admission = next(item for item in catalog if item["skill_id"] == "mock_admission")
    bundle = registry.get("mock_admission")
    assert bundle is not None
    assert admission["brief"] == bundle.runtime_metadata.brief
    assert admission["info"] == bundle.runtime_metadata.info


def test_skill_loader_accepts_lowercase_and_legacy_uppercase_entrypoints(tmp_path):
    for directory_name, entrypoint in (("lower", "skill.md"), ("upper", "SKILL.md")):
        skill_dir = tmp_path / directory_name
        skill_dir.mkdir()
        (skill_dir / entrypoint).write_text(
            "---\nname: Test Skill\nskill_id: test_skill\n---\n# Test Skill\n",
            encoding="utf-8",
        )

        bundle = load_skill_bundle_from_directory(skill_dir)

        assert bundle.skill_file.name == entrypoint


def test_replacement_skills_do_not_enable_web_or_other_runtime_tools():
    for skill_id in ("mock_admission", "multi_path_planning"):
        bundle = _bundle(skill_id)
        specs = build_tool_specs(bundle, SessionState(session_id=f"sess_{skill_id}", active_skill_id=skill_id))

        assert all(not spec.enabled for spec in specs)
        assert not next(spec for spec in specs if spec.name == "web_search").enabled


class _QuestionnaireClient:
    def complete(self, *_args, **_kwargs):
        return (
            '{"question_ids":["高考省份"],"collection_complete":false,'
            '"assistant_message":"请补充省份。"}'
        )


class _RetryQuestionnaireClient:
    def __init__(self, *, recover: bool) -> None:
        self.recover = recover
        self.calls = 0

    def complete(self, *_args, **_kwargs):
        self.calls += 1
        if not self.recover:
            return "请补充选科科目、预估高考总分和性别。"
        return (
            '{"question_ids":["选科科目","预估高考总分","性别"],'
            '"collection_complete":false,"assistant_message":"请填写匹配信息。"}'
        )


class _NativePathClient:
    def complete_with_tools(self, *_args, **_kwargs):
        from hailiang_skills.skill_runtime.models import AssistantTurnResult

        return AssistantTurnResult(
            final_text=(
                '{"assistant_message":"推荐普通高考、强基计划和综合评价。",'
                '"state_patch":{"stage":"output","matched_paths":["0101","0401","0501"]},'
                '"question_ids":[]}'
            ),
            tool_mode="none",
        )


def _native_skill_context(session_id: str, skill_id: str) -> SessionContext:
    context = SessionContext(session_id=session_id, user_id="user")
    context.interaction_state["active_skill"] = skill_id
    context.skill_states["skill_runtime"] = {
        "active_skill_id": skill_id,
        "stage": "collect",
        "skill_facts": {skill_id: {}},
    }
    if skill_id == "multi_path_planning":
        context.update_fact("grade", "高二", source_skill="test")
    return context


def test_native_multi_path_conclusion_becomes_suggested_paths_and_candidate_assets(monkeypatch):
    orchestrator = MainPlannerOrchestrator(SkillRegistry(), load_llm_config())
    orchestrator.runtime_client = _NativePathClient()
    monkeypatch.setattr(
        "hailiang_skills.runtime_bridge.main_planner.questionnaire_continuation_context",
        lambda *_args: None,
    )
    monkeypatch.setattr(orchestrator, "_prepare_ms_agent_native_turn", lambda *_args: None)
    context = _native_skill_context("sess_native_path_output", "multi_path_planning")

    result = orchestrator.handle_message("帮我匹配升学路径", context)

    assert result.suggested_paths == ["普通高考", "强基计划", "综合评价"]
    assert [item["path_id"] for item in result.candidate_paths] == ["0101", "0401", "0501"]
    assert [item["path_id"] for item in context.candidate_paths] == ["0101", "0401", "0501"]


def test_new_skills_use_native_runtime_until_server_rollback_is_explicitly_enabled(monkeypatch):
    orchestrator = MainPlannerOrchestrator(SkillRegistry(), load_llm_config())
    orchestrator.runtime_client = _QuestionnaireClient()
    orchestrator.questionnaire_client = orchestrator.runtime_client
    # The dispatch test isolates bridge selection from the optional ms-agent
    # package. The production adapter is exercised separately by its suite.
    monkeypatch.setattr(orchestrator, "_prepare_ms_agent_native_turn", lambda *_args: None)

    legacy_calls: list[str] = []

    def legacy_target(_message, _context, target, _turn_id):
        legacy_calls.append(target["skill"])
        from hailiang_skills.skills.base import SkillResult

        return SkillResult(assistant_message="legacy bridge reply")

    monkeypatch.setattr(orchestrator, "_run_hailiang_target", legacy_target)
    for skill_id, message, legacy_skill in (
        ("mock_admission", "我想做模拟升学", "admission"),
        ("multi_path_planning", "我想了解多元升学路径", "convergence"),
    ):
        orchestrator.runtime_bridge_config = replace(
            orchestrator.runtime_bridge_config,
            legacy_bridge_skill_ids=frozenset(),
        )
        legacy_call_count = len(legacy_calls)
        native_context = _native_skill_context(f"sess_native_{skill_id}", skill_id)

        native_result = orchestrator.handle_message(message, native_context)

        assert native_result.assistant_message == "请补充省份。"
        assert len(legacy_calls) == legacy_call_count
        assert native_context.messages[-1]["blocks"][0]["type"] == "fact_form"

        orchestrator.runtime_bridge_config = replace(
            orchestrator.runtime_bridge_config,
            legacy_bridge_skill_ids=frozenset({skill_id}),
        )
        rollback_context = _native_skill_context(f"sess_rollback_{skill_id}", skill_id)

        rollback_result = orchestrator.handle_message(message, rollback_context)

        assert rollback_result.assistant_message == "legacy bridge reply"
        assert legacy_calls[-1] == legacy_skill


def test_valid_lightweight_questionnaire_reply_uses_one_small_call(monkeypatch):
    orchestrator = MainPlannerOrchestrator(SkillRegistry(), load_llm_config())
    client = _RetryQuestionnaireClient(recover=True)
    orchestrator.questionnaire_client = client
    monkeypatch.setattr(
        orchestrator,
        "_prepare_ms_agent_native_turn",
        lambda *_args: (_ for _ in ()).throw(AssertionError("questionnaire turn must skip full planner")),
    )
    context = _native_skill_context("sess_questionnaire_retry", "multi_path_planning")

    result = orchestrator.handle_message("帮我看看现在的条件能不能报军校", context)

    assert client.calls == 1
    assert result.assistant_message == "请填写匹配信息。"
    form = next(block for block in context.messages[-1]["blocks"] if block["type"] == "fact_form")
    assert [field["question_id"] for field in form["payload"]["fields"]] == [
        "选科科目", "预估高考总分", "性别"
    ]


def test_invalid_lightweight_questionnaire_reply_falls_back_without_retry(monkeypatch):
    orchestrator = MainPlannerOrchestrator(SkillRegistry(), load_llm_config())
    client = _RetryQuestionnaireClient(recover=False)
    orchestrator.questionnaire_client = client
    monkeypatch.setattr(orchestrator, "_prepare_ms_agent_native_turn", lambda *_args: None)
    context = _native_skill_context("sess_questionnaire_fallback", "multi_path_planning")

    result = orchestrator.handle_message("帮我看看现在的条件能不能报军校", context)

    assert client.calls == 1
    assert result.assistant_message == "请通过下面的表单继续补充关键信息。"
    form = next(block for block in context.messages[-1]["blocks"] if block["type"] == "fact_form")
    assert form["payload"]["fields"]


def test_explicit_non_questionnaire_skill_entry_skips_full_planner(monkeypatch):
    orchestrator = MainPlannerOrchestrator(SkillRegistry(), load_llm_config())
    fast_path_calls: list[str] = []

    monkeypatch.setattr(
        orchestrator,
        "_prepare_turn_long_context",
        lambda _context, state, _skill_id: setattr(state, "conversation_memory", {}),
    )
    monkeypatch.setattr(
        orchestrator,
        "_resolve_lightweight_skill_entry",
        lambda bundle, *_args: (
            fast_path_calls.append(bundle.contract.skill_id) or "进入轮快速回复",
            "",
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "_resolve_runtime_reply",
        lambda *_args: (_ for _ in ()).throw(AssertionError("explicit entry must skip full planner")),
    )

    for skill_id in ("interest_explore", "career_plan_entity"):
        context = _native_skill_context(f"sess_explicit_fast_entry_{skill_id}", skill_id)
        context.session_meta.update(
            {
                "requested_target_skill_id": skill_id,
                "preactivated_requested_target_skill_id": skill_id,
                "preactivated_previous_skill_id": "general_chat",
            }
        )

        result = orchestrator.handle_message(f"进入{skill_id}", context)

        assert result.assistant_message == "进入轮快速回复"
    assert fast_path_calls == ["interest_explore", "career_plan_entity"]
