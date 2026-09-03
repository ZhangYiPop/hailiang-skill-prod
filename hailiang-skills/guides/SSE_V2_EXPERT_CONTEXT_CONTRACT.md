# SSE v2：多孩子上下文与专家状态严格契约

> 生效日期：2026-09-03
>
> 入口：`POST /api/v2/sessions/chat/stream`
>
> 本文优先级高于旧示例中出现的 `input.profile_id`、顶层
> `expert_team_id` 或顶层 `expert_id`。

## 1. 基本原则

- 同一个 `session_id` 可以分别保存“未绑定孩子”、孩子 A、孩子 B 的分支。
  每个分支独立保存模型历史、Facts、当前 Skill、表单和交互卡；专家团及最后实际承接
  的专家是 session 级选择，会在切换孩子后延续。
- 本轮孩子身份只能来自 BFF 写入的 `context_data.profile_id`。`input` 中不得
  出现 `profile_id`。
- 除 `stop` 外，每个动作都必须带 `expert_context`。它是前端对当前分支状态的
  断言，也是服务端用来拒绝旧标签页覆盖新状态的并发保护。
- 浏览器不需要判断“是否第一条消息”或“目标孩子的分支是否已加载”。普通 `chat`
  默认执行无感上下文激活：服务端先切入/创建目标分支、恢复 session 级 Agent，再处理
  同一条消息。服务端返回的权威状态直接覆盖本地缓存。

## 2. 顶层请求

```json
{
  "session_id": "sess_001",
  "run_id": "run_001",
  "input": "{\"action\":\"chat\",\"context_scope\":\"profile\",\"context_activation\":\"auto\",\"content\":\"孩子最近不愿意沟通，怎么办？\",\"source\":\"chat\",\"expert_context\":{\"expert_team_id\":null,\"expert_id\":null,\"expected_branch_version\":1,\"expected_selection_version\":0,\"operation\":\"continue\"}}",
  "context_data": {
    "user_id": "user_001",
    "profile_id": "profile_001",
    "student_name": "小海"
  }
}
```

`input` 必须是 JSON 字符串。非停止动作必须有 `context_data`；停止动作不需要
`context_data` 或 `expert_context`。

### 2.1 `context_data` 的中文含义

| 字段 | 何时传 | 中文含义与规则 |
| --- | --- | --- |
| `user_id` | 所有非停止动作 | 登录用户 ID，由 BFF 注入。 |
| `profile_id` | `context_scope="profile"` | 当前孩子 ID，是本轮唯一的孩子来源。 |
| `student_name` | `profile` | 当前孩子名称，必填。 |
| `school_year`、`grade`、`facts` | `profile` 可选 | 受控的孩子上下文补充数据。 |

`context_scope="unbound"` 时，`context_data` 只能含 `user_id`；不得含孩子 ID、
名称、年级或 Facts。此范围不会读取、创建或写入孩子档案。

### 2.2 禁止的旧字段

| 旧字段 | 返回码 | 处理方式 |
| --- | --- | --- |
| `input.profile_id` | `422 INPUT_PROFILE_ID_FORBIDDEN` | 删除该字段，改用 `context_data.profile_id`。 |
| `input.expert_team_id` / `input.expert_id` | `422 LEGACY_EXPERT_FIELDS_FORBIDDEN` | 改放进 `input.expert_context`。 |
| 缺少 `input.expert_context` | `422 EXPERT_CONTEXT_REQUIRED` | 补齐下节对象。 |

## 3. `expert_context`

每个非停止动作均带：

```json
{
  "expert_team_id": "student_growth_expert_team",
  "expert_id": "career_plan_expert",
  "expected_branch_version": 12,
  "expected_selection_version": 4,
  "operation": "continue"
}
```

| 字段 | 中文含义 |
| --- | --- |
| `expert_team_id` | 当前范围正在使用的专家团 ID；无专家团时为 `null`。 |
| `expert_id` | 当前范围实际承接的专家 ID；无专家时为 `null`。 |
| `expected_branch_version` | 前端最后一次从服务端获得的该范围版本号，用于防止旧页面覆盖新状态。 |
| `expected_selection_version` | 前端最后一次获得的 session 级 Agent 选择版本。工具栏显式选择专家团/专家时必须断言，防止旧页面覆盖更新后的全局选择。 |
| `operation` | 本轮是普通续聊断言，还是用户明确选择专家团/专家。 |

服务端在每个 SSE `state`、`GET /sessions/{id}` 和
`GET /sessions/{id}/context` 中返回权威 `expert_context`：

```json
{
  "expert_context": {
    "expert_team_id": "student_growth_expert_team",
    "expert_id": "career_plan_expert",
    "branch_version": 12,
    "selection_version": 4
  }
}
```

