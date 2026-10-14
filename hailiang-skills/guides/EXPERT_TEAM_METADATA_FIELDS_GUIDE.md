# 专家团框架元数据字段说明

本文说明 Skill、Expert（专家）和 Expert Team（专家团）的常用元数据，以及这些字段在当前系统中的实际作用。

## 1. 对象关系

```
专家团 Team
  ├── 主协调专家 Agent
  │     ├── Skill A
  │     └── Skill B
  └── 成员专家 Agent
        └── Skill C
```

- 专家团决定由哪位专家承接问题。
- 专家根据 `AGENT.md` 决定直接回答，还是调用自己锁定的 Skill。
- Skill 根据 `SKILL.md` 执行业务流程、表单、脚本和资料引用。
- 平台通过发布版本和依赖锁校验实际可执行内容。

专家团本身不直接绑定 Skill，Skill 绑定在专家上。

## 2. Skill 字段

Skill 的业务权威是 `SKILL.md`。YAML front matter 主要描述身份、能力范围和运行配置。

| 字段 | 当前是否使用 | 作用 |
| --- | --- | --- |
| `skill_id` | 是 | 稳定唯一 ID，用于依赖锁、路由和 API |
| `name` | 是 | 面向业务人员展示的名称 |
| `version` | 是 | Skill 发布版本 |
| `author` | 是 | 创建或维护人员 |
| `description` | 是 | 能做什么，以及不适合做什么 |
| `brief` | 是 | 简短能力介绍，用于目录和路由卡片 |
| `info` | 兼容/辅助 | 补充说明，不是唯一业务规则来源 |
| `tags` | 是 | 路由辅助标签，不是强制触发条件 |
| `skill_type` | 是 | 运行时类型，普通 Skill 通常为 `native` |
| `entrypoint_role` | 是 | Skill 在专家中的入口角色 |

示例：

```yaml
skill_id: study_abroad_cost_calculator
name: 留学费用计算器
version: r3
author: 业务人员
description: >
  根据国家、地区、院校类型、专业和预算进行留学费用测算。
  不负责院校选择、专业推荐或申请时间规划。
brief: 留学费用测算
tags:
  - 留学费用
  - 预算测算
  - 成本估算
skill_type: native
```

### `tags` 的用途

`tags` 当前会被使用，主要用于：

1. 生成专家可见的 Skill 能力卡片；
2. 作为专家路由模型的辅助信息；
3. 参与没有明确工具调用时的候选 Skill 快速预筛；
4. 写入调试轨迹和路由审计。

标签不能单独决定调用 Skill。比如用户询问“英国哪所大学适合学航空航天”，不能因为命中“留学”或“英国”就进入费用计算 Skill。最终仍需结合 `AGENT.md`、`description`、`SKILL.md` 的适用范围和当前上下文。

建议使用核心能力短语：

```yaml
tags:
  - 留学费用
  - 预算测算
  - 成本估算
```

不建议堆放“留学、英国、美国、大学、学校、专业、申请”等泛化词。

### `skill_type` 的用途

`skill_type` 是运行时类型标记，不是业务分类，也不是路由触发词。

当前 `skill_type: native` 会影响：

- Runtime Skill 执行链路；
- Native Skill 的 Prompt 组装和会话状态；
- Native Questionnaire、表单、脚本和 Reference 流程；
- 本地脚本运行环境预热；
- Native Skill 的资料加载和结果处理。

加载器默认值也是 `native`，因此普通 Skill 不填写时通常仍按 Native Skill 处理。但建议显式写出，便于维护和未来扩展。不要写成 `skill_type: 留学` 或 `skill_type: 费用计算`，业务分类应放在 `description`、`tags` 和 `SKILL.md`。

### Skill 方法论

以下内容应写在 `SKILL.md` 正文中：

| 内容 | 作用 |
| --- | --- |
| 业务阶段 | 采集、分析、推荐或结论阶段 |
| 前置条件 | 进入下一阶段前必须具备的信息 |
| 提问规则 | 一次问什么，哪些问题不能提前问 |
| 表单规则 | 表单何时出现、字段如何条件渲染 |
| 脚本规则 | 什么条件下调用哪个脚本 |
| Reference 规则 | 什么阶段必须读取哪些资料 |
| 结论门禁 | 信息不完整时禁止输出的结论 |
| 异常处理 | 脚本失败、资料缺失或回答模糊时如何处理 |

## 3. 专家 Agent 字段

专家使用：

```
agent.yaml
AGENT.md
skills.lock.json
```

### `agent.yaml`

