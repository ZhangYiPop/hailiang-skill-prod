# SSE v2 专家团对话接入说明

> 文档状态：当前实现（2026-09-04）
>
> 协议：`hailiang.sse.v2`
>
> 对话入口：`POST /api/v2/sessions/chat/stream`
>
> 适用对象：业务前端、转发后端（BFF）、专家团配置与联调人员

> **严格请求字段更新（2026-09-03）：** 专家团/专家状态统一放入
> `input.expert_context`；每一轮均回传并携带该对象。`input.profile_id`、顶层
> `expert_team_id`、顶层 `expert_id` 已被拒绝。完整请求、停止和错误处理请见
> [SSE_V2_EXPERT_CONTEXT_CONTRACT.md](SSE_V2_EXPERT_CONTEXT_CONTRACT.md)。

本文只说明“由专家团承接”的会话对话：选择专家团、主协调专家回答、专家转交卡、用户确认转交、工具栏切换成员，以及流式状态的消费方式。

不包含普通 Skill 的进入/退出、Skill 推荐卡、路径卡等协议说明。它们不应作为专家团前端的路由入口；专家团内由 Runtime 根据专家的已锁定能力自行决定实际执行路径。

## 1. 先记住这十条规则

1. 统一入口为 `POST /api/v2/sessions/chat/stream`，请求顶层是 JSON，但 `input` 字段本身必须是 **JSON 字符串**。
2. 用户首次选择专家团时，在 `action="chat"` 的 `expert_context` 中以
   `operation="select_team"` 传团队 ID；未选成员时服务端将主协调专家绑定到当前会话上下文范围。
   若用户已从该团队成员菜单选择专家，则使用 `operation="select_team_member"` 一次绑定团队和成员。
3. 已经进入专家团后，后续普通追问仍带 `expert_context`，但使用
   `operation="continue"`。其中版本断言字段可省略；它是状态断言，不会重置为主协调专家。
   只要当前仍由主协调专家承接，服务端会在每一轮新的用户消息中重新判断成员承接意图；
   若本轮更适合团内成员，必须重新生成本轮 `team_handoff` 卡片，上一轮卡片未点击也不阻止再次建议。
4. 主协调专家可以直接回答，也可以下发 `team_handoff` 专家转交卡。前端不能仅从回复正文里的“建议转交”文字判断或自行切换专家。
5. 用户点击转交卡后，前端发送 `confirm_team_handoff`；服务端校验该卡仍有效、目标专家仍属于当前团队后，才切换专家并让目标专家回答原问题。
6. 用户在专家团工具栏主动指定成员并提问时，发送 `switch_team_member`；不是把 `@专家名称` 拼入文本。
7. SSE 对外只发送 `state`、`ping`、`done`。`state.data` 是完整快照，不是文本增量；每个新状态用整帧替换当前助手占位消息。
8. 非 `stop` 动作每次都必须使用新的 `run_id`；`stop` 必须复用正在生成的 `run_id`。
9. 同一个 session 可以处于未绑定孩子、孩子 A、孩子 B 等多个上下文范围；模型只看到当前范围历史。专家团及最后实际承接成员会跨孩子延续，Skill、表单、Facts 和交互卡仍只属于当前范围。
10. BFF 只能校验身份、注入可信上下文并原样低延迟转发 SSE，不能自行路由到专家或重组 SSE 帧。

## 2. 角色、标识符与职责

```text
用户
  │  选择专家团 / 发送消息
  ▼
专家团（team_id）
  │  默认由主协调专家承接
  ▼
主协调专家（coordinator_expert_id）
  ├─ 直接回答
  └─ 提出成员转交建议（team_handoff）
        │
        │ 用户确认候选专家
        ▼
成员专家（target_expert_id）
  │
  └─ 在自身授权范围内完成回答
```

| 标识符 | 示例 | 中文含义 | 使用边界 |
| --- | --- | --- | --- |
| `session_id` | `session_01...` | 一个用户会话的稳定 ID | 后续所有轮次复用；不能跨用户使用。 |
| `run_id` | `run_01...` | 一次 HTTP/SSE 执行的唯一 ID | 每次非停止动作新建；不等于消息 ID。 |
| `message_id` | `msg_01...` | 一条助手消息的稳定 ID | 转交卡确认使用；不能用 `run_id` 代替。 |
| `expert_team_id` | `student_growth_expert_team` | 专家团唯一 ID | 用户首次选择/主动切换专家团时，放入 `chat.expert_context.expert_team_id`。 |
| `coordinator_expert_id` | `career_plan_expert` | 专家团主协调专家 ID | 选择专家团后默认的当前专家；由服务端目录返回。 |
| `expert_id` | `family_education_expert` | 某位专家的唯一 ID | 当前承接状态放入 `expert_context`；团队成员切换/转交使用 `target_expert_id`。 |
| `target_expert_id` | `family_education_expert` | 本次要切换/确认接管的目标专家 ID | 仅用于 `switch_team_member`、`confirm_team_handoff`。 |
| `handoff_id` | `handoff_01...` | 一张专家转交卡的实例 ID | 用于前端区分卡片和调试；**不**放入确认请求。 |
| `source_message_id` | `msg_01...` | 生成转交卡的助手消息 ID | 确认卡片时原样传回；必须是该卡所属的当前范围消息。 |
| `seq` | `1`、`2`、`3` | 同一 `run_id` 内的 SSE 状态序号 | 仅用于前端去重和乱序保护。 |

## 3. 专家团目录：先获取可选项

业务前端应从服务端读取可用专家团与成员，而不是把名称或 ID 写死在浏览器中。

```http
GET /api/v1/expert-teams
GET /api/v1/experts
```

`GET /api/v1/expert-teams` 响应示例：

```json
{
  "expert_teams": [
    {
      "team_id": "student_growth_expert_team",
      "name": "学生成长专家团",
      "description": "主协调专家负责承接用户尚未明确归属的问题，并根据问题选择是否建议转交。",
      "topology": "team",
      "coordinator_expert_id": "career_plan_expert",
      "coordinator_name": "升学规划专家",
      "members": [
        {
          "expert_id": "career_plan_expert",
          "name": "升学规划专家",
          "mention_name": "升学规划专家",
          "routing_brief": "负责升学路径、选科、院校专业与学业规划，并作为主协调专家兜底。",
          "is_coordinator": true
        },
        {
          "expert_id": "family_education_expert",
          "name": "家庭教育专家",
          "mention_name": "家庭教育专家",
          "routing_brief": "负责亲子沟通、家庭规则、学习习惯与明确请求的 MBTI 自我探索。",
          "is_coordinator": false
        }
      ]
    }
  ]
}
```

### 3.1 专家团目录字段

| 字段 | 类型 | 中文含义 | 前端用途 |
| --- | --- | --- | --- |
| `expert_teams` | 数组 | 当前可用专家团列表 | 专家团选择器的数据源。 |
| `team_id` | 字符串 | 专家团 ID | 用户选择后传给 `chat.expert_context.expert_team_id`。 |
| `name` | 字符串 | 专家团显示名称 | 展示在选择器、会话标题、状态栏。 |
| `description` | 字符串 | 专家团的协同与兜底简介 | 可作二级说明，不参与路由。 |
| `topology` | 固定为 `team` | 表示该目录项是专家团 | 用于前端区分单专家目录。 |
| `coordinator_expert_id` | 字符串 | 主协调专家 ID | 选择团队后的默认接管者；展示时应再根据成员信息取名称。 |
| `coordinator_name` | 字符串 | 主协调专家显示名 | 仅展示，不能反向用名称发请求。 |
| `members` | 数组 | 专家团成员 | 用于 @、工具栏切换和本地成员合法性预校验。 |
| `members[].expert_id` | 字符串 | 成员专家 ID | `target_expert_id` 的候选值。 |
| `members[].name` | 字符串 | 成员显示名称 | 卡片和菜单展示。 |
| `members[].mention_name` | 字符串 | 适合 @ 的显示名 | UI 文案，例如 `@家庭教育专家`；请求仍使用 `expert_id`。 |
| `members[].routing_brief` | 字符串 | 该成员的职责简介 | 切换菜单、转交卡的辅助说明。 |
| `members[].is_coordinator` | 布尔值 | 是否主协调专家 | UI 标记“主协调专家”；不要据此在浏览器自行路由。 |

## 4. 前端 `input` 节点式请求（当前可联调 cURL）

这一节是业务前端和 BFF 的实际对接模板。每段 cURL 都包含完整 HTTP 包，只是为了
便于本地联调；**浏览器实际只构造 `input` 对象**。BFF 负责生成并注入顶层的
`session_id`、每次新的 `run_id`、`context_data`、鉴权头和 `X-Request-Id`，再将
`input` 序列化成字符串转发。`expert_context` 中的版本字段可以省略，服务端会在
恢复当前会话和上下文分支后使用权威版本。

```text
前端负责：action / content / context_scope / context_activation / expert_context /
          target_expert_id / source_message_id / source
BFF 负责：session_id / run_id / context_data / 用户鉴权 / SSE 原样转发
服务端负责：恢复当前 expert_context；显式版本存在时校验，缺省版本时使用当前版本
服务端返回：每个 state 的完整权威 expert_context，供需要同步状态的客户端使用
```

### 4.1 所有节点共用的 `expert_context`

`expert_context` 不是前端猜测的路由参数。团队 ID、专家 ID 和操作类型用于表达本轮意图；
版本字段对请求方可选。前端如果已经拥有上一次同一范围 `state` 或会话恢复接口返回的版本，
可以继续回传；不回传时服务端会按当前 session/profile 状态处理。未绑定孩子使用
`__unbound__` 作为本地键。

```json
{
  "expert_team_id": "student_growth_expert_team",
  "expert_id": "family_education_expert",
  "expected_branch_version": 12,
  "expected_selection_version": 4,
  "operation": "continue"
}
```

