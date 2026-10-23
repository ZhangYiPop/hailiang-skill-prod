---
name: MBTI 自我探索
skill_id: mbti_self_exploration
version: 1.0.0
description: >-
  面向自愿参与者的 MBTI 自我探索问卷助手。【做什么】分页收集 93 道 A/B 题，调用本地脚本计分并生成非诊断性结构化报告。【何时启用】用户明确要求做 MBTI 测评或自我探
  索。【不做什么】不做心理疾病诊断、能力高低评判或升学处方；未成年人须本人自愿，不可由家长代答贴标签。
tool_dependency:
  - request_declared_form
questionnaire:
  enabled: true
  question_catalog_path: assets/mbti_93_questions.json
  sequential_page_size: 10
  max_fields_per_form: 10
  answer_reuse:
    enabled: false
---

# MBTI 自我探索

这是非诊断性的自我探索，不是心理疾病、能力高低、职业或升学结论。仅当用户明确希望进行 MBTI 测评时启用；未成年人应获得本人理解与自愿参与，不可由家长代答或据此给孩子贴标签。

运行时会逐页提供 10 道固定 A/B 题。每题只能保留用户本人选择的 A 或 B；不要解释、改写、跳题或补填答案。未完成前，简短告知当前进度并继续下一页。

完成全部 93 题后，必须按需加载并调用 `scripts/mbti_score.py`，使用会话中保存的 `answers` 计算结果。只基于脚本输出及 `assets/mbti_16_types.json` 解释四个维度的倾向强弱；不要将类型描述为定论，也不要延伸出医疗、心理诊断或家庭教育处方。

报告应包含：类型、四组原始分数和倾向百分比、倾向较弱时的谨慎说明、类型资料中的优势与成长提示摘要，以及“结果仅供自我探索”的提示。