前端按 `profile_id`（未绑定使用 `__unbound__`）分别保存这些字段。切换孩子或
恢复会话后，以服务端返回值覆盖本地状态。

### 3.1 `context_activation`：同一条请求完成孩子切换

`action="chat"` 可选传 `context_activation`：

| 值 | 中文含义 | 适用场景 |
| --- | --- | --- |
| `auto`（默认） | 服务端在本轮需要新建 session、创建目标孩子分支或切换孩子分支时，先完成激活并恢复 session 级最后活跃 Agent，然后直接回答本条消息。 | 业务前端/BFF 固定使用，无需预检接口或重发。 |
| `strict` | 拒绝会导致范围切换的请求，返回 `409 CONTEXT_ACTIVATION_REQUIRED`。 | 需要完全禁止隐式切换的管理端或批处理调用。 |

首次进入新孩子分支时，若 session 已选择专家团或专家，服务端沿用该选择（包括已确认
转交的成员专家），但只使用新孩子自己的历史和 Facts 来重新决定 Skill。普通聊天没有
session 级 Agent 时，仍是 `general_chat + Soul`，不会自动开启专家团。自动激活首帧的
`state` 包含 `context_switched`、`context_activation:"auto"`、权威 `expert_context` 与
当前 Skill。孩子分支原有、但属于不同 Agent 的表单/转交卡/路径卡会过期并保持历史只读。

首次创建有名字的孩子范围，或切换到有名字的孩子时，首个 `state.context_notice` 还会返回
`text: "本轮回答将结合 **孩子名** 的档案数据。"`。这是供前端 Markdown 渲染的系统提示，
不能拼入模型回答或下一轮 `content`；孩子名称为空或仅空白时，该字段保持 `{}`。

## 4. 三种普通聊天操作

### 4.1 续聊：`operation="continue"`

用于主协调专家、成员专家、单专家或普通聊天的所有继续追问：

```json
{
  "action": "chat",
  "context_scope": "profile",
  "content": "继续说说具体怎么做。",
  "source": "chat",
  "expert_context": {
    "expert_team_id": "student_growth_expert_team",
    "expert_id": "family_education_expert",
    "expected_branch_version": 12,
    "operation": "continue"
  }
}
```

同一孩子范围内，团队、专家和版本必须与服务端当前分支一致；成功时不改变专家状态。
跨孩子的首条 `auto` 聊天不做旧分支断言，而是返回目标范围的权威状态并直接执行。

### 4.2 用户选择专家团：`operation="select_team"`

仅当用户在 UI 中明确选择或切换专家团时使用。服务端激活该团主协调专家。

```json
{
  "action": "chat",
  "context_scope": "profile",
  "content": "请从学生成长角度分析。",
  "source": "chat",
  "expert_context": {
    "expert_team_id": "student_growth_expert_team",
    "expert_id": null,
    "expected_branch_version": 12,
    "operation": "select_team"
  }
}
```

### 4.3 用户选择专家：`operation="select_expert"`

没有专家团时，进入单专家模式；已有专家团时，目标专家必须属于当前团队。要切换团队，
先发 `select_team`，不能借 `select_expert` 跨团切换。

```json
{
  "action": "chat",
  "context_scope": "unbound",
  "content": "我想直接咨询家庭教育问题。",
  "source": "chat",
  "expert_context": {
    "expert_team_id": null,
    "expert_id": "family_education_expert",
    "expected_branch_version": 1,
    "operation": "select_expert"
  }
}
```

`enter_skill`、`quit_skill`、`switch_team_member`、`confirm_team_handoff` 也必须带
`expert_context`，且其 `operation` 固定为 `continue`：它们不能顺带改变专家团选择。

## 5. 转交卡与工具栏成员切换

- 主协调专家给出转交卡后，点击卡片使用 `confirm_team_handoff`，并传
  `source_message_id`、`target_expert_id` 和卡片出现时的 `expert_context`。
- 用户在当前团队工具栏指定成员并提问时，使用 `switch_team_member`，并传
  `target_expert_id`、`content` 和当前 `expert_context`。
- 两类动作都会校验范围、团队成员资格、卡片有效期及 `expected_branch_version`；
  不能跨孩子、跨未绑定分支或重复提交。

## 6. 过期状态与错误处理

旧标签页、并发请求或已切换专家后的续聊，可能返回：

```json
{
  "code": "EXPERT_CONTEXT_STALE",
  "message": "专家上下文已更新，请使用最新会话状态继续。",
  "detail": {
    "code": "EXPERT_CONTEXT_STALE",
    "details": {
      "expert_context": {
        "expert_team_id": "student_growth_expert_team",
        "expert_id": "family_education_expert",
        "branch_version": 13
      }
    }
  }
}
```

