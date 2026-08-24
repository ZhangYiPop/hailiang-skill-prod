---
name: MBTI 自我探索
skill_id: mbti_self_exploration
version: 1.0.0
author: Hailiang Platform
description: 基于 93 道 A/B 题的 MBTI 自我探索问卷，分页收集答案，使用本地 Python 计分并生成结构化报告。
brief: 用 93 道题做一次非诊断性的自我探索。
tags: [MBTI, 人格探索, 自我认知, 家庭教育]
skill_type: native
entrypoint_role: child
accepts_scenes: [MBTI人格探索, MBTI测评]
triggers: [测一下我的MBTI, 做个MBTI测试, MBTI测评, 人格测试, 性格测试, 我是什么人格]
tool_policy:
  allow_tool_call_first: true
  allow_direct_answer: true
  max_tool_calls: 1
prompt_loading:
  strategy: progressive
  include_skill_markdown: full
  include_session_state: true
  include_tool_capabilities: true
  include_route_targets: false
  include_references: on_demand
  include_local_assets: summary
  include_generated_assets: none
retrieval:
  enabled: true
  sources: [local_assets]
  top_k: 2
  snippet_chars: 700
  include_catalog: true
assets:
  local_enabled: true
  local_dir: assets
  local_prompt_policy: summary
  generated_domains: []
debug:
  record_prompt_assembly: true
  record_retrieval_details: true
routing:
  scene_name: MBTI 人格探索
  intent_clarity: explicit
  routing_examples: [测一下我的MBTI, 做个MBTI测试, 我是什么人格类型]
questionnaire:
  enabled: true
  question_catalog_path: assets/mbti_93_questions.json
  sequential_page_size: 10
  max_fields_per_form: 10
  answer_reuse:
    enabled: false
requires:
  tools: []
  env: []
---
# MBTI 自我探索

这是非诊断性的自我探索，不是心理疾病、能力高低、职业或升学结论。仅当用户明确希望进行 MBTI 测评时启用；未成年人应获得本人理解与自愿参与，不可由家长代答或据此给孩子贴标签。

运行时会逐页提供 10 道固定 A/B 题。每题只能保留用户本人选择的 A 或 B；不要解释、改写、跳题或补填答案。未完成前，简短告知当前进度并继续下一页。

完成全部 93 题后，必须按需加载并调用 `scripts/mbti_score.py`，使用会话中保存的 `answers` 计算结果。只基于脚本输出及 `assets/mbti_16_types.json` 解释四个维度的倾向强弱；不要将类型描述为定论，也不要延伸出医疗、心理诊断或家庭教育处方。

报告应包含：类型、四组原始分数和倾向百分比、倾向较弱时的谨慎说明、类型资料中的优势与成长提示摘要，以及“结果仅供自我探索”的提示。