| 字段 | 前端从哪里取 | 作用 |
| --- | --- | --- |
| `expert_team_id` | 最近权威 `state.expert_context.expert_team_id` | 当前专家团；普通聊天为 `null`。 |
| `expert_id` | 最近权威 `state.expert_context.expert_id` | 当前实际承接专家，可能是主协调专家或已转交成员。 |
| `expected_branch_version` | 可选；有缓存时取最近权威 `state.expert_context.branch_version` | 当前孩子/未绑定范围的版本断言；省略时服务端使用当前分支版本。 |
| `expected_selection_version` | 可选；有缓存时取最近权威 `state.expert_context.selection_version` | session 级专家选择版本；省略时服务端使用当前选择版本。 |
| `operation` | 由本次用户动作决定 | 普通追问用 `continue`；用户主动选择团队、团队成员或单专家时分别用 `select_team` / `select_team_member` / `select_expert`。 |

不要在 `input` 顶层传 `profile_id`、`expert_team_id` 或 `expert_id`：它们分别会返回
`422 INPUT_PROFILE_ID_FORBIDDEN` 和 `422 LEGACY_EXPERT_FIELDS_FORBIDDEN`。

下面的 `run_*`、`session_*`、`profile_*` 是 BFF 示例占位符；如果请求携带版本号，
版本号 `12/4` 也是示例，真实请求必须替换为刚收到的权威值。多端或旧客户端可以
省略两个 `expected_*` 字段，由服务端读取当前权威状态。

### 4.2 以指定专家团唤起新对话

前端用户在工具栏选择“学生成长专家团”并发送第一句时，使用 `chat + select_team`。
新 session 的服务端初始分支版本通常为 `branch_version=1`、选择版本为
`selection_version=0`。请求方可以省略两个 `expected_*` 字段；服务端把团队主协调专家
设为当前承接者，并在回复的 `state` 中返回完整权威版本。

```bash
curl --no-buffer -N -X POST "http://127.0.0.1:8013/api/v2/sessions/chat/stream" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "X-SSE-Protocol: hailiang.sse.v2" \
  -H "X-Request-Id: req-team-open-001" \
  --data-raw '{
    "session_id":"session_bff_generated_001",
    "run_id":"run_bff_generated_001",
    "input":"{\"action\":\"chat\",\"context_scope\":\"profile\",\"context_activation\":\"auto\",\"content\":\"孩子最近不愿意和我沟通，怎么办？\",\"source\":\"chat\",\"expert_context\":{\"expert_team_id\":\"student_growth_expert_team\",\"expert_id\":null,\"operation\":\"select_team\"},\"enable_thinking\":false,\"return_reasoning\":false}",
    "context_data":{"user_id":"user_001","profile_id":"profile_child_a","student_name":"小海"}
  }'
```

前端实际构造的 `input`：

```json
{
  "action": "chat",
  "context_scope": "profile",
  "context_activation": "auto",
  "content": "孩子最近不愿意和我沟通，怎么办？",
  "source": "chat",
  "expert_context": {
    "expert_team_id": "student_growth_expert_team",
    "expert_id": null,
    "operation": "select_team"
  }
}
```

`select_team` 的边界：只有用户**明确点击专家团选择器**才发送它；不能在每次追问中重复发送，否则会将当前成员重新设回主协调专家，并使等待中的转交卡失效。

### 4.2.1 以指定成员唤起新对话

前端默认展示一个专家团并平铺该团成员时，用户不选择成员就沿用上面的 `select_team`；用户在
首条消息前选择成员，则同样发送 `action="chat"`，仅把 `operation` 改为
`select_team_member`，并同时传入团队与成员 ID。服务端原子校验成员归属并直接由该成员回答。

```json
{
  "action": "chat",
  "context_scope": "profile",
  "context_activation": "auto",
  "content": "请直接分析孩子总是顶嘴的问题。",
  "source": "toolbar",
  "expert_context": {
    "expert_team_id": "student_growth_expert_team",
    "expert_id": "family_education_expert",
    "expected_branch_version": 1,
    "expected_selection_version": 0,
    "operation": "select_team_member"
  }
}
```

不要把成员 ID 写进正文，也不要先发送一个没有用户问题的团队选择请求。若成员不属于指定团队，
服务端返回 `422 EXPERT_NOT_IN_ACTIVE_TEAM`。

### 4.3 主协调专家承接后的普通追问

假设上一轮最终 `state.expert_context` 为：团队 `student_growth_expert_team`、专家
`career_plan_expert`、分支版本 `1`、选择版本 `1`。前端可以只回传团队、专家和
`continue`；也可以附带两个 `expected_*` 版本字段。无论是否附带版本，都不重新传
“我选择了哪个团队”的意图。

```bash
curl --no-buffer -N -X POST "http://127.0.0.1:8013/api/v2/sessions/chat/stream" \
  -H "Content-Type: application/json" -H "Accept: text/event-stream" \
  --data-raw '{
    "session_id":"session_bff_generated_001",
    "run_id":"run_bff_generated_002",
    "input":"{\"action\":\"chat\",\"context_scope\":\"profile\",\"context_activation\":\"auto\",\"content\":\"他主要是在写作业和玩手机时顶撞我。\",\"source\":\"chat\",\"expert_context\":{\"expert_team_id\":\"student_growth_expert_team\",\"expert_id\":\"career_plan_expert\",\"operation\":\"continue\"}}",
    "context_data":{"user_id":"user_001","profile_id":"profile_child_a","student_name":"小海"}
  }'
```

理由：`continue` 是状态断言，表示“仍由服务端确认的当前主协调专家承接”；它不会重新选择
团队、不会解析文本中的 `@专家`，也不会重置专家团状态。主协调专家仍会基于本轮新消息重新
判断是否需要生成新的 `team_handoff`；如果需要，必须调用转交工具，不能只在正文中提及专家。

### 4.4 切换孩子后的首条消息：不预检、不重发

用户在同一 session 从孩子 A 切到孩子 B 后，前端仍只发送一次普通 `chat`。`context_data`
由 BFF 改成孩子 B；前端保持 `context_activation="auto"`。服务端会先恢复/创建 B 分支，
再恢复 session 最后活跃成员（包含已确认转交的成员），最后回答此条消息。

```bash
curl --no-buffer -N -X POST "http://127.0.0.1:8013/api/v2/sessions/chat/stream" \
  -H "Content-Type: application/json" -H "Accept: text/event-stream" \
  --data-raw '{
    "session_id":"session_bff_generated_001",
    "run_id":"run_bff_generated_switch_b",
    "input":"{\"action\":\"chat\",\"context_scope\":\"profile\",\"context_activation\":\"auto\",\"content\":\"请结合小明自己的情况分析沟通问题。\",\"source\":\"chat\",\"expert_context\":{\"expert_team_id\":\"student_growth_expert_team\",\"expert_id\":\"family_education_expert\",\"expected_branch_version\":1,\"expected_selection_version\":2,\"operation\":\"continue\"}}",
    "context_data":{"user_id":"user_001","profile_id":"profile_child_b","student_name":"小明"}
  }'
```

这里 `expert_context` 中的版本字段可以不传，不要求前端事先知道 B 是否已有服务端分支；
发生范围切换时服务端会在恢复 B 分支后使用 B 的当前版本。首个 `state` 会返回：

- `context_switched: true`、`context_activation: "auto"`；
- B 范围的权威 `expert_context` 和 `session.active_skill`；
- B 自己的历史/Facts，不混入 A；如 B 的旧表单、转交卡或 Skill 属于不同 Agent，则仅保留为历史且标为过期。

之后用户在 B 的下一次追问，改用这个首帧/终帧返回的 B 专属 `expert_context`，仍为
`operation="continue"`。只有 `context_activation="strict"` 才会拒绝这种隐式切换并返回
`409 CONTEXT_ACTIVATION_REQUIRED`；业务前端不应使用 `strict`。

#### 孩子档案结合提示：`context_notice`

首次创建带名字的孩子会话，或本轮切换到带名字的孩子时，首个 `state` 返回：

```json
{
  "context_notice": {
    "type": "profile_context_activated",
    "text": "本轮回答将结合 **小海** 的档案数据。",
    "to_context_scope": "profile",
    "to_profile_id": "profile_child_a",
    "to_context_label": "小海"
  }
}
```

切换孩子时 `type` 为 `profile_switched`，但 `text` 仍使用同样的 Markdown 格式。前端应
按 Markdown 渲染 `text`，因此孩子名会加粗；该提示是系统上下文说明，不能拼入模型回答或
下一轮 `content`。孩子名称为空或仅空白时，服务端返回 `context_notice: {}`，不展示这句话。

### 4.5 工具栏主动切换专家并同时提问

用户在当前专家团工具栏点击“家庭教育专家”并输入问题时，动作是
`switch_team_member`，不是 `chat`，也不是把 `@专家名称` 拼入正文。此动作的
`expert_context.operation` 固定为 `continue`；团队 ID、专家 ID 和版本字段可以由服务端
按当前会话状态补齐或校验，前端不需要维护版本字段。

```bash
curl --no-buffer -N -X POST "http://127.0.0.1:8013/api/v2/sessions/chat/stream" \
  -H "Content-Type: application/json" -H "Accept: text/event-stream" \
  --data-raw '{
    "session_id":"session_bff_generated_001",
    "run_id":"run_bff_generated_toolbar_001",
    "input":"{\"action\":\"switch_team_member\",\"context_scope\":\"profile\",\"target_expert_id\":\"family_education_expert\",\"content\":\"我想直接请家庭教育专家分析孩子顶撞的问题。\",\"source\":\"toolbar\",\"expert_context\":{\"expert_team_id\":\"student_growth_expert_team\",\"expert_id\":\"career_plan_expert\",\"expected_branch_version\":1,\"expected_selection_version\":1,\"operation\":\"continue\"}}",
    "context_data":{"user_id":"user_001","profile_id":"profile_child_a","student_name":"小海"}
  }'
```

成功后 SSE 返回 `expert.transition.source="toolbar"`。前端必须用新 state 的
`expert_context` 覆盖缓存；该选择会更新 session 级 `selection_version`，因此后续切换
孩子也会延续此成员。

### 4.6 点击主协调专家的转交卡

协调专家返回 `team_handoff.status="active"` 后，前端从**卡片所在 state**取得
`source_message_id`、`target_expert_id`，并携带同一帧的 `expert_context`。这不是普通
用户 `chat.content`，也不能自行拼接 `@专家` 文本。

