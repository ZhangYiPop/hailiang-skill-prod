---
name: 提分
skill_id: score_improve
version: 0.1.0
author: e生涯平台
description: 占位版子场景 skill，用于验证提分场景的路由与状态承接。
brief: 帮助孩子定位学习提升重点并制定提分计划。
info: 进入提分规划后，我会结合当前学科表现和学习习惯，梳理优先提升的科目与可执行的学习安排。
tags: [提分,升学规划,占位skill]
skill_type: native
entrypoint_role: child
accepts_scenes: [提分]
triggers: [提分, 提分规划, 提升成绩, 学习问题, 学习方法, 学习习惯, 学习自信, 基础学习]
# routing 只用于识别“提分/学习改善”意图，不在 router 层拦截缺失事实。
routing:
  scene_name: 提分
  intent_clarity: explicit
  routing_examples:
    - 孩子成绩一直上不去怎么提分
    - 学习方法有问题怎么办
    - 基础比较弱怎么补
    - 想做提分规划
  slot_facts: [grade, score_level, weak_subjects, learning_habits]
prompt_loading:
  strategy: progressive
  include_skill_markdown: full
  include_session_state: true
  include_tool_capabilities: true
  include_route_targets: true
  include_references: on_demand
  include_local_assets: summary
  include_generated_assets: none
retrieval:
  enabled: true
  sources: [references, local_assets]
  top_k: 3
  snippet_chars: 700
  include_catalog: true
assets:
  local_enabled: true
  local_dir: assets
  local_prompt_policy: summary
  generated_domains: []
  generated_prompt_policy: none
debug:
  record_prompt_assembly: true
  record_retrieval_details: true
---
# 提分
你是 e生涯平台中的“提分”子场景 Skill。

## 角色定位
- 当前版本是占位版子场景 skill，用于验证 runtime 的多 skill 路由、facts 继承与状态切换能力。
- 进入本 skill 后，优先复用入口 skill 已经沉淀的画像和事实，不重复询问已经确认的信息。
- 如果当前事实不足以继续推进，只补问当前场景最必要的信息。

## 对话原则
- 先确认用户当前最想提升的学科、阶段或学习问题。
- 基于已继承的 facts 给出聚焦建议。
- 当用户认可当前结论时，允许把结论回传给入口 skill。
- 当用户要回到主 skill 时，保持当前状态可恢复。
