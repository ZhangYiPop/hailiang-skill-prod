from __future__ import annotations

import json

from hailiang_skills.core.sse_protocol import SseEnvelopeBuilder, presentation_from_message
from hailiang_skills.core.status_labels import normalize_ms_agent_progress_label


def decode_sse(raw: str) -> tuple[str, dict]:
    lines = raw.strip().splitlines()
    return lines[0].split(":", 1)[1].strip(), json.loads(lines[1].split(":", 1)[1].strip())


def test_v2_frames_have_one_event_name_and_fixed_shape() -> None:
    builder = SseEnvelopeBuilder(run_id="run_1", session_id="sess_1")
    frames = [
        builder.encode("run_started", {"risk_stage": "input"}),
        builder.encode("synthetic_progress", {"stage": "intent", "label": "意图判断"}),
        builder.encode("final_text_delta", {"delta": "你好，"}),
        builder.encode("final_text_delta", {"delta": "世界"}),
        builder.encode(
            "skill_action",
            {
                "message_blocks": [
                    {
                        "type": "fact_form",
                        "payload": {
                            "form_id": "grade",
                            "title": "补充信息",
                            "fields": [
                                {
                                    "fact_key": "subjects",
                                    "label": "选科科目",
                                    "input_type": "multi_select",
                                    "max_selections": 3,
                                },
                                {
                                    "fact_key": "gender",
                                    "label": "性别",
                                    "input_type": "single_select",
                                },
                            ],
                        },
                    },
                    {
                        "type": "path_actions",
                        "payload": {
                            "actions": [
                                {
                                    "path_id": "path_eval",
                                    "path_name": "综合评价招生",
                                    "description": "适合综合素质表现较好的学生。",
                                }
                            ]
                        },
                    }
                ],
                "route_suggestions": [
                    {"target_skill_id": "interest_explore", "agent_label": "兴趣探索", "reason": "继续了解兴趣"}
                ],
                "active_skill": "general_chat",
            },
        ),
        builder.encode("run_completed", {"message_id": "msg_1"}),
    ]
    payloads = [decode_sse(frame)[1] for frame in frames if frame]

    assert all(decode_sse(frame)[0] == "state" for frame in frames if frame)
    assert [payload["seq"] for payload in payloads] == sorted(payload["seq"] for payload in payloads)
    required = {
        "protocol", "session_id", "run_id", "seq", "message_id", "status", "assistant",
        "intent", "form", "skill_rooms", "skill_transition", "session", "risk", "error",
        "path_options",
    }
    assert all(required <= set(payload) for payload in payloads)
    assert payloads[-1]["assistant"] == {"content": "你好，世界", "status": "completed"}
    assert payloads[-1]["intent"]["status"] == "streaming"
    assert payloads[-1]["form"]["form_id"] == "grade"
    assert payloads[-1]["form"]["fields"][0]["max_selections"] == 3
    assert [field["input_type"] for field in payloads[-1]["form"]["fields"]] == [
        "multi_select", "single_select"
    ]
    assert payloads[-1]["skill_rooms"][0]["enabled"] is True
    assert payloads[-1]["path_options"] == {
        "status": "active",
        "interaction_id": "path_actions",
        "source_message_id": "",
        "options": [
            {
                "path_id": "path_eval",
                "title": "综合评价招生",
                "description": "适合综合素质表现较好的学生。",
                "prompt": "我想了解：综合评价招生 路径",
                "enabled": True,
            }
        ],
    }
    assert payloads[-1]["error"] == {
        "code": "", "message": "", "upstream_detail": "", "retryable": False, "terminal": False,
    }


def test_v2_model_error_is_streamed_with_terminal_state() -> None:
    builder = SseEnvelopeBuilder(run_id="run_1", session_id="sess_1")
    builder.encode("run_started", {"risk_stage": "input"})
    warning = builder.encode(
        "model_error",
        {"error": {"code": "MODEL_TIMEOUT", "message": "模型响应超时，请稍后重试。", "upstream_detail": "Timeout", "retryable": True, "terminal": False}},
    )
    _, warning_state = decode_sse(warning or "")
    assert warning_state["status"] == "streaming"
    assert warning_state["error"]["code"] == "MODEL_TIMEOUT"

    failed = builder.encode(
        "run_failed",
        {"error": {"code": "MODEL_UPSTREAM_ERROR", "message": "模型上游服务异常，请稍后重试。", "upstream_detail": "503", "retryable": True, "terminal": True}},
    )
    _, failed_state = decode_sse(failed or "")
    assert failed_state["status"] == "failed"
    assert failed_state["error"]["terminal"] is True


def test_skill_context_preserves_skill_brief_and_info_in_active_skill() -> None:
    builder = SseEnvelopeBuilder(run_id="run_skill", session_id="sess_skill")
    raw = builder.encode(
        "skill_context",
        {
            "active_skill": "multi_path_planning",
            "active_skill_label": "多元路径推荐",
            "skill_brief": "提供高中多元升学路径参考。",
            "skill_info": "结合省份、选科和成绩分析适合的升学路径。",
            "description": "高中多元升学路径推荐顾问",
            "scene_name": "多元路径推荐",
        },
    )
    _, payload = decode_sse(raw or "")

    active_skill = payload["session"]["active_skill"]
    assert active_skill["brief"] == "提供高中多元升学路径参考。"
    assert active_skill["info"] == "结合省份、选科和成绩分析适合的升学路径。"
    assert active_skill["description"] == "高中多元升学路径推荐顾问"