前端应保存 `details.expert_context`、刷新该范围的会话详情并提示用户；**不得自动重放**
原消息、不得自动切换专家。

## 7. `stop` 的可靠返回

停止请求只复用当前活动 `run_id`：

```json
{
  "session_id": "sess_001",
  "run_id": "run_001",
  "input": "{\"action\":\"stop\",\"source\":\"composer\"}"
}
```

即使模型尚未返回首字、已返回部分正文、正在由主协调专家回答或成员专家回答，最后一个
`state` 与 `done` 都会包含 `status="stopped"`、已收到的
`assistant.content`、原样的 `expert`、`expert_context`、`session.active_skill`、
`context_scope` 和 `branch_version`。前端保留这些状态和部分回答，结束 loading；下一轮
使用 stopped state 的 `expert_context` 创建新的 `run_id` 继续即可。

## 8. SSE 前端消费规则

`state` 是完整快照，`done` 是最后快照的传输确认。对同一个 `run_id` 只接收递增 `seq`。
收到 `stopped`、`completed`、`failed`、`blocked` 或 `superseded` 时结束 loading。模型上下文
始终只读取当前范围分支；主页面可展示整个带范围标签的时间线，但不能把其他范围历史送回模型。

## 9. 正式聊天页与 AI 业务调试台的状态对齐

正式聊天页使用本接口；AI 业务调试台的“候选修订手动测试”仍使用
`/workbench/v1/revision-tests/{debug_session_id}/turns/stream`。两者的鉴权、会话 ID 和配置来源不同：

| 场景 | 配置与身份来源 | `context_scope` | 前端状态语义 |
| --- | --- | --- | --- |
| 正式聊天 | 已发布部署、登录用户及孩子档案 | `profile` 或 `unbound` | 本文的原始 SSE v2 `state`。 |
| 候选修订手动测试 | 当前调试会话的不可变候选快照、匿名调试身份 | 固定 `unbound` | 工作台流也发送 `protocol="hailiang.sse.v2"` 的同形 `state`。 |

候选测试不得读取、创建或写入孩子档案、正式会话或生产版本；但它必须和正式页使用同一份
消息投影语义：`assistant`、`form`、`team_handoff`、`expert`、`expert_context`、当前 Skill、
错误及终态。前端应把两类流统一投影到同一套 Markdown、表单、专家转交卡和状态时间线组件，
不要另行解析模型文本来猜测表单或专家状态。

### 9.1 专家转交确认的历史事件

用户点击主协调专家的转交卡后，服务端会保存一条可见时间线事件：

```json
{
  "role": "user",
  "content": "@家庭教育专家",
  "message_type": "team_handoff_confirmation",
  "metadata": {
    "source_message_id": "msg_handoff_001",
    "target_expert_id": "family_education_expert",
    "expert_team_id": "student_growth_expert_team",
    "source": "team_handoff"
  }
}
```

其中文含义如下：

| 字段 | 含义 |
| --- | --- |
| `content` | 供用户和历史界面阅读的“已确认由某专家接管”标记。 |
| `message_type` | 固定为 `team_handoff_confirmation`；前端渲染为居中的确认事件，不能当作普通用户气泡。 |
| `source_message_id` | 原专家转交卡所在的助手消息 ID。 |
| `target_expert_id` / `expert_team_id` | 已由服务端校验过的目标成员和所属专家团。 |

该事件必须出现在正式页、候选区、完整证据 JSON、纯对话 JSON 以及 BFF 持久化的历史时间线中。
但它**不是**下一轮 `chat.content`，也不得进入模型消息历史；目标专家收到的是原始用户问题和
结构化转交说明。恢复历史时，先恢复此可见事件与卡片的 `selected` 状态，再以服务端返回的
`expert_context` 恢复当前专家，不要通过解析 `@家庭教育专家` 文本推断路由。

### 9.2 候选流的错误与结束

候选流中的 `done` 同样只是最终 `state` 的传输确认。候选测试停止使用独立接口
`POST /workbench/v1/revision-tests/{debug_session_id}/turns/{run_id}/stop`（仅带 `actor_id`）；
原流的最终 `state` 与 `done` 会以 `status="stopped"` 返回部分回答、当前专家、当前 Skill 和
候选 `expert_context`，前端不可用浏览器中断来代替这次服务端停止。若候选 Runtime 出错，工作台会先发送
`state.status="failed"`，其中保留已收到的 `assistant.content`、当前 `expert`、
`expert_context` 与当前 Skill，然后发送 `error`。前端据此保留部分回答和状态，结束 loading，
不得重放该请求。候选测试不共享正式会话的 `stop` endpoint；正式聊天的可靠停止契约仍按第 7 节执行。
