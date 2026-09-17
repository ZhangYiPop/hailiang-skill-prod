# SSE v2：多孩子上下文与专家状态严格契约

> 生效日期：2026-09-08
>
> 入口：`POST /api/v2/sessions/chat/stream`
>
> 本文优先级高于旧示例中出现的 `input.profile_id`、顶层
> `expert_team_id` 或顶层 `expert_id`。

## 1. 基本原则

- 同一个 `session_id` 可以分别保存“未绑定孩子”、孩子 A、孩子 B 的分支。
  每个分支独立保存模型历史、Facts、当前 Skill、表单和交互卡；专家团及最后实际承接
  的专家通常是 session 级选择，会在切换孩子后延续。例外是跨孩子点击专家转交卡：卡片
  只在目标孩子分支绑定被选专家，不改变其他孩子或 session 级选择。
- 本轮孩子身份只能来自 BFF 写入的 `context_data.profile_id`。`input` 中不得
  出现 `profile_id`。
- 除 `stop` 外，每个动作都必须带 `expert_context`，且**固定同时包含**
  `expert_team_id`、`expert_id`、`operation` 三个键。普通 `chat + continue` 固定传两个
  `null`，由服务端继承权威状态。工具栏选择和专家转交卡同样携带完整对象。
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

`expected_branch_version` 和 `expected_selection_version` 对请求方可省略。省略时，服务端会在完成
当前 session/profile 分支恢复后，使用对应的权威版本继续处理；响应中的 `state.expert_context` 仍会返回完整版本。
如果普通追问使用 `null/null/continue`，服务端不把可选版本字段当作断言，而是继承当前 session
的权威承接者。只有请求显式携带团队与专家 ID 的 `continue` 才会严格校验专家与版本；显式旧值返回
`409 EXPERT_CONTEXT_STALE`。

普通追问固定使用 `null/null/continue`；无论用户刚恢复历史、换了设备还是切换到另一位孩子，服务端都会
延续当前 session 的专家团/专家。若当前 session 未选择专家团或专家，才使用 `general_chat + Soul`：

```json
{
  "action": "chat",
  "context_scope": "profile",
  "content": "一年级，男孩，杭州",
  "source": "chat",
  "expert_context": {
    "expert_team_id": null,
    "expert_id": null,
    "operation": "continue"
  },
  "enable_thinking": false,
  "return_reasoning": false
}
```

若缺少 `expert_team_id`、`expert_id`、`operation` 中任意一个键，服务端返回
`422 EXPERT_CONTEXT_FIELDS_REQUIRED`。普通追问中前两个字段固定都为 `null`；它表示继承而不是退出
专家模式。只传一个具体 ID 会返回
`422 EXPERT_CONTEXT_OPERATION_INVALID`。

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
| `expert_context` 缺少固定三字段之一 | `422 EXPERT_CONTEXT_FIELDS_REQUIRED` | 同时传 `expert_team_id`、`expert_id`、`operation`；无值用 `null`，不能省略。 |

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
| `expert_team_id` | 必传。`continue` 时与 `expert_id` 同为 `null` 表示继承；具体值仅用于严格断言或显式选择。 |
| `expert_id` | 必传。`continue` 时与 `expert_team_id` 同为 `null` 表示继承；`select_expert` 时是目标专家。 |
| `expected_branch_version` | 前端最后一次从服务端获得的该范围版本号，用于防止旧页面覆盖新状态。 |
| `expected_selection_version` | 前端最后一次获得的 session 级 Agent 选择版本。工具栏显式选择专家团/专家时必须断言，防止旧页面覆盖更新后的全局选择。 |
| `operation` | 必传。支持 `continue`、`select_team`、`select_expert`、兼容别名 `select_team_member` 和显式退出 `clear_expert`。 |

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

## 4. 按场景发送请求：会话、孩子、专家团与专家

先记住唯一的孩子切换规则：**`input` 从不携带 `profile_id`；顶层
`context_data.profile_id` 与当前范围不同，就表示切换孩子。** 前端不需要判断目标孩子是
第一次还是第 N 次进入。`context_activation="auto"` 会在同一条请求中切入或创建该孩子分支。

下表的 `EC` 指 `input.expert_context`。除停止以外，所有请求都必须有 `EC`。