def test_v2_reasoning_label_is_user_facing_and_detail_is_not_wire_text() -> None:
    builder = SseEnvelopeBuilder(run_id="run_1", session_id="sess_1")
    raw = builder.encode(
        "skill_status",
        {"stage": "ms_agent_step_2", "label": "调用内部脚本读取资料", "detail": "SECRET"},
    )
    _, payload = decode_sse(raw or "")
    step = payload["intent"]["steps"][0]
    assert step["label"] == "调用内部脚本读取资料"
    assert step["detail"] == "SECRET"


def test_v2_reasoning_status_hides_response_and_deduplicates_labels() -> None:
    builder = SseEnvelopeBuilder(run_id="run_1", session_id="sess_1")
    builder.encode("skill_status", {"stage": "intent", "label": "正在推进本轮规划"})
    builder.encode("skill_status", {"stage": "ms_agent_step_2", "label": "正在推进本轮规划"})
    builder.encode("skill_status", {"stage": "response", "label": "正在生成回复"})

    steps = builder.snapshot()["intent"]["steps"]
    assert [item["label"] for item in steps] == ["推进本轮规划"]


def test_v2_reasoning_stage_keeps_its_first_label_until_completed() -> None:
    builder = SseEnvelopeBuilder(run_id="run_1", session_id="sess_1")
    builder.encode(
        "skill_status",
        {"stage": "intent", "label": "识别本轮需求", "detail": "等待 ms-agent 执行步骤"},
    )
    builder.encode(
        "skill_status",
        {
            "stage": "intent",
            "label": "直接回答用户关于浙江物理类分数的建议",
            "detail": "模型返回的完整计划动作",
        },
    )
    builder.encode("progress_completed", {"status": "completed"})

    intent = builder.snapshot()["intent"]
    assert intent["status"] == "completed"
    assert intent["steps"] == [
        {
            "id": "intent",
            "label": "识别本轮需求",
            "status": "completed",
            "detail": "模型返回的完整计划动作",
        }
    ]


def test_late_status_cannot_reopen_completed_intent_while_reply_streams() -> None:
    builder = SseEnvelopeBuilder(run_id="run_1", session_id="sess_1")
    builder.encode("run_started", {"session_id": "sess_1"})
    builder.encode("skill_status", {"stage": "intent", "label": "意图判断"})
    builder.encode("progress_completed", {"status": "completed"})
    builder.encode("final_text_delta", {"delta": "正文开始输出。"})
    completed_intent = builder.snapshot()["intent"]

    raw = builder.encode("skill_status", {"stage": "intent", "label": "继续推理"})

    assert raw is None
    snapshot = builder.snapshot()
    assert snapshot["intent"]["status"] == "completed"
    assert snapshot["intent"] == completed_intent


def test_ms_agent_progress_label_has_running_prefix_and_maximum_length() -> None:
    label = normalize_ms_agent_progress_label("分析湖北物理组成绩和院校层次")

    assert label.startswith("正在")
    assert len(label) <= 12


def test_v2_blocked_state_clears_visible_content_and_cards() -> None:
    builder = SseEnvelopeBuilder(run_id="run_1", session_id="sess_1")
    builder.encode("final_text_delta", {"delta": "不能保留的正文"})
    raw = builder.encode("moderation_blocked", {"stage": "output"})
    _, payload = decode_sse(raw or "")
    assert payload["status"] == "blocked"
    assert payload["assistant"]["content"] == ""
    assert payload["form"] == {}
    assert payload["skill_rooms"] == []
    assert payload["path_options"] == {}
    assert payload["risk"] == {
        "status": "blocked",
        "stage": "output",
        "blocked": True,
        "message": "该内容当前无法继续处理，请调整后重新输入。",
    }


def test_legacy_history_is_normalized_and_only_latest_room_is_enabled() -> None:
    message = {
        "message_id": "msg_1",
        "role": "assistant",
        "content": "历史回复",
        "blocks": [{"type": "fact_form", "payload": {"form_id": "f1", "fields": [{"fact_key": "grade"}]}}],
        "route_suggestions": [{"target_skill_id": "interest_explore", "agent_label": "兴趣探索", "reason": "继续"}],
    }
    old = presentation_from_message(message, latest=False)
    latest = presentation_from_message(message, latest=True)
    assert old["form"]["form_id"] == "f1"
    assert old["skill_rooms"][0]["enabled"] is False
    assert latest["skill_rooms"][0]["enabled"] is True


def test_legacy_path_actions_are_normalized_to_v2_path_options() -> None:
    message = {
        "message_id": "msg_path",
        "role": "assistant",
        "content": "已找到几条路径。",
        "blocks": [
            {
                "type": "path_actions",
                "payload": {
                    "actions": [
                        {"path_id": "p1", "path_name": "强基计划", "description": "基础学科拔尖路径"}
                    ]
                },
            }
        ],
    }
    old = presentation_from_message(message, latest=False)
    latest = presentation_from_message(message, latest=True)
    assert old["path_options"]["options"][0]["enabled"] is False
    assert latest["path_options"]["options"][0]["prompt"] == "我想了解：强基计划 路径"
    assert latest["path_options"]["options"][0]["enabled"] is True
