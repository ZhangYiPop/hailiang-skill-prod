import pytest

from hailiang_skills.llm.config import UnknownTesterModelError
from hailiang_skills.workbench import WorkbenchError
from hailiang_skills.workbench.batch_eval import BatchEvalService, _parse_decision, resolve_batch_model
from hailiang_skills.workbench.persona import answer_fields, parse_persona


SAMPLE = "城市:山东；学段:高一；性别:男孩；成绩:勉强及格；兴趣：理工科；各科成绩：语文108 数学99；学习状态：任务驱动型"


def test_parse_persona_maps_stage_city_and_role():
    persona = parse_persona(SAMPLE, "孩子数学差可以选物理吗")
    assert persona.user_role == "家长"
    assert persona.school_stage == "高中"
    assert persona.grade == "高一"
    assert persona.province == "山东"
    assert persona.fields["interest"] == "理工科"
    assert persona.seed_facts(preset_identity=True, seed_profile=False) == {
        "user_role": "家长",
        "school_stage": "高中",
    }
    seeded = persona.seed_facts(preset_identity=True, seed_profile=True)
    assert seeded["grade"] == "高一"
    assert seeded["student_province"] == "山东"


def test_explicit_speaker_role_overrides_opening_hints():
    parent = parse_persona("城市:广州；学段:五年级", "我该怎么选科", user_role="家长")
    child = parse_persona(SAMPLE, "孩子数学差可以选物理吗", user_role="学生")
    assert parent.user_role == "家长"
    assert child.user_role == "学生"
    assert child.seed_facts(preset_identity=True, seed_profile=False)["user_role"] == "学生"


def test_student_voice_and_city_province():
    persona = parse_persona("城市:广州；学段:五年级", "我该怎么选科")
    assert persona.user_role == "学生"
    assert persona.school_stage == "小学"
    assert persona.province == "广东"
    assert persona.grade == ""


def test_answer_fields_from_persona_options():
    persona = parse_persona(SAMPLE, "孩子想选科")
    answers, sources = answer_fields([
        {"key": "school_stage", "label": "学段", "input_type": "single_select", "options": [
            {"label": "小学", "value": "小学"},
            {"label": "高中", "value": "高中"},
        ]},
        {"key": "budget", "label": "预算", "input_type": "text"},
    ], persona)
    assert answers == {"school_stage": "高中"}
    assert sources["school_stage"] == "画像:school_stage"


def test_missing_persona_field_is_left_blank():
    persona = parse_persona("城市:山东；学段:高一", "孩子想选科")
    answers, _sources = answer_fields([
        {"key": "language", "label": "雅思成绩", "input_type": "text", "fact_key": "language_score"},
    ], persona)
    assert answers == {}


def test_batch_model_uses_the_debug_console_list():
    class Config:
        def resolve_tester_model(self, requested, *, environment):
            assert environment
            if requested == "pro":
                return "deepseek-v4-pro"
            raise UnknownTesterModelError(requested)

    class Orchestrator:
        llm_config = Config()

    assert resolve_batch_model(Orchestrator(), "pro") == ("pro", "deepseek-v4-pro")
    with pytest.raises(WorkbenchError):
        resolve_batch_model(Orchestrator(), "other")


def test_decision_json_and_batch_progress():
    assert _parse_decision('说明 {"kind":"done","utterance":"","reason":"已经给了结论"}')["reason"] == "已经给了结论"
    service = BatchEvalService(None, None, None, None, None)

    def finish(_job, case):
        case["status"] = "completed"
        case["stop_reason"] = "回复没有继续提问"
        case["result"] = "建议选物理"

    service._run_case = finish
    job = service.start({
        "revision_id": "rev_test",
        "actor_id": "actor_test",
        "persona_mode": "simulator",
        "preset_identity": True,
        "max_rounds": 2,
        "cases": [{"scene": "选科参谋", "opening": "怎么选", "persona": SAMPLE, "goal": "给出组合"}],
    })
    service._threads[job["job_id"]].join(timeout=3)
    done = service.get(job["job_id"])
    assert done["status"] == "completed"
    assert done["finished"] == 1
    assert done["cases"][0]["result"] == "建议选物理"
    exported = service.export_csv(job["job_id"])
    assert "选科参谋" in exported
    assert "建议选物理" in exported