| 场景 | 顶层 `context_data` | `input` 的关键字段 | `EC` | 结果与边界 |
| --- | --- | --- | --- | --- |
| 新会话，未绑定孩子 | `{"user_id":"u1"}` | `chat`、`context_scope:"unbound"`、`source:"chat"` | `{"expert_team_id":null,"expert_id":null,"operation":"continue"}` | 建立/恢复未绑定分支；不会读写孩子档案，也不会自动选专家。 |
| 新会话或当前会话绑定孩子 | `user_id` + `profile_id` + `student_name` | `chat`、`context_scope:"profile"`、`context_activation:"auto"` | `{"expert_team_id":null,"expert_id":null,"operation":"continue"}` | 建立/切入该孩子分支；未选专家时由通用对话处理。 |
| 绑定孩子并选择专家团 | 同上，`profile_id` 为目标孩子 | `action:"chat"`、`source:"chat"` | `expert_team_id` + `expert_id:null` + `operation:"select_team"` | 激活团队主协调专家。不能在此操作同时指定成员。 |
| 绑定孩子并直接选择团队成员 | 同上 | `action:"chat"`、`source:"toolbar"` | `expert_team_id` + 团内 `expert_id` + `operation:"select_expert"` | 原子激活团队并选成员；适用于首次进入、再次进入或切换孩子。 |
| 未绑定孩子的单专家 | 仅 `user_id` | `action:"chat"`、`context_scope:"unbound"`、`source:"chat"` | `expert_team_id:null` + `expert_id` + `operation:"select_expert"` | 进入单专家模式。当前已有专家团时，不能省略团队 ID 来改选团队成员。 |
| 已有任何状态，普通继续对话 | 当前目标范围的 `context_data` | `action:"chat"`、`source:"chat"` | `expert_team_id:null` + `expert_id:null` + `operation:"continue"` | 服务端继承当前专家选择；不改变专家/专家团。 |

### 4.1 会话开始与首次绑定孩子

未绑定孩子的首聊示例：

```json
{
  "session_id": "sess_001",
  "run_id": "run_unbound_001",
    "input": "{\"action\":\"chat\",\"context_scope\":\"unbound\",\"content\":\"我想先了解升学规划。\",\"source\":\"chat\",\"expert_context\":{\"expert_team_id\":null,\"expert_id\":null,\"operation\":\"continue\"}}",
  "context_data": {"user_id": "user_001"}
}
```

绑定孩子的首聊示例。`profile_id` 位于顶层 `context_data`，不是 JSON 字符串 `input` 的字段：

```json
{
  "session_id": "sess_001",
  "run_id": "run_profile_a_001",
    "input": "{\"action\":\"chat\",\"context_scope\":\"profile\",\"context_activation\":\"auto\",\"content\":\"孩子最近不愿意沟通。\",\"source\":\"chat\",\"expert_context\":{\"expert_team_id\":null,\"expert_id\":null,\"operation\":\"continue\"}}",
  "context_data": {"user_id": "user_001", "profile_id": "child_a", "student_name": "小海"}
}
```

首次就选团队成员，只把 `EC` 改为下列对象；不需要先发 `select_team`，也不要先让协调专家回答：

```json
{
  "expert_team_id": "student_growth_expert_team",
  "expert_id": "academic_coach",
  "operation": "select_expert"
}
```

### 4.2 不切换孩子时：继续、工具栏切换与转交卡切换

#### 什么都不选，只继续对话

普通 `continue` 固定传 `expert_team_id:null` 与 `expert_id:null`。版本字段也无需传；只有明确选择
专家或确认交互时才使用权威 state/卡片中的具体状态，不要猜测 `1/0`。

```json
{
  "action": "chat",
  "context_scope": "profile",
  "content": "继续说说具体怎么做。",
  "source": "chat",
  "expert_context": {"expert_team_id": null, "expert_id": null, "operation": "continue"}
}
```

#### 在当前专家团的工具栏中改选成员并提问

这不是 `chat + select_expert`，而是动作 `switch_team_member`。`target_expert_id` 是**要切换到的**
成员；`EC.expert_id`（如携带）是**切换前当前成员**的状态断言。因此这两个字段可以同时出现，
但值通常不同。

```json
{
  "action": "switch_team_member",
  "target_expert_id": "academic_coach",
  "content": "孩子不想上幼儿园，怎么沟通？",
  "source": "toolbar",
  "context_scope": "profile",
  "expert_context": {
    "expert_team_id": "student_growth_expert_team",
    "expert_id": "e_career_planner",
    "expected_branch_version": 12,
    "expected_selection_version": 4,
    "operation": "continue"
  }
}
```