```bash
curl --no-buffer -N -X POST "http://127.0.0.1:8013/api/v2/sessions/chat/stream" \
  -H "Content-Type: application/json" -H "Accept: text/event-stream" \
  --data-raw '{
    "session_id":"session_bff_generated_001",
    "run_id":"run_bff_generated_handoff_001",
    "input":"{\"action\":\"confirm_team_handoff\",\"context_scope\":\"profile\",\"source_message_id\":\"msg_handoff_001\",\"target_expert_id\":\"family_education_expert\",\"source\":\"team_handoff\",\"expert_context\":{\"expert_team_id\":\"student_growth_expert_team\",\"expert_id\":\"career_plan_expert\",\"expected_branch_version\":1,\"expected_selection_version\":1,\"operation\":\"continue\"}}",
    "context_data":{"user_id":"user_001","profile_id":"profile_child_a","student_name":"小海"}
  }'
```

服务端会校验卡片属于当前孩子分支、仍为 active、目标专家仍属于该团队。成功后历史会新增
展示事件 `@家庭教育专家`（它不是模型的后续 `chat.content`），并以新 state 返回
`expert_id="family_education_expert"`、`transition.source="team_handoff"` 和递增后的
`selection_version`。

### 4.7 成员专家承接后的普通追问

无论该成员来自工具栏还是转交卡，下一轮一律回到 `chat + continue`。例如上一步最新 state
返回分支版本 `1`、选择版本 `2`：

```bash
curl --no-buffer -N -X POST "http://127.0.0.1:8013/api/v2/sessions/chat/stream" \
  -H "Content-Type: application/json" -H "Accept: text/event-stream" \
  --data-raw '{
    "session_id":"session_bff_generated_001",
    "run_id":"run_bff_generated_member_followup_001",
    "input":"{\"action\":\"chat\",\"context_scope\":\"profile\",\"context_activation\":\"auto\",\"content\":\"那我今天晚上可以先怎么和他沟通？\",\"source\":\"chat\",\"expert_context\":{\"expert_team_id\":\"student_growth_expert_team\",\"expert_id\":\"family_education_expert\",\"expected_branch_version\":1,\"expected_selection_version\":2,\"operation\":\"continue\"}}",
    "context_data":{"user_id":"user_001","profile_id":"profile_child_a","student_name":"小海"}
  }'
```

### 4.8 输入参数速查与不可做的事

| 用户节点 | `action` | `operation` | 前端额外字段 | 不能做什么 |
| --- | --- | --- | --- | --- |
| 选择/切换专家团并发消息 | `chat` | `select_team` | `expert_context.expert_team_id`，`expert_id=null`，`context_activation=auto` | 不在顶层传团队 ID。 |
| 首条消息选择团内成员 | `chat` | `select_team_member` | `expert_context.expert_team_id` + 团内 `expert_id`，`source=toolbar` | 不传其他团队的专家 ID。 |
| 主协调/成员普通追问 | `chat` | `continue` | `content`，`context_activation=auto` | 不重复 `select_team`。 |
| 切换孩子后的首聊 | `chat` | `continue` | `context_scope`；BFF 改 `context_data`；`context_activation=auto` | 不预检 B 分支、不自动重发。 |
| 工具栏选择成员并问问题 | `switch_team_member` | `continue` | `target_expert_id`、`content`、`source=toolbar` | 不发送 `context_activation`，不把 @ 写入普通聊天。 |
| 点击转交卡 | `confirm_team_handoff` | `continue` | `source_message_id`、`target_expert_id`、`source=team_handoff` | 不把 `@专家` 当作 `content`。 |
| 成员承接后的追问 | `chat` | `continue` | 当前成员 `expert_context`、`context_activation=auto`；版本字段可省略 | 不沿用转交前的 coordinator ID。 |

显式携带旧版本时出现 `409 EXPERT_CONTEXT_STALE`，前端读取错误中 `details.expert_context`，
覆盖当前范围缓存并提示用户；**不能自动重放**原提问。省略版本字段的请求由服务端按当前
会话状态处理。出现 `409 ACTIVE_RUN_MUST_STOP` 时，先对该 run 发送
`{"action":"stop","source":"composer"}`，收到 stopped 的 state/done 后再发下一轮。

## 附录 A：旧版请求示例（不可用于当前联调）

以下内容为 2026-09-03 之前的结构说明，保留仅作历史阅读。它含顶层
`expert_team_id` / `expert_id` 或 `input.profile_id` 等已拒绝字段，**不得复制到当前前端或 BFF**。

## 4. HTTP 通用契约

### 4.1 请求头

```http
POST /api/v2/sessions/chat/stream HTTP/1.1
Content-Type: application/json
Accept: text/event-stream
X-SSE-Protocol: hailiang.sse.v2
X-Request-Id: req_全局唯一值
```

成功建流响应：

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
X-SSE-Protocol: hailiang.sse.v2
```

### 4.2 顶层请求体

```json
{
  "session_id": "session_01J...",
  "run_id": "run_01J...",
  "input": "{\"action\":\"chat\",\"context_scope\":\"profile\",\"expert_team_id\":\"student_growth_expert_team\",\"content\":\"孩子最近不愿意沟通，怎么办？\",\"source\":\"chat\"}",
  "context_data": {
    "user_id": "user_001",
    "profile_id": "profile_001",
    "student_name": "小海"
  }
}
```

| 顶层字段 | 类型 | 必填 | 中文含义与规则 |
| --- | --- | --- | --- |
| `session_id` | 非空字符串 | 是 | 会话 ID。新会话由 BFF 生成，后续轮次复用。BFF 必须校验它属于当前登录用户。 |
| `run_id` | 非空字符串 | 是 | 本次流式执行 ID。除停止外必须从未使用过。建议使用 UUID/ULID。 |
| `input` | 字符串 | 是 | 序列化后的 JSON 对象；**不是嵌套 JSON 对象**。未知字段会被拒绝。 |
| `context_data` | 对象 | 非停止动作必填 | BFF 从登录态和业务档案注入的可信上下文。浏览器不应直接决定 `user_id`。 |
| `debug_session_id` | 字符串 | 否 | 仅 AI 业务调试台的固定候选快照使用；正式业务前端不要传。 |

错误示例：

```json
{
  "session_id": "session_01",
  "run_id": "run_01",
  "input": {"action": "chat"}
}
```

上例的 `input` 是对象而非字符串，服务端会返回 `422`。

### 4.3 上下文范围 `context_scope`

专家团可以在两类上下文范围中工作。范围决定该轮模型可以读取哪一段会话历史，而不是根据用户文本自动猜测。

| `context_scope` | 适用场景 | `input.profile_id` | `context_data` | 数据隔离规则 |
| --- | --- | --- | --- | --- |
| `profile` | 以某个孩子档案为上下文咨询 | 可选的旧版兼容字段；若传必须匹配 | `user_id`、`profile_id`、`student_name` 必填，且 `profile_id` 是唯一上下文来源 | 只读取/写入该孩子对应的会话分支。 |
| `unbound` | 暂不绑定孩子的泛咨询 | 禁止传 | 只能传 `user_id` | 不读取或写入孩子档案、孩子 Facts、账户共享 Facts。 |

`profile` 范围示例：

```json
{
  "context_scope": "profile"
}
```

```json
{
  "user_id": "user_001",
  "profile_id": "profile_001",
  "student_name": "小海"
}
```

`unbound` 范围示例：

```json
{
  "context_scope": "unbound"
}
```

```json
{
  "user_id": "user_001"
}
```

一个 session 可依次使用“未绑定 → 孩子 A → 孩子 B → 未绑定”。前端可展示完整时间线，但模型每轮只能得到当前范围的历史；不同范围的转交卡也不能互相确认。

### 4.4 同一专家团会话中切换孩子

切换孩子**没有**独立的 `switch_profile` action。它就是下一次 `chat` 显式带上新的 `context_scope="profile"`，并在 `context_data` 中带上 BFF 当前选中的新孩子。

```text
同一 session
  孩子 A 的专家团分支 ── chat(context_data=孩子B) ──> 孩子 B 的专家团分支
                                  │
                                  ├─ B 已有分支：恢复 B 自己的专家团、当前专家、历史和交互状态
                                  └─ B 首次进入：创建 B 的空分支；如需专家团，当前请求必须显式选择
