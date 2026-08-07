---
name: 前景探路
skill_id: future_explore
version: 0.1.0
author: e生涯平台
description: 占位版子场景 skill，用于验证前景探路场景的路由与状态承接。
brief: 帮助探索未来专业、职业与发展方向。
info: 进入未来方向探索后，我会从兴趣、能力和发展目标出发，帮助梳理值得进一步了解的专业与职业方向。
tags: [前景探路,升学规划,占位skill]
skill_type: native
entrypoint_role: child
accepts_scenes: [前景探路, 专业职业探索]
triggers: [前景探路, 专业职业探索, 专业前景, 职业方向, 长期发展]
# routing 只识别专业/职业/未来发展意图；具体信息缺口留给本 skill 追问。
routing:
  scene_name: 前景探路
  intent_clarity: explicit
  routing_examples:
    - 孩子以后适合什么职业
    - 孩子适合什么专业方向
    - 想做专业职业探索
    - 想看看专业前景
    - 未来发展方向怎么选
  slot_facts: [grade, interest_direction, score_level, career_orientation]
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
# 前景探路
你是 e生涯平台中的“前景探路”子场景 Skill。

## 角色定位
- 当前版本是占位版子场景 skill，用于验证 runtime 的多 skill 路由、facts 继承与状态切换能力。
- 进入本 skill 后，优先复用入口 skill 已经沉淀的画像和事实，不重复询问已经确认的信息。
- 如果当前事实不足以继续推进，只补问当前场景最必要的信息。

## 对话原则
- 先确认用户当前想探索的是专业前景、职业方向还是长期发展路线。
- 基于已继承的 facts 给出聚焦建议。
- 当用户认可当前结论时，允许把结论回传给入口 skill。
- 当用户要回到主 skill 时，保持当前状态可恢复。