该动作只能在当前团队已激活、目标专家属于该团队时使用；否则分别可能返回
`409 EXPERT_TEAM_NOT_ACTIVE` 或 `422 EXPERT_NOT_IN_ACTIVE_TEAM`。若当前范围存在未提交原生表单，
服务端不会阻断切换：会先将旧表单标记为 `expired`、清除其待完成问卷与活动 Skill 状态，并记录
`form_abandoned` 审计事件（含表单、来源消息、原/目标专家和切换原因）。旧表单保持历史只读，不能再提交。

#### 点击专家转交卡

使用 `confirm_team_handoff`，不能自行将卡片的专家改写为 `EC.expert_id`。`target_expert_id` 是卡片
候选中用户点击的**目标**；`EC.expert_id` 是卡片仍有效时的**当前**专家状态，两者可以同时存在。

```json
{
  "action": "confirm_team_handoff",
  "source": "team_handoff",
  "source_message_id": "msg_handoff_001",
  "target_expert_id": "academic_coach",
  "context_scope": "profile",
  "expert_context": {
    "expert_team_id": "student_growth_expert_team",
    "expert_id": "e_career_planner",
    "expected_branch_version": 12,
    "expected_selection_version": 4,
    "operation": "continue"
  }
}
```

服务端会校验卡片来源、卡片活跃状态、当前团队及目标候选资格；不能用旧卡或手工伪造
的目标 ID 切换。外层 `context_data.profile_id` 始终是本轮执行孩子：若它不同于卡片来源孩子，
服务端只把卡片作为授权记录，在目标孩子分支激活候选专家，不读取来源孩子的档案、Facts、表单或历史。
切换孩子时，来源孩子的未完成表单与活动 Skill 会作废，但仍可确认的专家转交卡保持 `active`，直到
被确认、被新的实质性对话取代或因配置变更而失效。

### 4.3 切换孩子时：不选专家、选专家、切团队

每次都仅把顶层 `context_data.profile_id` 改为目标孩子。例如从 `child_a` 切到 `child_b`：

```json
"context_data": {"user_id": "user_001", "profile_id": "child_b", "student_name": "小明"}
```

`input.profile_id` 不能出现。以下三种请求都可直接作为这次切换的同一条 `chat` 请求：

`switch_team_member` 不是 `chat`，不能在同一条请求中切换孩子。`confirm_team_handoff` 则保持内层
`input` 不变，并允许由外层 `context_data.profile_id` 指定执行孩子；前端无需先预检或改写卡片参数。
当执行孩子与卡片来源不同，响应会补充 `source_profile_id`、`execution_profile_id` 与
`cross_profile:true` 供审计，旧前端可忽略这些新增字段。

| 用户意图 | `source` | `EC` | 服务端行为 |
| --- | --- | --- | --- |
| 不选专家，只继续聊 | `chat` | `expert_team_id:null` + `expert_id:null` + `operation:"continue"` | 切入 B；如 session 已有团队/成员则继承，否则通用对话。 |
| 手动选择单专家 | `chat` | `expert_team_id:null` + `expert_id` + `operation:"select_expert"` | 在 B 的单专家模式处理；若当前已有团队，必须改为下一行的显式团队形式。 |
| 切换专家团，但不指定成员 | `chat` | `expert_team_id` + `expert_id:null` + `operation:"select_team"` | 切入 B 后激活新团队的主协调专家。 |
| 选择/切换专家团并指定成员 | `toolbar` | `expert_team_id` + 团内 `expert_id` + `operation:"select_expert"` | 切入 B 后原子激活目标团队并选目标成员。无需区分 B 是否第一次进入。 |

切换孩子时如果前端没有目标孩子的本地 `branch_version` / `selection_version`，必须省略它们；服务端在
切换完成后使用目标分支权威版本。前端已缓存目标孩子的最新权威状态时才可携带断言，以防旧页面覆盖。

### 4.4 `expert_id`、`target_expert_id` 与字段互斥表

| 字段 | 所在位置 | 表示什么 | 允许出现的动作 |
| --- | --- | --- | --- |
| `expert_context.expert_id` | `input.expert_context` | `continue` 时与团队字段同为 `null` 表示继承；具体值是严格断言。`select_expert` 时是目标专家；`select_team` 时必须为 `null`。 | 所有非停止动作。 |
| `target_expert_id` | `input` 顶层 | 已激活团队里的“下一位目标成员”。 | 仅 `switch_team_member`、`confirm_team_handoff`。 |
| `expert_context.expert_team_id` | `input.expert_context` | 当前/目标专家团；单专家模式为 `null`。 | 所有非停止动作。 |
| `context_data.profile_id` | 顶层请求 | 本轮目标孩子；变化即切孩子。 | 仅 `context_scope:"profile"`。 |