```

#### 4.4.1 从孩子 A 切换到孩子 B，并由专家团承接

如果孩子 B 在该 session 中从未出现过，应在同一条 `chat` 中选择专家团：

```json
{
  "session_id": "session_001",
  "run_id": "run_switch_to_child_b_001",
  "input": "{\"action\":\"chat\",\"context_scope\":\"profile\",\"expert_team_id\":\"student_growth_expert_team\",\"content\":\"接下来请以孩子小明为上下文，帮我分析他的沟通问题。\",\"source\":\"chat\"}",
  "context_data": {
    "user_id": "user_001",
    "profile_id": "profile_child_b",
    "student_name": "小明"
  }
}
```

| 请求字段 | 本次切换中的值 | 作用 |
| --- | --- | --- |
| 顶层 `session_id` | 保持 `session_001` | 同一个会话时间线，不新建会话。 |
| 顶层 `run_id` | 新值 | 本次切换和回答是一轮新的执行。 |
| `input.context_scope` | `profile` | 明确新一轮使用孩子档案范围。 |
| `context_data.profile_id` | `profile_child_b` | 指定目标孩子；这是服务端实际采用的上下文。 |
| `context_data.student_name` | `小明` | 目标孩子展示名，不能继续传孩子 A 的名称。 |
| `input.expert_team_id` | `student_growth_expert_team` | 孩子 B 首次进入时显式选择专家团；服务端从该团主协调专家开始承接。 |
| `input.content` | 新问题 | 只作为孩子 B 分支的模型输入，不会混入孩子 A 的模型历史。 |

成功后的早期 `state` 应反映实际生效范围：

```json
{
  "session_id": "session_001",
  "run_id": "run_switch_to_child_b_001",
  "profile_id": "profile_child_b",
  "profile_name": "小明",
  "context_scope": "profile",
  "context_label": "小明",
  "context_switched": true,
  "context_notice": {
    "type": "profile_switched",
    "text": "已切换为孩子「小明」的上下文，后续回答将结合该孩子的信息。",
    "from_context_scope": "profile",
    "from_profile_id": "profile_child_a",
    "from_context_label": "小红",
    "to_context_scope": "profile",
    "to_profile_id": "profile_child_b",
    "to_context_label": "小明"
  },
  "profile_switched": true,
  "profile_context_status": "matched",
  "session_created": false,
  "expert": {
    "mode": "team",
    "team": {"team_id": "student_growth_expert_team"},
    "active": {"expert_id": "career_plan_expert", "is_coordinator": true},
    "activation": {"source": "explicit_or_restored", "is_default": false, "selection_source": ""},
    "transition": {}
  }
}
```

#### 4.4.2 切回已经使用过的孩子

若孩子 A 已经在当前 session 中建立过分支，切回 A 时只需要使用 A 的范围信息；服务端会恢复 A 自己保存的历史、Facts、专家团、当前专家、转交卡和其他进行中状态。

```json
{
  "action": "chat",
  "context_scope": "profile",
  "content": "继续刚才关于小红的讨论。",
  "source": "chat"
}
```

此处**不要**为了“续聊”重复传 `expert_team_id`。如果 A 分支本来已处于专家团，服务端会恢复该分支自己的团队和成员；若又传了 `expert_team_id`，含义变成“用户重新选择了该团队”，会回到其主协调专家。

#### 4.4.3 切换为未绑定孩子的专家团对话

未绑定孩子是同一个 session 的独立分支。首次在未绑定范围使用专家团时：

```json
{
  "session_id": "session_001",
  "run_id": "run_switch_unbound_001",
  "input": "{\"action\":\"chat\",\"context_scope\":\"unbound\",\"expert_team_id\":\"student_growth_expert_team\",\"content\":\"我想先咨询一个不涉及具体孩子的问题。\",\"source\":\"chat\"}",
  "context_data": {
    "user_id": "user_001"
  }
}
```

`unbound` 时，`input` 和 `context_data` 中都不得出现 `profile_id`、`student_name`、年级或孩子 Facts。返回中应为：

```json
{
  "profile_id": null,
  "profile_name": null,
  "context_scope": "unbound",
  "context_label": "未绑定孩子"
}
```

#### 4.4.4 切换孩子的强制规则

| 规则 | 原因与前端处理 |
| --- | --- |
| 切换孩子必须使用新的 `run_id` | 这是一次新的上下文范围执行，不能复用 A 的 run。 |
| 旧 run 正在生成时，先停止再切换 | 否则服务端返回 `409 ACTIVE_RUN_MUST_STOP`，防止旧范围结果写入新范围。 |
| `context_data.profile_id` 是本轮孩子来源 | `input.profile_id` 已被禁止；携带即返回 `422 INPUT_PROFILE_ID_FORBIDDEN`。 |
| 专家团与当前专家是**分支级状态** | 孩子 A、孩子 B、未绑定范围分别保存自己的团队和当前专家，不能认为它们在整个 session 中全局共享。 |
| 转交卡是**分支级交互** | 只能在产生它的孩子/未绑定范围确认；切到另一孩子时应显示为历史只读。 |
| 完整时间线不等于模型上下文 | UI 可显示所有孩子的带标签消息，但模型只读取本轮范围的消息。 |
| 首次进入一个范围需显式选择专家团 | 新范围没有继承其他范围的专家团；如不传 `expert_team_id`，它会走该范围的非专家团对话。 |

## 5. 专家团动作与请求字段

本说明只包含专家团相关的四个动作。

| `action` | `source` | 何时调用 | 是否新 `run_id` |
| --- | --- | --- | --- |
| `chat` | `chat` | 开始专家团对话、继续当前专家团的普通追问、切换到另一个专家团 | 是 |
| `confirm_team_handoff` | `team_handoff` | 用户点击主协调专家下发的转交卡 | 是 |
| `switch_team_member` | `toolbar` | 用户在当前专家团工具栏中主动指定成员并附带问题 | 是 |
| `stop` | `composer` | 用户停止当前生成 | 否，复用活动 run |

### 5.1 选择专家团并发送第一条消息：`chat`

```json
{
  "action": "chat",
  "context_scope": "profile",
  "expert_team_id": "student_growth_expert_team",
  "content": "孩子最近不愿意沟通，怎么办？",
  "source": "chat",
  "enable_thinking": false,
  "return_reasoning": false
}
```

| 字段 | 类型 | 必填 | 中文含义与规则 |
| --- | --- | --- | --- |
| `action` | 固定字符串 | 是 | 固定为 `chat`。 |
| `context_scope` | `profile` / `unbound` | 是 | 本轮使用的会话范围。 |
| `profile_id` | — | 不可传 | 当前孩子只由 `context_data.profile_id` 指定；`input.profile_id` 返回 `422 INPUT_PROFILE_ID_FORBIDDEN`。 |
| `expert_team_id` | 字符串 | 首次选择/切换专家团时必填 | 要由哪个专家团承接。服务端将当前专家设为该团队的主协调专家。 |
| `content` | 非空字符串 | 是 | 用户原始提问。不要把专家 ID 或转交指令埋进文本。 |
| `source` | 固定字符串 | 是 | 固定为 `chat`。 |
| `enable_thinking` | 布尔值 | 否 | 默认 `false`；不改变业务协议。 |
| `return_reasoning` | 布尔值 | 否 | 默认 `false`；正式前端不应依赖模型原始推理。 |

选择成功后，首批 `state` 即会逐步出现：

```json
{
  "expert": {
    "mode": "team",
    "team": {
      "team_id": "student_growth_expert_team",
      "name": "学生成长专家团",
      "coordinator_expert_id": "career_plan_expert"
    },
    "active": {
      "expert_id": "career_plan_expert",
      "name": "升学规划专家",
      "mention_name": "升学规划专家",
      "is_coordinator": true
    },
    "activation": {
      "source": "team_default_coordinator",
      "is_default": true,
      "selection_source": "manual_team"
    },
    "transition": {}
  }
}
```

其中 `expert.activation` 是当前专家的承接来源标记：

| 字段 | 中文含义 | 前端规则 |
| --- | --- | --- |
| `source="team_default_coordinator"` | 用户选定专家团后，由该团主协调专家默认承接。 | 显示“由主协调专家默认承接”。 |
| `is_default=true` | 当前承接是专家团默认入口，不是转交卡或工具栏切换。 | 可作为默认承接徽标的唯一判断依据。 |
| `selection_source` | 服务端记录的选择来源；当前可能为 `manual_team`、`deployment_snapshot` 或旧会话兼容的 `default_coordinator`。 | 仅用于调试/说明，业务判断以 `is_default` 为准。 |
| `source="explicit_or_restored"` | 当前专家来自用户显式选择、转交后的恢复或旧会话恢复。 | 不显示默认承接徽标；具体切换动作仍看 `expert.transition`。 |

`activation` 始终存在：没有专家模式时为 `{}`。当协调专家因工具栏切换或转交再次成为当前专家时，`is_default` 仍为 `false`，避免把后续人工切换误显示为首次默认承接。

### 5.2 继续当前专家团：`chat`

专家团已经在当前范围中激活时，只传普通问题即可：

```json
{
  "action": "chat",
  "context_scope": "profile",
  "content": "他主要是在写作业和玩手机时顶撞我。",
  "source": "chat"
}
```

此时服务端保留当前团队和当前成员专家。不要重复传 `expert_team_id`，否则它会被当成用户重新选择专家团：当前成员将回到主协调专家，待确认转交卡也会失效。

### 5.3 协调专家提出转交：前端只渲染卡，不自行请求

当主协调专家判断团队中另一位成员更适合承接时，它返回 `team_handoff`。这不是一个单独的推送事件，而是任意 `state` 中的字段。

```json
{
  "message_id": "msg_handoff_001",
  "assistant": {
    "content": "这个问题更适合由家庭教育专家继续协助。",
    "status": "completed"
  },
  "team_handoff": {
    "handoff_id": "handoff_001",
    "status": "active",
    "team_id": "student_growth_expert_team",
    "source_message_id": "msg_handoff_001",
    "reason": "问题聚焦亲子沟通和家庭规则。",
    "proposed_by_expert_id": "career_plan_expert",
    "candidates": [
      {
        "expert_id": "family_education_expert",
        "name": "家庭教育专家",
        "mention_name": "家庭教育专家",
        "brief": "亲子沟通与家庭教育支持"
      }
    ]
  }
}
```

前端规则：

1. `team_handoff={}` 时不渲染卡。
2. 卡片只在 `status="active"`、当前上下文范围、且 `message_id` 已存在时可点击。
3. 卡片上展示 `reason`、候选人的 `name`/`brief`，但不能因展示文字或模型正文而提前切换当前专家。
4. 用户继续发送新的普通 `chat` 时，旧的 active 转交建议会失效；此后不要再允许点击旧卡。
5. 普通成员专家不会产生 `team_handoff`；当前实现只有主协调专家可以建议转交。

### 5.4 用户确认转交：`confirm_team_handoff`

用户点击转交卡的候选专家后，发送：

```json
{
  "action": "confirm_team_handoff",
  "context_scope": "profile",
  "source_message_id": "msg_handoff_001",
  "target_expert_id": "family_education_expert",
  "source": "team_handoff",
  "enable_thinking": false,
  "return_reasoning": false
}
```

| 字段 | 类型 | 必填 | 中文含义与规则 |
| --- | --- | --- | --- |
| `source_message_id` | 字符串 | 是 | 转交卡所在的助手消息 ID。直接使用 `team_handoff.source_message_id` 或该帧的 `message_id`。 |
| `target_expert_id` | 字符串 | 是 | 用户选中的候选专家 ID，只能从 `team_handoff.candidates[].expert_id` 取。 |
| `source` | 固定字符串 | 是 | 固定为 `team_handoff`。 |
| `content` | 不支持 | 否 | 不传。服务端会把触发转交的原始用户问题及必要上下文交给目标专家。 |

成功后有两个可观察结果：

- 历史来源消息的转交卡状态会变为 `selected`；前端应将原卡改为已确认、只读。
- 新一轮 SSE 中 `expert.active` 切换为目标专家，且 `expert.transition.source="team_handoff"`。

```json
{
  "expert": {
    "mode": "team",
    "team": {
      "team_id": "student_growth_expert_team",
      "name": "学生成长专家团",
      "coordinator_expert_id": "career_plan_expert"
    },
    "active": {
      "expert_id": "family_education_expert",
      "name": "家庭教育专家",
      "mention_name": "家庭教育专家",
      "is_coordinator": false
    },
    "activation": {
      "source": "explicit_or_restored",
      "is_default": false,
      "selection_source": "handoff_card"
    },
    "transition": {
      "status": "completed",
      "source": "team_handoff",
      "from_expert_id": "career_plan_expert",
      "to_expert_id": "family_education_expert",
      "source_message_id": "msg_handoff_001"
    }
  }
}
```

### 5.5 用户主动 @ 团队成员并提问：`switch_team_member`

在 UI 中用户可以看到“@家庭教育专家”；实际协议必须发送结构化 ID：

```json
{
  "action": "switch_team_member",
  "context_scope": "profile",
  "target_expert_id": "family_education_expert",
  "content": "我想直接请家庭教育专家分析孩子顶撞的问题。",
  "source": "toolbar",
  "enable_thinking": false,
  "return_reasoning": false
}
```

| 字段 | 类型 | 必填 | 中文含义与规则 |
| --- | --- | --- | --- |
| `target_expert_id` | 字符串 | 是 | 用户主动指定的当前专家，必须属于当前已激活专家团。 |
| `content` | 非空字符串 | 是 | 与切换同时发送给目标专家的用户问题。 |
| `source` | 固定字符串 | 是 | 固定为 `toolbar`，即便 UI 呈现为 @。 |

成功后，`expert.transition.source` 为 `toolbar`，`source_message_id` 为 `null`。前端必须等待 SSE 中的 `expert.active` 变更后，再更新全局“当前专家”。

不要只发送：

```json
{"action":"chat", "content":"@家庭教育专家 帮我看看"}
```

服务端不会从 `content` 文字中解析出专家身份。

### 5.6 停止生成：`stop`

```json
{
  "action": "stop",
  "source": "composer"
}
```

顶层 `run_id` 必须是当前仍在生成的 run ID，`context_data` 可省略。停止不会删除会话，也不会取消已经完成的专家转交或专家选择。

## 6. 完整请求示例

### 6.1 cURL：从选择专家团开始

```bash
curl --no-buffer -N \
  -X POST "http://127.0.0.1:8013/api/v2/sessions/chat/stream" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "X-SSE-Protocol: hailiang.sse.v2" \
  -H "X-Request-Id: req-team-001" \
  --data-raw '{
    "session_id": "session_team_demo_001",
    "run_id": "run_team_demo_001",
    "input": "{\\"action\\":\\"chat\\",\\"context_scope\\":\\"profile\\",\\"expert_team_id\\":\\"student_growth_expert_team\\",\\"content\\":\\"孩子最近不愿意和我沟通，怎么办？\\",\\"source\\":\\"chat\\",\\"enable_thinking\\":false,\\"return_reasoning\\":false}",
    "context_data": {
      "user_id": "user_001",
      "profile_id": "profile_001",
      "student_name": "小海"
    }
  }'
