# Skill 标准与渐进式 Prompt 约定

## 目标

本项目的 runtime skill 采用“Anthropic progressive disclosure + OpenClaw / AgentSkills 目录规范”的融合方案：

- `SKILL.md` 负责声明 skill 元数据、prompt 装载策略、retrieval 策略、assets 声明、debug 声明
- `runtime_contract.json` 负责声明 stage、facts、routes、accepts_scenes 等运行时契约
- `references/` 和 `assets/` 作为 skill 目录内的本地资源
- `assets/generated/` 作为共享结构化资产根目录

## 标准目录

```text
runtime_skills/<skill_name>/
  SKILL.md
  runtime_contract.json
  references/           # 可选，知识文档
  assets/               # 可选，skill 私有轻量 assets / 索引 / 说明资源
  scripts/              # 可选，status_track / profile_op / validators
```

## `SKILL.md` Frontmatter

推荐字段：

```yaml
---
name: 升学规划顾问
skill_id: career_plan_entity
description: 可由自由问答推荐进入的生涯规划专项 Skill
version: 1.0.0
author: e生涯平台
tags: [升学规划, 新高考]
skill_type: native
entrypoint_role: specialist
accepts_scenes: []
triggers: [升学规划, 选科, 择校]
tool_policy:
  allow_tool_call_first: true
  allow_direct_answer: true
  max_tool_calls: 3
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
  supplemental_enabled: false
  sources: [references, local_assets, generated_assets]
  top_k: 4
  snippet_chars: 900
  include_catalog: true
assets:
  local_enabled: true
  local_dir: assets
  local_prompt_policy: summary
  generated_domains: [admission, multiroute]
  generated_prompt_policy: summary
debug:
  record_prompt_assembly: true
  record_retrieval_details: true
bridge:
  target_skill: ""
---
```

## `runtime_contract.json`

继续负责以下运行时契约：

- `skill_id`
- `skill_role`
- `stages`
- `facts`
- `routes`
- `accepts_scenes`
- `metadata`

原则上：

- `SKILL.md` 决定“怎么加载、怎么解释、怎么调试”
- `runtime_contract.json` 决定“怎么路由、怎么持久化状态、怎么推进阶段”

## Assets 约定

### skill 本地 assets

路径：

```text
runtime_skills/<skill>/assets/
```

适合放：

- 轻量说明文档
- 索引或映射关系
- 示例 / 模板
- 不适合进入全局共享资产库的小型资源

### 共享 generated assets

路径：

```text
assets/generated/<domain>/
```

适合放：

- admission / multiroute / school_intro 这类共享结构化大资产
- 需要跨多个 skill 复用的 JSON 资产

skill 通过 `assets.generated_domains` 显式声明依赖的共享资产域。

## 渐进式 Prompt 加载

### 分层模型

- `core`
  - skill metadata
  - skill 正文
  - session state
  - route / tool policy
- `retrieval`
  - reference catalog
  - local asset catalog
  - 命中的 references / assets snippets
- `final`
  - 最终送给模型的 prompt

### 设计原则

- 不再默认把 `references/` 整包拼进 system prompt
- 先暴露目录 / 摘要 / catalog
- 标准 `native + progressive` Skill 默认只使用 ms-agent 本轮 lazy load 命中的 references 注入 snippets
- Hailiang 自研 retrieval 只作为补充召回；需要处理更复杂、更长的 md 或 assets 时，再显式配置 `retrieval.supplemental_enabled: true`
- `assets/generated` 只注入当前 skill 声明的 domain 与本轮命中的内容

## Debug 事件

`prompt_assembly` 事件统一记录：

- `layer`
- `skill_id`
- `skill_type`
- `reference_strategy`
- `retrieved_sources`
- `retrieved_count`
- `generated_asset_domains`
- `local_asset_paths`

调试时建议：

- 先看 `core` 有没有不该出现的大段 reference 正文
- 再看 `retrieval` 是否只包含本轮命中的片段
- 最后看 `final` 是否正确组合了 core + retrieval + tool results