组合规则：

| `action` | `operation` | `expert_team_id` | `expert_id` | `target_expert_id` | 不能一起传的内容 |
| --- | --- | --- | --- | --- | --- |
| `chat` 普通续聊 | `continue` | 必须 `null` | 必须 `null` | 不传 | 缺少任一固定字段；只传其中一个具体 ID；`input.profile_id`。 |
| `chat` 选团队 | `select_team` | 必传 | 必须 `null` | 不传 | 非空 `expert_id`、`target_expert_id`。 |
| `chat` 选团队成员 | `select_expert` | 必传 | 必传，且属于该团队 | 不传 | `target_expert_id`、`select_team_member`（新客户端）。 |
| `chat` 单专家 | `select_expert` | `null` | 必传 | 不传 | `target_expert_id`；当前已有团队时省略团队 ID。 |
| `chat` 退出专家模式 | `clear_expert` | 必须 `null` | 必须 `null` | 不传 | 具体团队/专家 ID。 |
| `switch_team_member` | `continue` | 必传：当前团队 | 必传：当前专家 | 必传，目标成员 | `select_team` / `select_expert`；换目标时不要改写 EC 为目标。 |
| `confirm_team_handoff` | `continue` | 必传：当前团队 | 必传：当前专家 | 必传，且来自卡片候选 | `select_team` / `select_expert`；不要伪造 `source_message_id`。 |

`select_team_member` 仅保留为旧客户端兼容别名；新客户端始终使用 `select_expert`。所有 `input`
模型均禁止未知字段，因此在 `chat` 请求顶层传 `target_expert_id`，或在 `switch_team_member` /
`confirm_team_handoff` 中把 `target_expert_id` 放进 `expert_context`，都会被拒绝。

`enter_skill`、`quit_skill`、`switch_team_member`、`confirm_team_handoff` 也必须带 `EC`，且其
`operation` 固定为 `continue`：它们不能顺带改变专家团选择。

`clear_expert` 只允许 `action:"chat"` 且 `source:"toolbar"` 使用。它是用户明确的“退出专家模式”
操作：服务端清空 session 级 Agent 选择，作废当前分支未完成的表单/Skill 交互，并让本条消息按
`general_chat + Soul` 执行。`null/null/continue` 永远不会触发这一退出行为。

## 5. 过期状态与错误处理

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

## 6. `stop` 的可靠返回

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

## 7. SSE 前端消费规则

`state` 是完整快照，`done` 是最后快照的传输确认。对同一个 `run_id` 只接收递增 `seq`。
收到 `stopped`、`completed`、`failed`、`blocked` 或 `superseded` 时结束 loading。模型上下文
始终只读取当前范围分支；主页面可展示整个带范围标签的时间线，但不能把其他范围历史送回模型。

## 8. 正式聊天页与 AI 业务调试台的状态对齐

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

### 8.1 专家转交确认的历史事件

用户点击主协调专家的转交卡后，服务端会保存一条可见时间线事件：

```json
{
  "role": "user",
  "content": "@家庭教育专家",
  "message_type": "team_handoff_confirmation",
  "metadata": {
    "source_message_id": "msg_handoff_001",
    "target_expert_id": "academic_coach",
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

### 8.2 候选流的错误与结束

候选流中的 `done` 同样只是最终 `state` 的传输确认。候选测试停止使用独立接口
`POST /workbench/v1/revision-tests/{debug_session_id}/turns/{run_id}/stop`（仅带 `actor_id`）；
原流的最终 `state` 与 `done` 会以 `status="stopped"` 返回部分回答、当前专家、当前 Skill 和
候选 `expert_context`，前端不可用浏览器中断来代替这次服务端停止。若候选 Runtime 出错，工作台会先发送
`state.status="failed"`，其中保留已收到的 `assistant.content`、当前 `expert`、
`expert_context` 与当前 Skill，然后发送 `error`。前端据此保留部分回答和状态，结束 loading，
不得重放该请求。候选测试不共享正式会话的 `stop` endpoint；正式聊天的可靠停止契约仍按第 6 节执行。