```yaml
schema_version: 1
topology: single_expert
id: study_abroad_consultant
name: 留学咨询师
rule_file: AGENT.md
brief: 负责留学方向咨询
skills:
  - study_abroad_cost_calculator
budget:
  max_iters: 4
  max_skill_calls: 3
capabilities:
  - execute_skill
  - request_declared_form
  - read_effective_facts
```

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 配置格式版本 |
| `topology` | 专家通常为 `single_expert` |
| `id` | 专家稳定 ID |
| `name` | 专家展示名称 |
| `rule_file` | 通常为 `AGENT.md` |
| `brief` | 专家能力简介 |
| `skills` | 专家绑定的 Skill ID 列表 |
| `budget.max_iters` | 最大推理轮次 |
| `budget.max_skill_calls` | 单轮最大 Skill 调用次数 |
| `capabilities` | 允许使用的运行能力 |

### `skills.lock.json`

```json
{
  "skills": [
    {
      "skill_id": "study_abroad_cost_calculator",
      "version": "r3"
    }
  ]
}
```

它锁定专家实际允许使用的 Skill 及发布版本。运行时校验链路为：

```
agent.yaml 中的 Skill
  → skills.lock.json 中的 Skill 和版本
  → 数据库当前发布版本
```

### `AGENT.md`

`AGENT.md` 包含 YAML front matter 和 Markdown 正文两层策略：

```md
---
skill_routing:
  rules:
    - skill_id: study_abroad_cost_calculator
      when:
        - 用户明确询问留学费用、预算或成本测算
      priority: 10
---

# 专家职责

当用户明确需要费用测算时，调用留学费用计算 Skill。
当用户询问院校选择或专业方向时，由专家直接回答。
```

`priority` 只是排序和冲突判断的辅助信号，不是硬触发机制。正文仍可描述复杂判断、追问、例外和直答规则。

## 4. 专家团 Agent Team 字段

专家团使用：

```
team.yaml
TEAM.md
```

专家团不直接拥有 Skill，Skill 配置在成员专家中。

### `team.yaml`

```yaml
schema_version: 1
topology: team
id: student_growth_expert_team
name: 学生成长专家团
rule_file: TEAM.md
brief: 为学生成长和升学问题提供综合咨询
coordinator_expert_id: education_coordinator
members:
  - expert_id: education_coordinator
    mention_name: 升学协调专家
    routing_brief: 负责识别问题并选择合适专家
  - expert_id: study_abroad_consultant
    mention_name: 留学咨询师
    routing_brief: 负责留学国家、院校、专业和费用咨询
```

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 配置格式版本 |
| `topology` | 必须为 `team` |
| `id` | 专家团稳定 ID |
| `name` | 专家团展示名称 |
| `rule_file` | 通常为 `TEAM.md` |
| `brief` | 专家团简介 |
| `coordinator_expert_id` | 主协调专家 ID |
| `members` | 专家团成员列表 |
| `members[].expert_id` | 已存在的专家 ID |
| `members[].mention_name` | 前端展示名称 |
| `members[].routing_brief` | 帮助主协调专家选择成员 |

`TEAM.md` 负责团队级协调策略：选择专家、澄清问题、展示转交卡片，以及主协调专家何时直接回答。

## 5. 运行时和发布字段

以下字段由平台维护，业务人员通常不应手工编辑：

| 字段 | 说明 |
| --- | --- |
| `object_id` | 平台数据库对象 ID |
| `object_key` | 业务对象稳定标识 |
| `revision_id` | 可编辑修订版本 ID |
| `revision_no` | 修订序号 |
| `release_id` | 已发布版本 ID |
| `release_no` / `version` | 已发布版本号 |
| `content_hash` | 内容哈希 |
| `created_by` / `published_by` | 创建人 / 发布人 |
| `modified_at` | 修改时间 |
| `deployment_id` | 部署记录 ID |
| `package_hash` | 发布包哈希 |
| `environment` | 部署环境 |

| ID | 含义 |
| --- | --- |
| `skill_id` / `agent_id` / `team_id` | 包和业务配置中的稳定标识 |
| `object_id` | 平台内部对象 ID |
| `revision_id` | 可编辑修订版本 |
| `release_id` | 不可变的已发布版本 |
| `session_id` | 一次会话 |
| `turn_id` | 会话中的一轮对话 |

路由和执行应使用已发布版本，不应直接使用未发布的 `revision_id`。

## 6. 策略优先级

```
TEAM.md
  ↓ 选择哪个专家承接
AGENT.md
  ↓ 决定专家直答还是调用哪个已授权 Skill
SKILL.md
  ↓ 决定具体业务流程、表单、脚本和结论
发布锁与运行时校验
  ↓ 决定实际可执行的 release/version
```

核心原则：

- `tags` 只能辅助发现候选 Skill，不能覆盖专家和 Skill 的语义判断；
- `skill_type` 只表示运行时执行类型，不能代替业务分类；
- `AGENT.md` 决定专家如何统筹自己的 Skill；
- `SKILL.md` 决定 Skill 内部的方法论；
- 专家不能调用未在 `skills.lock.json` 中锁定的 Skill；
- 专家团不能越过专家权限直接调用 Skill。

