---
name: e生涯 初中多元路径规划
skill_id: junior_multi_path_planning
description: 面向初中阶段的多元路径规划 runtime 原生 skill。
brief: 帮助初中阶段家庭了解多元升学方向与准备节奏。
info: 进入初中多元路径规划后，我会结合孩子所在年级和特点，梳理适合提前了解的路径及关键准备节点。
skill_type: native
entrypoint_role: child
accepts_scenes: [初中多元路径规划]
triggers: [初中多元路径规划, 初中多元路径, 中考保底路径, 职教, 普职融通]
# routing 只识别初中多元路径意图；年级、成绩、特长等缺口由本 skill 继续收集。
routing:
  scene_name: 初中多元路径规划
  intent_clarity: explicit
  routing_examples:
    - 初中孩子有美术特长升学有什么路径
    - 初二除了普通中考还有什么路
    - 中考保底升学通道有哪些
    - 初中多元路径怎么规划
  slot_facts: [grade, score_level, talent]
  school_stage_scope: junior
prompt_loading:
  strategy: progressive
  include_skill_markdown: full
  include_session_state: true
  include_tool_capabilities: true
  include_route_targets: true
  include_references: on_demand
  include_local_assets: summary
  include_generated_assets: summary
retrieval:
  enabled: true
  sources: [references, local_assets, generated_assets]
  top_k: 3
  snippet_chars: 800
  include_catalog: true
assets:
  local_enabled: true
  local_dir: assets
  local_prompt_policy: summary
  generated_domains: [multiroute]
  generated_prompt_policy: summary
debug:
  record_prompt_assembly: true
  record_retrieval_details: true
---

# e生涯 初中多元路径规划

You are the dedicated runtime skill for junior-high multi-path planning.

Use this scene when the student is in junior-high school and the user asks
about alternative routes beyond the default exam path, such as:

- 多元路径
- 初中多元路径
- 中考保底路径
- 特长生通道
- 竞赛 / 综评前置规划
- 职教 / 3+2 / 普职融通

Keep the response focused on junior-high planning semantics instead of the
existing senior-high `multi_path_planning` bridge.