```

每轮都更换：

- `run_id`
- `X-Request-Id`
- `input.content` 或对应交互字段

同一条会话的 `session_id` 不变。

### 6.2 JavaScript：BFF 发送前序列化 `input`

```ts
const input = {
  action: "chat",
  context_scope: "profile",
  expert_team_id: selectedTeamId,
  content: userText,
  source: "chat",
  enable_thinking: false,
  return_reasoning: false,
};

const body = {
  session_id: sessionId,
  run_id: crypto.randomUUID(),
  input: JSON.stringify(input),
  context_data: {
    user_id: trustedUserId, // 由 BFF 登录态注入
    profile_id: profileId,
    student_name: studentName,
  },
};
```

浏览器的身份信息应先到 BFF；BFF 校验后才将可信 `user_id`、档案信息转发给算法服务。

## 7. SSE 事件和前端消费方式

### 7.1 Wire 格式

```text
event: state
data: {完整的 SseV2State JSON}

event: ping
data: {}

event: done
data: {与最后一个 state 相同的完整 SseV2State JSON}
```

| SSE 事件 | 含义 | 前端处理 |
| --- | --- | --- |
| `state` | 本轮最新完整展示状态 | 若 `seq` 大于该 `run_id` 已处理序号，则整帧替换当前助手占位消息。 |
| `ping` | 保活 | 忽略，不影响消息内容、状态或序号。 |
| `done` | 传输结束确认 | 标记本轮结束。内容与最后一个 `state` 相同，不重复追加或渲染。 |

服务端不会发送“文本 delta”事件。`assistant.content` 始终是截至当前帧的完整累计正文：

```ts
// 正确：替换
draftAssistant.content = state.assistant.content;

// 错误：会在每帧重复拼接旧正文
draftAssistant.content += state.assistant.content;
```

### 7.2 专家团前端最小状态机

```text
idle
  └─ 用户发送 chat ──> streaming
streaming
  ├─ 收到 state(seq 更大) ──> 替换当前助手快照
  ├─ 收到 team_handoff(active) ──> 展示可确认转交卡
  ├─ 收到 done ──> completed
  └─ 网络断开/错误 ──> recover_from_session

handoff_active
  ├─ 用户确认候选 ──> confirm_team_handoff + 新 run
  ├─ 用户普通追问 ──> chat + 新 run，旧卡失效
  └─ 用户切换成员 ──> switch_team_member + 新 run
