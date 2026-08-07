#!/usr/bin/env python3
"""
根据当前 prompt_registry 中的 prompt 定义，生成一份静态的 prompt 示例文件，
供业务人员预览。每次修改 prompt 后，重新运行此脚本即可更新示例。

运行方式：
    python scripts/generate_prompt_examples.py

输出目录：config/prompts_examples/
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# 确保 src 在 sys.path 中
_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from hailiang_skills.llm.prompt_registry import (
    get_prompt_spec,
    resolve_skill_response_prompt_key,
)


SAMPLE_FACTS = {
    "student_province": {"value": "浙江", "confidence": 0.99},
    "subject_group": {"value": "物理", "confidence": 0.99},
    "score_total": {"value": 580, "confidence": 0.99},
    "score_source": {"value": "estimated_total", "confidence": 0.99},
}

SAMPLE_USER_INPUT = "我是浙江考生，预估分数580分，能上什么学校"

SAMPLE_ROUTER_STATE = {
    "intent": "admission",
    "target_skill": "admission",
    "confidence": 0.95,
    "reason": "用户明确提供了省份和分数，属于典型模拟升学场景",
}

SAMPLE_PLANNER_STATE = {
    "target_skill": "admission",
    "goal": "基于浙江物理类580分，提供适配的院校层次定位与可报学校范围",
    "response_mode": "answer",
    "missing_facts": [],
    "focus_points": ["强调分数与特控线的关系", "引用代表院校"],
}

SAMPLE_ADMISSION_STRUCTURED = {
    "matched_count": 2,
    "score": 580,
    "province": "浙江",
    "subject_group": "物理",
    "matched_items_brief": [
        {
            "region_variant": "浙江（省内）",
            "tier_name": "浙江省重点第三梯队",
            "score_range": {"min_score": 570, "max_score": 589},
            "sample_schools": ["中国计量大学", "浙江海洋大学"],
            "recommended_paths": ["省属三位一体"],
        }
    ],
    "recommended_path_names": ["省属三位一体"],
    "recommended_path_ids": ["0601"],
}

SAMPLE_CONVERGENCE_STRUCTURED = {
    "feasible_count": 2,
    "partial_count": 3,
    "infeasible_count": 3,
    "top_feasible": [
        {
            "path_id": "0601",
            "primary_category": "省属三位一体",
            "feasibility_status": "feasible",
            "reasons": ["浙江本地专项，分数门槛适中"],
        }
    ],
}

SAMPLE_PATHS_STRUCTURED = [
    {
        "path_id": "0401",
        "primary_category": "强基计划",
        "feasibility_status": "partial",
        "missing_slots": ["student_province", "subject_group"],
        "blocking_reasons": [],
        "required_fact_keys": ["student_province", "subject_group", "score_total"],
        "timeline_step_count": 3,
    }
]

SAMPLE_TERMINATE_STRUCTURED = {
    "candidate_count": 0,
    "direct_count": 0,
    "conditional_count": 0,
}

EXAMPLES = [
    {
        "index": "01",
        "slug": "router",
        "phase": "router",
        "skill_name": "router",
        "title": "Router 阶段",
        "description": "识别用户意图，决定进入哪个 skill",
        "trigger_scenario": f'用户输入"{SAMPLE_USER_INPUT}"时，Router 使用此 prompt',
        "variables": {
            "user_input": SAMPLE_USER_INPUT,
            "context": {
                "known_facts": SAMPLE_FACTS,
                "candidate_paths": [],
                "recent_messages": [],
            },
        },
        "output_hint": '{"intent": "...", "target_skill": "...", "confidence": 0.xx, "reason": "..."}',
    },
    {
        "index": "02",
        "slug": "facts_extractor",
        "phase": "facts_extractor",
        "skill_name": "facts_extractor",
        "title": "Facts 抽取阶段",
        "description": "从用户输入和上下文中抽取结构化 facts",
        "trigger_scenario": f'Router 完成后、Planner 之前使用，处理用户输入"{SAMPLE_USER_INPUT}"',
        "variables": {
            "user_input": SAMPLE_USER_INPUT,
            "context": {
                "known_facts": SAMPLE_FACTS,
                "recent_messages": [],
            },
            "known_provinces": ["浙江", "甘肃", "安徽", "江苏", "山东"],
            "path_catalog_brief": [
                {
                    "path_id": "0401",
                    "primary_category": "强基计划",
                    "required_fact_keys": ["student_province", "subject_group", "score_total"],
                }
            ],
            "school_catalog_brief": [{"school_name": "北京大学"}, {"school_name": "清华大学"}],
        },
        "output_hint": '{"fact_updates": {...}, "confidence": 0.9, "reason": "..."}',
    },
    {
        "index": "03",
        "slug": "planner",
        "phase": "planner",
        "skill_name": "planner",
        "title": "Planner 阶段",
        "description": "在 Router 选定方向后，决定本轮目标、回答模式、缺失 facts 和追问策略",
        "trigger_scenario": 'Facts 抽取完成后、具体 skill 执行前使用',
        "variables": {
            "user_input": SAMPLE_USER_INPUT,
            "router_state": SAMPLE_ROUTER_STATE,
            "context": {
                "known_facts": SAMPLE_FACTS,
                "recent_messages": [],
            },
        },
        "output_hint": '{"target_skill": "...", "goal": "...", "response_mode": "...", "missing_facts": [], ...}',
    },
    {
        "index": "04",
        "slug": "admission_response",
        "phase": "admission_response",
        "skill_name": "admission",
        "title": "Admission Skill 回复",
        "description": "对浙江物理类580分做模拟升学回复",
        "trigger_scenario": 'AdmissionSkill 完成分数档命中后使用',
        "variables": {
            "skill_name": "admission",
            "user_input": SAMPLE_USER_INPUT,
            "context": {"known_facts": SAMPLE_FACTS},
            "planner_state": SAMPLE_PLANNER_STATE,
            "structured_result": SAMPLE_ADMISSION_STRUCTURED,
        },
        "output_hint": "自然语言文本（无需 JSON）",
    },
    {
        "index": "05",
        "slug": "convergence_response",
        "phase": "convergence_response",
        "skill_name": "convergence",
        "title": "Convergence Skill 回复",
        "description": "对多条路径做可行性收敛回复",
        "trigger_scenario": 'ConvergenceSkill 完成候选路径打分、状态判定后使用',
        "variables": {
            "skill_name": "convergence",
            "user_input": "浙江考生有哪些升学路径可以推荐？",
            "context": {"known_facts": SAMPLE_FACTS},
            "planner_state": SAMPLE_PLANNER_STATE,
            "structured_result": SAMPLE_CONVERGENCE_STRUCTURED,
        },
        "output_hint": "自然语言文本（无需 JSON）",
    },
    {
        "index": "06",
        "slug": "path_drilldown_response",
        "phase": "path_drilldown_response",
        "skill_name": "path_drilldown",
        "title": "PathDrillDown Skill 回复",
        "description": "对强基计划做路径深挖回复",
        "trigger_scenario": '用户说"强基计划详细讲讲，我现在适合吗"时，PathDrillDown 使用此 prompt（通过 llm_polish_structured_reply）',
        "variables": {
            "skill_name": "path_drilldown",
            "user_input": "强基计划详细讲讲，我现在适合吗",
            "draft_reply": "【Draft Reply - 结构化回复草稿】强基计划适合高考成绩优秀且对基础学科有兴趣的学生...",
            "style": "xiaohongshu",
            "structured_result": {
                "candidate_count": 1,
                "matched_targets": SAMPLE_PATHS_STRUCTURED,
            },
        },
        "output_hint": "自然语言文本（润色后）",
    },
    {
        "index": "07",
        "slug": "school_intro_response",
        "phase": "school_intro_response",
        "skill_name": "school_intro",
        "title": "SchoolIntro Skill 回复",
        "description": "对合肥大学做学校介绍回复",
        "trigger_scenario": '用户说"介绍一下合肥大学"时，SchoolIntro 使用此 prompt',
        "variables": {
            "skill_name": "school_intro",
            "user_input": "合肥大学怎么样，给我介绍一下",
            "context": {"known_facts": SAMPLE_FACTS},
            "planner_state": SAMPLE_PLANNER_STATE,
            "structured_result": {
                "matched_count": 1,
                "targets": [
                    {
                        "school_name": "合肥大学",
                        "school_url": "https://hailiangm.gaokaow.cc/colleges/detail?collegeCode=10495",
                        "school_intro": "合肥大学是公办本科、安徽省属重点建设高校，2023年由合肥学院升格更名。",
                    }
                ],
            },
        },
        "output_hint": "自然语言文本（无需 JSON）",
    },
    {
        "index": "08",
        "slug": "terminate_response",
        "phase": "terminate_response",
        "skill_name": "terminate_or_recommend",
        "title": "TerminateOrRecommend Skill 回复",
        "description": "用户要求直接给推荐时的回复",
        "trigger_scenario": '用户说"直接推荐吧"时，TerminateOrRecommend 使用此 prompt',
        "variables": {
            "skill_name": "terminate_or_recommend",
            "user_input": "直接推荐吧",
            "context": {"known_facts": SAMPLE_FACTS},
            "planner_state": {**SAMPLE_PLANNER_STATE, "response_mode": "answer"},
            "structured_result": SAMPLE_TERMINATE_STRUCTURED,
        },
        "output_hint": "自然语言文本（无需 JSON）",
    },
]


def resolve_prompt_key(skill_name: str, phase: str) -> str:
    """将 skill_name 解析为正确的 prompt key"""
    mapping = {
        "admission": "admission_response",
        "convergence": "convergence_response",
        "path_drilldown": "path_drilldown_response",
        "school_intro": "school_intro_response",
        "terminate_or_recommend": "terminate_or_recommend_response",
    }
    if skill_name in mapping:
        return mapping[skill_name]
    if phase.endswith("_response") and phase != "router" and phase != "facts_extractor" and phase != "planner":
        return phase
    return skill_name


def generate_markdown(example: dict) -> str:
    prompt_key = resolve_prompt_key(example["skill_name"], example["phase"])
    spec = get_prompt_spec(prompt_key)
    prompt_content = spec.content if spec else "(未找到 prompt 定义)"

    variables_json = json.dumps(example["variables"], ensure_ascii=False, indent=2)

    lines = [
        f"# {example['title']} Prompt 示例",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 所属阶段：`{example['phase']}`",
        "",
        "## 基本信息",
        "",
        f"- **描述**：{example['description']}",
        f"- **触发场景**：{example['trigger_scenario']}",
        f"- **期望输出格式**：{example['output_hint']}",
        "",
        "## 变量值（示例）",
        "",
        "以下 JSON 是本示例中使用的变量值，模拟真实运行时传入的内容：",
        "",
        "```json",
        variables_json,
        "```",
        "",
        "## 实际 Prompt 内容",
        "",
        "以下是完整发送给 LLM 的 system prompt + user prompt 组合（变量已用上方实际值替换）：",
        "",
        "---",
        prompt_content,
        "---",
        "",
        "## Prompt 元信息",
        "",
        f"- **Prompt Key**：`{spec.key if spec else 'N/A'}`",
        f"- **Prompt Title**：`{spec.title if spec else 'N/A'}`",
        f"- **When to Use**：`{spec.when_to_use if spec else 'N/A'}`",
        f"- **Output Contract**：`{spec.output_contract if spec else 'N/A'}`",
    ]

    return "\n".join(lines)


def main() -> None:
    output_dir = Path(__file__).parent.parent / "config" / "prompts_examples"
    output_dir.mkdir(parents=True, exist_ok=True)

    for example in EXAMPLES:
        filename = f"{example['index']}_{example['slug']}_example.md"
        content = generate_markdown(example)
        output_path = output_dir / filename
        output_path.write_text(content, encoding="utf-8")
        print(f"  ✓ {output_path.relative_to(Path(__file__).parent.parent)}")

    print(f"\n✅ 已生成 {len(EXAMPLES)} 个 prompt 示例文件到 config/prompts_examples/")


if __name__ == "__main__":
    main()