```

发送普通聊天后，前端应立即：

1. 追加用户气泡。
2. 创建该 `run_id` 的空助手气泡（`status=streaming`）。
3. 按 `run_id + seq` 更新该气泡。
4. 在终态或 `done` 后结束加载状态。

不要等待完整模型回复后才同时追加两条气泡。

## 8. 专家团相关响应字段

每个 `state`/`done` 都遵循固定对象形状。专家团前端至少应消费以下字段。

### 8.0 完整 `SseV2State` 数据格式骨架

无论当前处于协调专家回答、成员专家回答、转交确认、流式中、已完成或失败状态，`state.data` 与 `done.data` 均使用下面这份固定骨架。**字段始终存在**；没有内容时使用 `{}`、`[]`、空字符串或 `null`，前端不要通过“字段是否存在”判断业务状态。

```json
{
  "protocol": "hailiang.sse.v2",
  "session_id": "session_01J...",
  "run_id": "run_01J...",
  "seq": 12,
  "ts": "2026-09-02T08:30:00+00:00",
  "elapsed_ms": 1280,

  "message_id": "msg_01J...",

  "profile_id": "profile_001",
  "profile_name": "小海",
  "context_scope": "profile",
  "context_label": "小海",
  "context_switched": false,
  "context_notice": {},
  "branch_version": 3,
  "profile_context_status": "matched",
  "session_created": false,
  "profile_switched": false,

  "status": "streaming",
  "assistant": {
    "content": "截至当前帧已经生成的完整 Markdown 正文",
    "status": "streaming"
  },

  "intent": {
    "status": "streaming",
    "steps": [
      {
        "id": "intent",
        "label": "正在识别本轮需求",
        "detail": "",
        "status": "completed"
      },
      {
        "id": "planner",
        "label": "正在制定规划思路",
        "detail": "",
        "status": "active"
      }
    ]
  },

  "form": {},
  "path_options": {},
  "skill_rooms": [],

  "team_handoff": {
    "handoff_id": "handoff_01J...",
    "status": "active",
    "team_id": "student_growth_expert_team",
    "source_message_id": "msg_01J...",
    "reason": "该问题更适合由家庭教育专家继续处理。",
    "proposed_by_expert_id": "career_plan_expert",
    "candidates": [
      {
        "expert_id": "family_education_expert",
        "name": "家庭教育专家",
        "mention_name": "家庭教育专家",
        "brief": "亲子沟通与家庭教育支持"
      }
    ]
  },

  "expert": {
    "mode": "team",
    "team": {
      "team_id": "student_growth_expert_team",
      "name": "学生成长专家团",
      "coordinator_expert_id": "career_plan_expert"
    },
    "active": {
      "expert_id": "career_plan_expert",
      "name": "升学规划专家",
      "mention_name": "升学规划专家",
      "is_coordinator": true
    },
    "activation": {
      "source": "team_default_coordinator",
      "is_default": true,
      "selection_source": "manual_team"
    },
    "transition": {}
  },

  "skill_transition": {},
  "session": {
    "active_skill": {}
  },

  "risk": {
    "status": "passed",
    "stage": "output",
    "blocked": false,
    "message": ""
  },
  "error": {
    "code": "",
    "message": "",
    "upstream_detail": "",
    "retryable": false,
    "terminal": false
  }
}
```

这是一张“主协调专家已提出成员转交”的完整示例，因此 `team_handoff` 非空、`expert.active` 仍是主协调专家。下面是同一对象在不同阶段应出现的关键变化：

| 场景 | `status` | `assistant.content` | `team_handoff` | `expert.active` | `expert.transition` |
| --- | --- | --- | --- | --- | --- |
| 刚开始建流 | `streaming` | 通常为空字符串 | `{}` | 已能看到协调专家或当前成员 | `{}` |
| 协调专家流式回答 | `streaming` | 每个 `state` 是完整累计正文 | `{}` 或当前卡 | 协调专家 | `{}` |
| 协调专家建议转交 | `completed` 或 `streaming` | 建议说明正文 | `status="active"` 的完整对象 | 仍是协调专家 | `{}` |
| 用户已确认转交，成员开始回答 | `streaming` | 目标成员的完整累计正文 | `{}` | 目标成员 | `source="team_handoff"` |
| 用户工具栏主动换成员 | `streaming` | 目标成员的完整累计正文 | `{}` | 目标成员 | `source="toolbar"` |
| 本轮失败 | `failed` | 已生成部分或空字符串 | 以最后状态为准 | 以最后状态为准 | 以最后状态为准，通常为 `{}` |

### 8.0.1 骨架每个字段的中文含义

下表覆盖上方完整骨架中的每一个顶层字段，以及骨架中直接出现的嵌套字段。`expert`、`team_handoff` 的成员级字段在后续 8.3、8.4 节继续逐项展开。

| 字段 | 类型 / 空值 | 中文含义 | 前端处理规则 |
| --- | --- | --- | --- |
| `protocol` | 固定字符串 | SSE 协议标识 | 必须等于 `hailiang.sse.v2`。不一致的帧直接丢弃。 |
| `session_id` | 字符串 | 本条状态归属的会话 ID | 必须与正在展示的 session 一致。 |
| `run_id` | 字符串 | 本次流式执行 ID | 只更新同一 `run_id` 创建的助手占位气泡。 |
| `seq` | 正整数 | 同一 run 的状态序号 | 仅接受严格大于本地已处理值的帧。 |
| `ts` | ISO 8601 字符串 | 该状态帧生成时间 | 用于调试、性能记录；不是用户消息创建时间。 |
| `elapsed_ms` | 整数 | 本轮从服务端开始到当前帧的耗时（毫秒） | 可用于调试信息和慢请求提示。 |
| `message_id` | 字符串或 `null` | 当前助手消息的稳定 ID | 转交卡、历史交互的来源 ID。`null` 时需要消息 ID 的按钮不可点。 |
| `profile_id` | 字符串或 `null` | 本轮实际使用的孩子档案 ID | `unbound` 时必须为 `null`；不能用它推断其他时间线消息的范围。 |
| `profile_name` | 字符串或 `null` | 当前孩子档案显示名 | 作为气泡/范围标签；`unbound` 时为 `null`。 |
| `context_scope` | `profile` / `unbound` | 当前模型上下文范围 | 每条消息都按此字段标记范围和交互可用性。 |
| `context_label` | 字符串 | 当前范围的用户可见名称 | 可直接显示，例如“张毅”或“未绑定孩子”。 |
| `context_switched` | 布尔值 | 本轮是否从另一个范围切换而来 | 为 `true` 时可显示“已切换上下文”系统提示。 |
| `context_notice` | 对象或 `{}` | 服务端下发的上下文切换提示 | 仅真正切换范围时非空；前端应使用其 `text` 作为系统提示，不能拼进 `assistant.content`。 |
| `context_notice.type` | 固定为 `profile_switched` | 上下文变更类型 | 用于选择“上下文切换”系统气泡样式。 |
| `context_notice.text` | 字符串 | 可直接展示的中文提示 | 例如“已切换为孩子「小明」的上下文，后续回答将结合该孩子的信息。” |
| `context_notice.from_context_scope` | `profile` / `unbound` | 切换前范围 | 用于时间线与调试信息。 |
| `context_notice.from_profile_id` | 字符串或 `null` | 切换前孩子档案 ID | 从未绑定范围切出时为 `null`。 |
| `context_notice.from_context_label` | 字符串 | 切换前范围显示名 | 可在受控的“从 A 切到 B”提示中使用。 |
| `context_notice.to_context_scope` | `profile` / `unbound` | 切换后范围 | 应与本帧 `context_scope` 一致。 |
| `context_notice.to_profile_id` | 字符串或 `null` | 切换后孩子档案 ID | 应与本帧 `profile_id` 一致；切到未绑定范围时为 `null`。 |
| `context_notice.to_context_label` | 字符串 | 切换后范围显示名 | 应与本帧 `context_label` 一致。 |
| `branch_version` | 非负整数 | 当前范围的本地上下文分支版本 | 用于调试/同步；不是对外档案版本。 |
| `profile_context_status` | `matched` / `mismatched` / `unbound` | `input` 与转发上下文的匹配结果 | 正常孩子请求应为 `matched`；其他值应记录并检查请求构造。 |
| `session_created` | 布尔值 | 本次动作是否首次创建了该 session | 用于页面初始化，不代表是否创建孩子档案。 |
| `profile_switched` | 布尔值 | 本次是否切换了 session 当前范围 | 为 `true` 时刷新当前专家团/专家展示；历史消息仍保留标签。 |
| `status` | 状态枚举 | 本轮总体生命周期状态 | `streaming` 显示生成中；`completed`/`stopped`/`failed`/`blocked`/`superseded` 为终态。 |
| `assistant` | 对象 | 当前助手气泡的正文与状态 | 整体替换本地 draft，不做字符串增量拼接。 |
| `assistant.content` | 字符串 | 截至当前帧生成的完整 Markdown 正文 | 用 Markdown 组件渲染；每帧直接覆盖旧 content。 |
| `assistant.status` | 状态枚举 | 助手正文的生成状态 | 通常与顶层 `status` 一致；以顶层状态判断 run 是否结束。 |
| `intent` | 对象或 `{}` | 可展示的执行进度 | 可以渲染为“正在处理”进度，不是模型原始思维链。空对象不渲染。 |
| `intent.status` | 字符串 | 进度整体状态 | 常见为 `streaming` / `completed`。 |
| `intent.steps` | 数组 | 有序进度步骤 | 按服务端顺序展示；不根据本地猜测补步骤。 |
| `intent.steps[].id` | 字符串 | 步骤稳定键 | 同一 ID 用于替换该步骤状态。 |
| `intent.steps[].label` | 字符串 | 可直接展示的中文步骤名 | 原样显示。 |
| `intent.steps[].detail` | 字符串 | 辅助诊断描述 | 正式前端默认不展示；受控调试可展示。 |
| `intent.steps[].status` | 字符串 | 单步骤状态 | 常见 `active` / `completed`。 |
| `form` | 对象或 `{}` | 当前轮的结构化信息填写面板 | 非空时按统一表单组件渲染；空对象不渲染。它不改变专家团/成员路由。 |
| `form.form_id` | 字符串 | 表单实例 ID | 与 `interaction_id` 一起关联表单状态。 |
| `form.title` / `form.description` | 字符串 | 表单标题 / 辅助说明 | 原样展示。 |
| `form.status` | `active` / `submitted` / `expired` | 表单交互状态 | 仅 `active` 可编辑；其他状态只读。 |
| `form.interaction_id` | 字符串 | 表单交互 ID | 用于后续交互状态同步。 |
| `form.fields` | 数组 | 当前需要填写的字段 | 按返回顺序渲染。 |
| `form.fields[].fact_key` | 字符串 | 字段稳定键 | 作为填写结果的字段键。 |
| `form.fields[].label` / `placeholder` / `example` | 字符串 | 字段标题、占位提示、示例 | 原样展示。 |
| `form.fields[].input_type` | `text` / `single_select` / `multi_select` | 控件类型 | 分别使用文本、单选、多选控件；未知类型可降级文本。 |
| `form.fields[].required` | 布尔值 | 是否必填 | 提交前校验。 |
| `form.fields[].options` | 数组 | 单选/多选候选项 | 每项使用返回的 `label` 展示、`value` 提交。 |
| `form.fields[].submit_mode` | `auto` / `manual` | 提交方式 | `auto` 可随填写自动提交；`manual` 显示提交按钮。 |
| `form.fields[].scope` | 字符串 | 表单值所属数据范围 | 只作展示/提交上下文，不能跨当前范围写入。 |
| `form.fields[].value_type` | 字符串 | 字段值 JSON 类型 | 保持服务端要求的字符串、数组等类型。 |
| `form.fields[].max_selections` | 正整数或不存在 | 多选最大数量 | 存在时限制多选数量。 |
| `path_options` | 对象或 `{}` | 保留的路径选择展示模块 | 不属于专家团转交协议；不应据此切换专家团或成员。当前团队 UI 不支持时可不渲染。 |
| `skill_rooms` | 数组 | 保留的通用推荐展示模块 | 不属于专家团转交协议；团队 UI 不应从中推断当前专家。 |
| `team_handoff` | 对象或 `{}` | 主协调专家提出的成员转交卡 | 非空时按 8.4 节渲染，只有 active 卡可确认。 |
| `expert` | 固定对象 | 当前专家团、实际回答专家和最近切换状态 | 当前团队/专家只以该对象为权威，详见 8.3 节。 |
| `skill_transition` | 对象或 `{}` | 保留的运行时转场展示状态 | 不作为专家团选择、专家转交或成员切换的依据。 |
| `session` | 对象 | 当前会话的运行时摘要 | 专家团 UI 只作兼容保存；当前团队和专家仍以 `expert` 为准。 |
| `session.active_skill` | 对象或 `{}` | 当前运行时主题摘要 | 不是专家团 ID、专家 ID 或转交依据。 |
| `risk` | 固定对象 | 安全检查结果 | 以 `blocked` 与 `message` 驱动安全提示。 |
| `risk.status` | 字符串 | 安全检查状态 | 常见 `idle` / `checking` / `passed`。 |
| `risk.stage` | 字符串 | 当前安全检查阶段 | 用于调试/辅助提示。 |
| `risk.blocked` | 布尔值 | 是否已拦截本轮输出 | 为 `true` 时不显示被拦截正文或交互。 |
| `risk.message` | 字符串 | 可面向用户展示的安全提示 | 可直接用于安全提示区。 |
| `error` | 固定对象 | 本轮错误状态 | `status="failed"` 时展示其公开字段。 |
| `error.code` | 字符串 | 稳定错误码 | 用于埋点、分类和重试策略。 |
| `error.message` | 字符串 | 面向用户的错误说明 | 正式界面可展示。 |
| `error.upstream_detail` | 字符串 | 上游诊断信息 | 仅受控调试台可展开，生产界面不展示。 |
| `error.retryable` | 布尔值 | 是否可由用户重新发起 | `true` 时可以显示“重新发送”。 |
| `error.terminal` | 布尔值 | 是否终止本轮 | 为 `true` 时停止 loading。 |

### 8.0.2 转交确认后的完整关键片段

用户确认 `team_handoff` 后，新 `run_id` 的 SSE 状态应至少呈现如下关系。来源消息的卡片会在历史记录中变为 `selected`；**新成员回复不重复带旧卡**。

```json
{
  "protocol": "hailiang.sse.v2",
  "session_id": "session_01J...",
  "run_id": "run_confirm_handoff_01J...",
  "seq": 5,
  "message_id": "msg_member_reply_01J...",
  "status": "streaming",
  "assistant": {
    "content": "你好，我是家庭教育专家。关于孩子顶撞的问题，我们先……",
    "status": "streaming"
  },
  "team_handoff": {},
  "expert": {
    "mode": "team",
    "team": {
      "team_id": "student_growth_expert_team",
      "name": "学生成长专家团",
      "coordinator_expert_id": "career_plan_expert"
    },
    "active": {
      "expert_id": "family_education_expert",
      "name": "家庭教育专家",
      "mention_name": "家庭教育专家",
      "is_coordinator": false
    },
    "activation": {
      "source": "explicit_or_restored",
      "is_default": false,
      "selection_source": "handoff_card"
    },
    "transition": {
      "status": "completed",
      "source": "team_handoff",
      "from_expert_id": "career_plan_expert",
      "to_expert_id": "family_education_expert",
      "source_message_id": "msg_01J..."
    }
  },
  "risk": {
    "status": "passed",
    "stage": "output",
    "blocked": false,
    "message": ""
  },
  "error": {
    "code": "",
    "message": "",
    "upstream_detail": "",
    "retryable": false,
    "terminal": false
  }
}
```

### 8.1 流与会话字段

| 字段 | 类型 | 中文含义 | 前端规则 |
| --- | --- | --- | --- |
| `protocol` | 固定字符串 | 协议版本 | 必须为 `hailiang.sse.v2`，否则丢弃该帧。 |
| `session_id` | 字符串 | 会话 ID | 校验是否属于当前会话；不展示为正文。 |
| `run_id` | 字符串 | 当前流执行 ID | 只更新对应助手占位消息。 |
| `seq` | 正整数 | 当前 run 内的严格递增序号 | 仅接收大于已处理 seq 的帧。 |
| `ts` | ISO 8601 字符串 | 服务端本帧时间 | 调试、链路追踪；不作为用户消息创建时间。 |
| `elapsed_ms` | 整数 | 本轮已耗时毫秒数 | 可用于受控调试展示。 |
| `message_id` | 字符串或 `null` | 助手消息 ID | 转交卡确认所需的稳定 ID；为 `null` 时卡片不可点击。 |
| `status` | 枚举 | 本轮总体运行状态 | 控制 loading、失败、停止与终态。 |
| `assistant.content` | 字符串 | 截至当前帧的完整 Markdown 正文 | 直接替换当前助手气泡正文。 |
| `assistant.status` | 枚举 | 助手正文生成状态 | 通常跟随顶层 `status`。 |

### 8.2 上下文范围字段

| 字段 | 类型 | 中文含义 | 前端规则 |
| --- | --- | --- | --- |
| `context_scope` | `profile` / `unbound` | 本轮采用的上下文范围 | 为每条消息显示“孩子名称”或“未绑定孩子”。 |
| `context_label` | 字符串 | 范围显示名 | 可直接显示。 |
| `profile_id` | 字符串或 `null` | 实际生效的孩子档案 ID | `unbound` 时必须为 `null`。 |
| `profile_name` | 字符串或 `null` | 孩子档案显示名 | `unbound` 时必须为 `null`。 |
| `context_switched` | 布尔值 | 本轮是否从 session 的其他范围切换而来 | 需要时插入范围切换提示。 |
| `branch_version` | 非负整数 | 当前范围分支版本 | 用于前端上下文同步和调试。 |
| `profile_context_status` | `matched` / `mismatched` | 请求目标和转发上下文是否一致 | 正常业务调用应是 `matched`；`mismatched` 应记录并排查。 |
| `session_created` | 布尔值 | 本轮是否创建新会话 | 用于初始化页面状态。 |
| `profile_switched` | 布尔值 | 本轮是否切换了会话当前孩子 | 仅作界面同步，不允许据此混用历史。 |

### 8.3 `expert`：专家团和当前专家的权威状态

```json
{
  "expert": {
    "mode": "team",
    "team": {
      "team_id": "student_growth_expert_team",
      "name": "学生成长专家团",
      "coordinator_expert_id": "career_plan_expert"
    },
    "active": {
      "expert_id": "family_education_expert",
      "name": "家庭教育专家",
      "mention_name": "家庭教育专家",
      "is_coordinator": false
    },
    "transition": {
      "status": "completed",
      "source": "team_handoff",
      "from_expert_id": "career_plan_expert",
      "to_expert_id": "family_education_expert",
      "source_message_id": "msg_handoff_001"
    }
  }
}
```

| 字段 | 值/类型 | 中文含义 | 前端规则 |
| --- | --- | --- | --- |
| `expert.mode` | `team` / `single` / `none` | 当前承接模式 | 本文目标为 `team`。`none` 是非专家团对话；不能将其伪装成专家团。 |
| `expert.team.team_id` | 字符串或空对象 | 当前绑定专家团 ID | 当前团队的唯一权威来源。 |
| `expert.team.name` | 字符串 | 当前专家团名 | 展示在会话顶部。 |
| `expert.team.coordinator_expert_id` | 字符串 | 主协调专家 ID | 展示协调角色、辅助调试。 |
| `expert.active.expert_id` | 字符串 | **本轮实际承接/回答的专家** | 当前专家 UI 的唯一权威来源。 |
| `expert.active.name` | 字符串 | 当前专家显示名 | 气泡头、状态栏展示。 |
| `expert.active.mention_name` | 字符串 | 当前专家的 @ 显示名 | 仅 UI 文案；不能作为 API 参数。 |
| `expert.active.is_coordinator` | 布尔值 | 当前专家是否主协调专家 | 作为“主协调专家”徽标。 |
| `expert.activation` | 对象或 `{}` | 当前专家的承接来源 | `is_default=true` 时显示“由主协调专家默认承接”。 |
| `activation.source` | `team_default_coordinator` / `explicit_or_restored` | 默认承接或其他显式/恢复承接 | 不用于判断转交；转交仍以 `expert.transition` 为准。 |
| `activation.is_default` | 布尔值 | 是否专家团选择后的默认主协调承接 | 默认承接徽标的唯一判断依据。 |
| `activation.selection_source` | 字符串 | 服务端选择来源的调试说明 | 仅展示/调试，不能用来在浏览器中路由。 |
| `expert.transition` | 对象或 `{}` | 本轮审计到的专家切换 | 空对象表示本轮没有“切换动作”记录。 |
| `transition.status` | 通常为 `completed` | 本次切换完成状态 | 与来源一起写入时间线/调试信息。 |
| `transition.source` | `toolbar` / `team_handoff` | 切换来源 | 解释“用户主动指定”或“确认协调专家建议”。 |
| `transition.from_expert_id` | 字符串 | 切换前专家 ID | 调试和时间线。 |
| `transition.to_expert_id` | 字符串 | 切换后专家 ID | 调试和时间线。 |
| `transition.source_message_id` | 字符串或 `null` | 触发切换的消息 ID | 转交确认时为卡片消息 ID；工具栏切换为 `null`。 |

特别说明：在 `chat` 中首次选择 `expert_team_id`，或通过 `chat.expert_id` 显式指定专家时，当前实现可能不填 `expert.transition`。此时应以本轮请求参数和最终 `expert.active` 一起记录“用户显式选择”，不要把空 `transition` 误判成失败。

### 8.4 `team_handoff`：专家转交确认卡

| 字段 | 类型 | 中文含义 | 前端规则 |
| --- | --- | --- | --- |
| `handoff_id` | 字符串 | 本次转交建议实例 ID | 卡片 React key/调试关联；不传回确认接口。 |
| `status` | `active` / `selected` / 其他 | 卡片交互状态 | 仅 `active` 的当前范围卡可点击；其余只读。 |
| `team_id` | 字符串 | 提出转交建议的团队 ID | 用于显示、调试校验。 |
| `source_message_id` | 字符串 | 卡片所在助手消息 ID | 确认请求必须原样使用。 |
| `reason` | 字符串 | 主协调专家给出的转交原因 | 可面向用户展示，长度不应由前端截断后再回传。 |
| `proposed_by_expert_id` | 字符串 | 提出建议的专家 ID | 记录“谁建议转交”。 |
| `candidates` | 数组 | 可接管候选专家列表 | 为每项渲染一个明确确认入口。 |
| `candidates[].expert_id` | 字符串 | 候选专家 ID | 点击后作为 `target_expert_id` 传回。 |
| `candidates[].name` | 字符串 | 候选专家显示名 | 卡片按钮文案。 |
| `candidates[].mention_name` | 字符串 | 候选专家 @ 名 | 辅助展示。 |
| `candidates[].brief` | 字符串 | 候选专家能力简介 | 卡片说明。 |

### 8.5 运行状态、风险与错误

| 字段 | 典型值 | 含义 | 前端规则 |
| --- | --- | --- | --- |
| `status` | `streaming` | 生成仍在进行 | 显示 loading、可发送 stop。 |
| `status` | `completed` | 本轮正常完成 | 结束 loading，保留正文与有效交互。 |
| `status` | `stopped` | 用户停止 | 保留已收到正文；不要继续显示该轮新卡片。 |
| `status` | `superseded` | 被新输入替代 | 标记旧占位消息被取代，不要把未完成内容当正式答复。 |
| `status` | `blocked` | 安全拦截 | 只展示 `risk.message`，不展示被拦截正文。 |
| `status` | `failed` | 执行失败 | 展示 `error.code` 与 `error.message`。 |
| `intent` | 对象或 `{}` | 可展示执行进度，不是模型思维链 | 可做“正在分析/正在整理”进度；不要将其当作正式事实。 |
| `risk.status` | `idle` / `checking` / `passed` | 风控处理状态 | 驱动通用安全提示。 |
| `risk.blocked` | 布尔值 | 是否已拦截 | 为 `true` 时进入 blocked UI。 |
| `risk.message` | 字符串 | 可向用户展示的安全提示 | 可以展示。 |
| `error.code` | 字符串 | 稳定错误码 | 用于埋点、重试判定。 |
| `error.message` | 字符串 | 可展示错误描述 | 向用户展示。 |
| `error.retryable` | 布尔值 | 是否可由用户重新发起 | `true` 时可显示“重新发送”。 |
| `error.terminal` | 布尔值 | 是否为本轮终止错误 | 为 `true` 时结束本轮 loading。 |
| `error.upstream_detail` | 字符串 | 上游诊断详情 | 仅受控调试台可展开，生产 UI 不展示。 |

## 9. 真实交互时序

### 9.1 选择专家团 → 协调专家回答 → 建议转交 → 用户确认

```text
前端                  BFF                    算法服务                   主协调专家 / 成员
 │ 选择专家团+提问       │                         │                              │
 │─────────────────────>│ POST chat              │                              │
 │                      │────────────────────────>│ 绑定团队、主协调专家         │
 │                      │<────────────────────────│ state: expert=协调专家       │
 │<─────────────────────│ 原样转发 SSE            │                              │
 │                      │<────────────────────────│ state: team_handoff=active   │
 │<─────────────────────│ 原样转发 SSE            │                              │
 │ 点击候选专家          │                         │                              │
 │─────────────────────>│ POST confirm_team_handoff                              │
 │                      │────────────────────────>│ 校验卡、切换成员             │
 │                      │<────────────────────────│ state: expert.transition     │
 │<─────────────────────│ 原样转发 SSE            │                              │
 │                      │<────────────────────────│ state*: 成员流式回答         │
 │<─────────────────────│                         │                              │
 │                      │<────────────────────────│ done                         │
 │<─────────────────────│                         │                              │
```

### 9.2 用户主动指定成员

```text
用户在当前专家团的成员菜单选择“家庭教育专家”并写下问题
  → 前端发送 switch_team_member(target_expert_id, content)
  → 服务端校验目标是当前团队成员
  → SSE 返回 expert.transition.source="toolbar"
  → SSE 返回 expert.active=家庭教育专家及其回答
```

### 9.3 前端状态更新伪代码

```ts
const latestSeqByRun = new Map<string, number>();

function onSseEvent(event: { event: string; data: string }) {
  if (event.event === "ping") return;

  const state = JSON.parse(event.data);
  if (state.protocol !== "hailiang.sse.v2") return;

  if (event.event === "state") {
    const previousSeq = latestSeqByRun.get(state.run_id) ?? 0;
    if (state.seq <= previousSeq) return;
    latestSeqByRun.set(state.run_id, state.seq);

    replaceAssistantDraft(state.run_id, {
      messageId: state.message_id,
      content: state.assistant.content,
      status: state.status,
      contextScope: state.context_scope,
      contextLabel: state.context_label,
      teamHandoff: state.team_handoff,
      expert: state.expert,
      error: state.error,
    });
    setCurrentTeam(state.expert.team);
    setCurrentExpert(state.expert.active);
  }

  if (event.event === "done") {
    markRunFinished(state.run_id, state.status);
  }
}
```

## 10. BFF 转发要求

### 必须做

- 从登录态取得可信 `user_id`，并校验 `session_id` 与 `profile_id` 的归属。
- 为每个非停止动作创建新的全局唯一 `run_id`。
- 注入可信的 `context_data`，再调用算法服务。
- 低延迟逐字节转发 `event:`、`data:`、空行分隔符以及 UTF-8 边界。
- 浏览器主动取消时取消上游 HTTP 请求。
- 透传建流前的 HTTP 状态码、响应头、`Retry-After` 与普通 JSON 错误。
- 使用 `X-Request-Id` 关联日志，但不要记录完整问题、孩子资料、SSE 正文或授权信息。

### 不能做

- 不能先收完模型回复再转给浏览器。
- 不能把 SSE 聚合成普通 JSON、WebSocket 自定义事件或自创 delta 协议。
- 不能根据用户文本自行选专家团、专家或转交目标。
- 不能因网络不确定而透明重试同一个 POST SSE 请求。
- 不能自动确认 `team_handoff`；必须由用户明确点击候选专家。

Nginx/网关至少满足：

```nginx
proxy_http_version 1.1;
proxy_buffering off;
proxy_cache off;
gzip off;
proxy_read_timeout 180s;
```

## 11. 错误码与前端处理

建流前错误是普通 JSON（不是 SSE）。常见专家团相关错误如下。

| HTTP | 错误码/`detail` | 含义 | 前端处理 |
| --- | --- | --- | --- |
| 422 | `EXPERT_TEAM_NOT_FOUND` | 指定专家团不存在或当前不可用 | 刷新专家团目录，要求重新选择。 |
| 422 | `EXPERT_NOT_FOUND` | 指定专家不存在或当前不可用 | 刷新专家目录。 |
| 422 | `EXPERT_NOT_IN_ACTIVE_TEAM` | 指定专家不属于当前已激活专家团 | 不切换，刷新当前团队成员列表。 |
| 404 | `TEAM_HANDOFF_SOURCE_NOT_FOUND` | 转交卡来源消息找不到 | 刷新会话历史并移除本地旧卡。 |
| 409 | `TEAM_HANDOFF_NOT_ACTIVE` | 转交卡已被确认、过期或被新对话替代 | 卡片置为只读，刷新会话状态。 |
| 422 | `TEAM_HANDOFF_TARGET_NOT_ALLOWED` | 目标专家不在该卡候选范围 | 不重试，使用服务端返回候选重新渲染。 |
| 409 | `RUN_ID_CONFLICT` | 重复使用了非停止 run ID | 生成新 `run_id`；不要重放已提交请求。 |
| 409 | `SESSION_UPDATE_CONFLICT` | 会话并发更新冲突 | 短暂退避后读取最新会话；由用户决定是否重新发送。 |
| 409 | `ACTIVE_RUN_MUST_STOP` | 切换孩子范围时旧 run 尚未结束 | 先停止旧 run，等待终态后再切换范围。 |
| 422 | `PROFILE_ID_REQUIRED` / `PROFILE_CONTEXT_REQUIRED` | profile 范围缺少必要档案字段 | 修正请求。 |
| 422 | `UNBOUND_CONTEXT_MUST_NOT_INCLUDE_PROFILE_ID` | 未绑定范围仍传了孩子 ID | 移除所有孩子字段后重发。 |
| 429 | `LLM_RATE_LIMITED` / 容量错误 | 模型限流或并发容量不足 | 按 `Retry-After` 提示稍后再试。 |

HTTP 200 建流后发生异常时，不会再改 HTTP 状态码；前端从最后一个 `state` 的 `status="failed"` 和 `error` 字段读取结果。

## 12. 断线、刷新与历史恢复

SSE v2 不支持 `Last-Event-ID` 断点续传。

| 场景 | 正确处理 |
| --- | --- |
| 已收到任意 `state` 后断线 | 不重放同一请求，不复用同一 `run_id`。读取会话历史，恢复已持久化状态。 |
| 浏览器刷新 | 调用会话详情/上下文接口，恢复消息、当前专家团、当前专家和卡片状态。 |
| 卡片显示 active 但点击返回 409 | 以服务端会话历史为准，将本地卡改为失效。 |
| 用户发送下一条普通消息 | 使用新 `run_id`；旧未确认转交卡不再可操作。 |

建议恢复顺序：

```http
GET /api/v1/sessions/{session_id}
GET /api/v1/sessions/{session_id}/context
```

历史消息与实时消息必须复用同一套组件。读取历史时，专家转交卡也应根据保存的 `team_handoff.status` 呈现 active、selected 或只读状态，并继续遵守上下文范围隔离。

## 13. 联调验收清单

### 选择与续聊

- [ ] `GET /api/v1/expert-teams` 可以拉取团队、协调专家和成员 ID。
- [ ] 首轮 `chat` 带 `expert_team_id` 后，SSE 的 `expert.mode` 为 `team`。
- [ ] 首轮 `expert.active.expert_id` 为该团队的 `coordinator_expert_id`。
- [ ] 同一范围续聊不带 `expert_team_id` 时，当前成员专家不被错误重置为协调专家。
- [ ] 用户主动换专家团时才重新带 `expert_team_id`，并能观察到新的协调专家。

### 转交卡

- [ ] 主协调专家下发 `team_handoff.status="active"` 时，前端展示候选专家卡。
- [ ] 卡片不可仅因正文包含“转交”而出现；必须由结构化 `team_handoff` 驱动。
- [ ] 点击候选专家发送 `confirm_team_handoff`，并正确传递 `source_message_id` 与 `target_expert_id`。
- [ ] 确认成功后来源卡变为 `selected`，且下一轮 `expert.transition.source="team_handoff"`。
- [ ] 用户在卡片出现后先发普通消息，旧卡变为不可点击。

### 主动 @ 和流式体验

- [ ] 用户 @ 成员时发送 `switch_team_member`，而不是把名称拼到 `content`。
- [ ] 返回中 `expert.transition.source="toolbar"`，并以 `expert.active` 更新当前专家。
- [ ] 用户气泡在请求发出时立即出现；助手正文随每个 `state` 整帧替换更新。
- [ ] 对相同 `run_id`，旧 `seq` 和重复 `seq` 不会覆盖新状态。
- [ ] `done` 不会让正文重复出现一次。

### 上下文、错误和安全

- [ ] `profile` 与 `unbound` 请求字段符合各自约束。
- [ ] 不能在孩子 A 的范围点击孩子 B 范围产生的转交卡。
- [ ] BFF 不记录完整个人资料或 SSE 正文。
- [ ] 异常时能正确处理建流前 HTTP 错误和流内 `error`。
- [ ] 前端不会自动重试不确定是否已到达服务端的 POST SSE 请求。

## 14. 与其他 SSE 文档的关系

本文件是专家团业务对接的独立入口。需要查看底层完整字段或非专家团能力时，再参考：

- [SSE v2 前端与转发后端联调指南](SSE_V2_INTEGRATION_GUIDE.md)：全量 SSE v2 接入与通用运行规则。
- [SSE v2 前端对齐协议](SSE_RESPONSE_CONTRACT.md)：完整响应对象、所有 UI 模块和状态字段。
- [API 文档](API_DOCUMENTATION.md)：会话、档案、Facts 等普通 JSON 接口。
