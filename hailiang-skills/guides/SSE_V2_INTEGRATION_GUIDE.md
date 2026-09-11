# SSE v2 前端与转发后端联调指南

> 文档状态：当前实现（2026-09-01）
>
> 协议名称：`hailiang.sse.v2`
>
> 算法服务入口：`POST /api/v2/sessions/chat/stream`
>
> 适用对象：业务前端、项目转发后端（BFF）、联调与测试人员

> **严格请求字段更新（2026-09-03）：** `input.profile_id`、顶层
> `expert_team_id` 与顶层 `expert_id` 已不再兼容。所有非停止动作必须携带
> `expert_context`。请以
> [SSE_V2_EXPERT_CONTEXT_CONTRACT.md](SSE_V2_EXPERT_CONTEXT_CONTRACT.md) 为准；
> 本文中遗留的旧请求示例仅供理解历史流程，不能直接用于联调。

本文是 SSE v2 跨服务联调的统一入口。字段的详细渲染语义继续以
[SSE_RESPONSE_CONTRACT.md](SSE_RESPONSE_CONTRACT.md) 为准；普通 JSON 接口见
[API_DOCUMENTATION.md](API_DOCUMENTATION.md)。

## 1. 接入方只需要先记住这些规则

1. 普通聊天、普通模式下进入/退出 Skill、切换专家、确认专家转交和停止生成，都调用同一个接口：
   `POST /api/v2/sessions/chat/stream`。
2. 请求体顶层是 JSON，但其中 `input` 本身是一个 **JSON 字符串**，不是嵌套对象。
3. 除 `stop` 外，每轮都应显式选择 `context_scope` 并携带 `expert_context`：
   `profile` 时孩子只由 `context_data.profile_id` 表示；`input.profile_id` 会被
   拒绝。`unbound` 时 `context_data` 仅传可信 `user_id`。
4. 服务端对外只发送 `state`、`ping`、`done` 三种 SSE 事件。前端不消费 Runtime 内部事件。
5. 每个 `state.data` 都是完整状态快照，不是 patch，也不是文本 delta。
6. 同一 `run_id` 只接受 `seq` 更大的状态；`done` 与最后一个 `state` 相同，不重复渲染。
7. 普通动作必须使用新的 `run_id`；只有 `stop` 复用正在生成的 `run_id`。
8. BFF 必须逐字节、低延迟转发 SSE，禁止缓冲、聚合、压缩、重排或改写事件。
9. 收到任何 `state` 后断线，不自动重放原请求；通过会话历史恢复，再由用户决定是否重试。
10. `run_id` 是一次执行，`message_id` 是一条消息，两者不能互换。

新会话的普通 `chat` 不传 `expert_team_id`、`expert_id` 时，由通用对话 Runtime 承接：**大模型 + 当前 Soul**。但已在该 session 中显式选择专家团或专家后，后续 `chat + continue` 可省略两个 ID，服务端会沿用最近选择；只有工具栏显式切换和专家转交卡操作需要前端提供专家身份。

## 2. 调用链与职责边界

```text
浏览器
  │ 业务请求（浏览器不可信）
  ▼
项目 BFF
  ├─ 校验登录态、session/profile 归属
  ├─ 生成 session_id / run_id
  ├─ 注入可信 user_id、profile_id、student_name
  └─ 原样流式转发请求和响应
  ▼
算法服务 POST /api/v2/sessions/chat/stream
  ├─ 会话、并发、风控、专家团/专家/Skill Runtime
  ├─ 内部细粒度事件归并为 SSE v2 完整状态
  └─ state* → ping* → done → 关闭连接
```

浏览器无需、也不应知道算法服务内部的 `profile_id`。用户在页面选择孩子后，浏览器只向
BFF 传业务侧的选中孩子标识；BFF 校验该孩子属于当前登录用户，并映射为算法服务所需的
`context_data.profile_id`。同一 `session_id` 的下一轮请求只要携带不同的
`context_data.profile_id`，就表示切换孩子；算法服务会隔离旧分支并恢复或创建新孩子分支。
`input` 只承载动作、正文和专家/Skill 交互，不需要镜像孩子 ID。
普通 `chat` 固定传 `context_activation="auto"`：同一条请求会恢复 session 级最后活跃
专家（如已选择），无需浏览器先读取目标孩子分支或因版本未知重发消息。

### BFF 负责

- 从登录态获取可信 `user_id`，校验 `profile_id`、`session_id` 的访问权。
- 为非停止动作生成全局唯一 `run_id`；保留当前活动 run，供停止生成使用。
- 将算法服务的 HTTP 状态、响应头和 SSE 字节流及时转发给前端。
- 浏览器取消请求时取消算法服务上游请求。
- 使用 `X-Request-Id` 做跨服务日志关联，但不要把 SSE 正文、Prompt 或隐私 Facts 写入普通访问日志。

### BFF 不负责

- 不解析或重组 `state`，不把 SSE 包装成 WebSocket/普通 JSON 后再拼装。
- 不根据用户文本自行决定专家、专家团或 Skill。
- 不把 `run_id` 改写为 `message_id`，也不生成交互卡片。
- 不对已开始的 POST SSE 做透明重试。

### 前端负责

- 用户发送后立即显示用户消息和空的助手占位消息。
- 以 `run_id + seq` 接收完整快照并替换助手占位消息的展示状态。
- 按服务端返回的 `status`、表单、专家转交和 Skill 卡片控制交互。
- `done` 只作为传输完成确认，不再次追加正文。
- 刷新或断线后调用会话/上下文接口恢复，不重放旧 SSE。

## 3. HTTP 请求契约

### 3.1 请求头

```http
POST /api/v2/sessions/chat/stream HTTP/1.1
Content-Type: application/json
Accept: text/event-stream
X-SSE-Protocol: hailiang.sse.v2
X-Request-Id: req_全局唯一值
```

服务成功建流时返回：

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
X-SSE-Protocol: hailiang.sse.v2
```

### 3.2 顶层请求

```json
{
  "session_id": "sess_01J...",
  "run_id": "run_01J...",
  "input": "{\"action\":\"chat\",\"context_scope\":\"profile\",\"content\":\"孩子最近不愿意沟通，怎么办？\",\"source\":\"chat\"}",
  "context_data": {
    "student_name": "小海",
    "user_id": "user_123",
    "profile_id": "profile_123",
    "school_year": "2026-2027",
    "grade": "高一",
    "facts": {"student_province": "浙江"}
  }
}
```

| 字段 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- |
| `session_id` | 非空字符串 | 是 | 新会话由 BFF 生成；后续轮次复用。不能跨用户复用。 |
| `run_id` | 非空字符串 | 是 | 单次执行 ID。非停止动作不可重复；`stop` 必须复用活动 run。 |
| `input` | JSON 字符串 | 是 | 解码后必须是第 3.4 节中的一种严格对象；未知字段会被拒绝。 |
| `context_data` | 对象 | 非停止动作是 | 可信身份和可选业务上下文。`stop` 可不传。 |
| `debug_session_id` | 字符串 | 否 | 仅供获授权的工作台固定快照会话使用；普通业务前端不要传。 |

不要把 `input` 直接写成对象：

```json
{
  "input": {"action": "chat"}
}
```

上述请求会因为 `input` 不是字符串而返回 `422`。

### 3.3 `context_data`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `student_name` | 非空字符串 | `profile` 时是 | 当前档案展示名；`unbound` 时不得传。 |
| `user_id` | 非空字符串 | 是 | 由 BFF 从登录态注入，不能信任浏览器值。 |
| `profile_id` | 非空字符串 | `profile` 时是 | 当前孩子/档案 ID；`unbound` 时不得传。 |
| `school_year` | 非空字符串 | 否 | 如 `2026-2027`。 |
| `grade` | 非空字符串 | 否 | 如 `高一`。 |
| `facts` | 对象 | 否 | 仅已注册 Facts 会被校验并写入；未知键不会进入模型 Facts。 |
| 扩展字段 | 任意 JSON | 否 | 可作为转发元数据保留，不自动成为 Facts。 |

`context_scope: "profile"` 时，服务端以 `context_data.profile_id` 作为本轮明确目标。
`input.profile_id` 已被禁止；携带它会返回 `422 INPUT_PROFILE_ID_FORBIDDEN`。

`context_scope: "unbound"` 时，`context_data` 只能包含 `user_id`，不得包含孩子名称、档案 ID、年级或 Facts。该范围只使用当前 session 的未绑定分支，不读取或写入孩子/账户共享 Facts。

### 3.4 `input` 动作总表

以下字段是 `input` JSON 字符串解码后的内容。除 `stop` 外，所有动作都带 `context_scope` 和
`expert_context`。孩子只由顶层 `context_data.profile_id` 表示，`input.profile_id` 已禁止。

| action | source | 必填业务字段 | run_id 规则 |
| --- | --- | --- | --- |
| `chat` | `chat` / `toolbar` | `content`；`profile` 范围的孩子 ID 由 `context_data` 提供；`expert_context` 内选择或续用专家 | 新 run |
| `switch_team_member` | `toolbar` | `target_expert_id`、`content`；`expert_context` 必须回传当前团队和当前专家 | 新 run |
| `confirm_team_handoff` | `team_handoff` | `source_message_id`、`target_expert_id`；`expert_context` 必须回传当前团队和当前专家 | 新 run |
| `enter_skill` | `toolbar` | `target_skill_id`；孩子 ID 由 `context_data` 提供 | 新 run |
| `enter_skill` | `route_suggestion` | 上述字段加 `source_message_id`、`source_interaction_id` | 新 run |
| `quit_skill` | `toolbar` / `exit_button` | 当前 `target_skill_id`；孩子 ID 由 `context_data` 提供 | 新 run |
| `stop` | `composer` | 无 | 复用活动 run |

#### 专家团与专家字段的中文业务含义

| 字段 | 使用动作 | 中文业务含义 | 校验与使用规则 |
| --- | --- | --- | --- |
| `expert_context.expert_team_id` | `chat` | 本轮开始时用户**指定要由哪个专家团承接**；它会成为当前上下文范围持续使用的专家团。 | `select_team` 时必传，`expert_id` 必须为 `null`；指定后激活该团主协调专家。 |
| `expert_context.expert_id` | `chat` | `select_expert` 时是用户点选的**目标回答专家**；`continue` 时是当前专家断言。 | 必须是可用专家；选择团内成员时还必须属于该团队。服务端不从 `content` 的 `@专家名称` 文本猜测专家。 |
| `target_expert_id` | `switch_team_member`、`confirm_team_handoff` | 用户要切换或确认接管的**目标专家**。 | 工具栏切换时必须是当前团队成员；确认转交卡时还必须是该卡的有效候选。 |
| `source_message_id` | `confirm_team_handoff` | 产生专家转交卡的那条助手消息 ID，用来证明用户确认的是哪一张卡。 | 必须指向当前范围仍有效的 `team_handoff` 卡；不能自行生成、跨上下文范围或重复使用。 |
| `source` | 所有动作 | 动作来源，用于服务端校验、审计和前端解释。 | 普通对话为 `chat`，工具栏指定专家为 `toolbar`，确认专家转交卡为 `team_handoff`。 |

#### 用户唤起专家的三种方式

| 用户操作 | 请求形式 | 用户看到的业务效果 | SSE 中如何识别 |
| --- | --- | --- | --- |
| 在输入框点选 / @ 某位专家并提问 | `action="chat" + expert_id + content` | 在当前专家团内由该专家回答；未选专家团时进入单专家模式。 | `expert.active` 变为该专家。当前 v2 对这类显式选择不填 `expert.transition`，转发端应结合自己刚发送的 `expert_id` 识别为用户主动选择。 |
| 在专家团工具栏切换专家并提问 | `action="switch_team_member" + target_expert_id + content` | 切到目标专家后，由其回答同一条问题。 | `expert.transition.source="toolbar"`，并带切换前后专家 ID。 |
| 点击协调专家给出的转交确认卡 | `action="confirm_team_handoff" + source_message_id + target_expert_id` | 用户确认由建议的成员专家接管。 | 原消息的 `team_handoff.status` 变为 `selected`；新状态中 `expert.transition.source="team_handoff"`。 |

`expert_team_id` 是“选择专家团”的字段，不是某位成员专家的切换字段；普通 `chat` 不带 `expert_team_id`、`expert_id` 时始终保持通用对话 Runtime，不会自动选择专家团。

未绑定孩子聊天示例：

```json
{
  "session_id": "sess_001",
  "run_id": "run_unbound_001",
  "input": "{\"action\":\"chat\",\"context_scope\":\"unbound\",\"content\":\"我想先泛聊一下\",\"source\":\"chat\"}",
  "context_data": {"user_id": "user_001"}
}
```

`enable_thinking` 和 `return_reasoning` 可用于非停止动作，默认均为 `false`。SSE v2 当前不向业务前端暴露模型原始推理文本；前端不得依赖 `reasoning_delta`。

`open_session` 已预留但当前返回 `501 MODEL_OPENING_NOT_ENABLED`，接入方不要调用。

## 4. 请求示例

示例为了可读性将 `input` 先写成对象。BFF 发往算法服务前必须执行一次 `JSON.stringify(input)`。

### 4.1 普通聊天

```json
{
  "session_id": "sess_001",
  "run_id": "run_chat_001",
  "input": "{\"action\":\"chat\",\"context_scope\":\"profile\",\"context_activation\":\"auto\",\"content\":\"我想了解孩子适合什么方向\",\"source\":\"chat\",\"expert_context\":{\"operation\":\"continue\"},\"enable_thinking\":false,\"return_reasoning\":false}",
  "context_data": {
    "student_name": "小海",
    "user_id": "user_001",
    "profile_id": "profile_001"
  }
}
```

### 4.2 指定专家团或专家

在普通 `chat` 中显式选择专家团：

```json
{
  "action": "chat",
  "context_scope": "profile",
  "context_activation": "auto",
  "content": "帮我分析一下选科方向",
  "source": "chat",
  "expert_context": {
    "expert_team_id": "student_growth_expert_team",
    "expert_id": null,
    "operation": "select_team"
  }
}
```

指定专家时，若当前已进入专家团，专家必须属于该团队，否则返回
`EXPERT_NOT_IN_ACTIVE_TEAM`；未进入专家团时，会进入单专家模式：

```json
{
  "action": "chat",
  "context_scope": "profile",
  "context_activation": "auto",
  "content": "继续分析刚才的问题",
  "source": "toolbar",
  "expert_context": {
    "expert_team_id": "student_growth_expert_team",
    "expert_id": "academic_coach",
    "operation": "select_expert"
  }
}
```

### 4.3 手动 `@` 团队成员

前端显示可以是“@家庭教育专家”，但协议必须提交结构化 ID：

```json
{
  "action": "switch_team_member",
  "context_scope": "profile",
  "target_expert_id": "academic_coach",
  "content": "孩子最近不愿意和我沟通，怎么办？",
  "source": "toolbar",
  "expert_context": {
    "expert_team_id": "student_growth_expert_team",
    "expert_id": "e_career_planner",
    "operation": "continue"
  }
}
```

不能仅在普通 `chat.content` 中拼接 `@专家名称`；服务端不会从文本中解析专家身份。

### 4.4 确认专家转交卡

```json
{
  "action": "confirm_team_handoff",
  "context_scope": "profile",
  "source_message_id": "msg_001",
  "target_expert_id": "academic_coach",
  "source": "team_handoff",
  "expert_context": {
    "expert_team_id": "student_growth_expert_team",
    "expert_id": "e_career_planner",
    "operation": "continue"
  }
}
```

`source_message_id` 必须来自当前有效 `team_handoff` 卡片，目标专家必须在卡片候选和当前团队成员中。

### 4.5 专家转交卡未点击时的边界

专家转交卡是建议，不是强制切换。用户没有点击卡片时，前端只发送普通 `chat`，**绝不能**自行补
`source_message_id` 或改写当前专家。服务端根据当前孩子范围内的卡片和用户文本作如下处理：

| 用户后续行为 | 应发送的动作 | 服务端结果 |
| --- | --- | --- |
| 单候选卡后只发精确短确认，如“好的”“继续”“确认” | `chat + continue` | 服务端可把它识别为确认，自动转交给唯一候选；前端以新 `state.expert.active` 为准。 |
| 多候选卡后只发短确认 | `chat + continue` | 不猜测目标专家；卡片保持有效并随状态重发，用户仍须点击具体候选。 |
| 不点卡，而是提出新问题或带有犹豫的内容 | `chat + continue` | 当前专家继续回答；原未确认卡过期，之后不能再确认。 |
| 切换到孩子 B，不指定专家 | `chat + continue + context_activation:"auto"`，顶层 `context_data.profile_id=B` | 切入 B 并继承 session 当前专家选择；A 的卡片不作为 B 的操作依据。 |
| 切换到孩子 B，并选专家团 | `chat + select_team + context_activation:"auto"` | 切入 B 后由目标团队协调专家接待；不传 A 的 `source_message_id`。 |
| 切换到孩子 B，并指定团内专家 | `chat + select_expert + context_activation:"auto"` | 切入 B 后直接选择目标成员；不使用 `switch_team_member` 或 `confirm_team_handoff`。 |

`switch_team_member` 与 `confirm_team_handoff` 都要求请求所带的 `context_data.profile_id` 已是当前激活
孩子，不能顺便切孩子；跨孩子会返回 `409 CONTEXT_ACTIVATION_REQUIRED`。先用上表的 `chat` 切入 B，
收到 B 的权威 `state` 后，才能在 B 内点击工具栏或 B 自己的有效转交卡。

### 4.6 普通聊天模式下进入和退出 Skill

工具栏进入：

```json
{
  "action": "enter_skill",
  "context_scope": "profile",
  "profile_id": "profile_001",
  "target_skill_id": "interest_explore",
  "source": "toolbar"
}
```

推荐卡进入：

```json
{
  "action": "enter_skill",
  "context_scope": "profile",
  "profile_id": "profile_001",
  "target_skill_id": "interest_explore",
  "source": "route_suggestion",
  "source_message_id": "msg_001",
  "source_interaction_id": "route_suggestions"
}
```

退出：

```json
{
  "action": "quit_skill",
  "context_scope": "profile",
  "profile_id": "profile_001",
  "target_skill_id": "interest_explore",
  "source": "exit_button"
}
```

本节的 `enter_skill` / `quit_skill` 只适用于**通用对话模式**的工具栏或推荐卡操作。当前处于专家团模式时，直接 `enter_skill` 会返回 `409 SKILL_ENTRY_BLOCKED_IN_EXPERT_TEAM`；专家团内由当前专家在其锁定范围内自动选择、进入、切换或结束 Skill，前端/BFF 不发送这两个动作，也不展示普通模式的 Skill 进入/退出按钮。

当前专家若只锁定了一个 Skill，服务端会在每个普通对话轮次直接进入该 Skill，不再额外调用专家 Runtime 做同一层的 Skill 选择；这不会跳过 Skill 内的能力选择，RAG、MCP、Web Search、脚本、资料和表单仍由该 Skill 的运行契约与工具策略决定。多成员专家团的主协调专家例外：即使它只锁定一个 Skill，仍会保留专家团内的专家转交/协调判断。前端无需为这条优化增加请求字段，应始终以 SSE `state.context.active_skill` 为准展示当前实际执行的 Skill。

### 4.7 停止生成

停止动作不需要 `context_data`，并复用目标活动 run：

```json
{
  "session_id": "sess_001",
  "run_id": "run_chat_001",
  "input": "{\"action\":\"stop\",\"source\":\"composer\"}"
}
```

停止的是当前生成，不会删除会话。活动 run 不存在时返回 `409 RUN_NOT_ACTIVE`。

## 5. SSE 响应契约

### 5.1 Wire event

```text
event: state
data: {完整 SseV2State JSON}

event: ping
data: {}

event: done
data: {与最后一个 state 相同的完整 SseV2State JSON}

```

| event | seq | 处理方式 |
| --- | --- | --- |
| `state` | 严格递增 | 校验协议、会话和 run 后，用整帧替换当前状态。 |
| `ping` | 无 | 仅保活，忽略。 |
| `done` | 与最后 state 相同 | 标记传输完成，不重复渲染。随后连接关闭。 |

### 5.2 完整状态骨架

```json
{
  "protocol": "hailiang.sse.v2",
  "session_id": "sess_001",
  "run_id": "run_chat_001",
  "seq": 8,
  "ts": "2026-09-01T03:10:00+00:00",
  "elapsed_ms": 1240,
  "message_id": "msg_001",
  "profile_id": "profile_001",
  "profile_name": "小海",
  "branch_version": 1,
  "profile_context_status": "matched",
  "context_notice": {},
  "session_created": false,
  "profile_switched": false,
  "status": "completed",
  "assistant": {"content": "完整 Markdown 正文", "status": "completed"},
  "intent": {},
  "form": {},
  "path_options": {},
  "skill_rooms": [],
  "team_handoff": {},
  "expert": {"mode": "team", "team": {}, "active": {}, "activation": {}, "transition": {}},
  "skill_transition": {},
  "session": {"active_skill": {}},
  "risk": {"status": "passed", "stage": "output", "blocked": false, "message": ""},
  "error": {"code": "", "message": "", "upstream_detail": "", "retryable": false, "terminal": false}
}
```

所有顶层字段固定存在。空模块分别使用 `{}`、`[]`、空字符串或 `null`，前端不要通过“字段是否存在”推断状态。

### 5.3 顶层状态字段

| 字段 | 用途 |
| --- | --- |
| `protocol` | 必须等于 `hailiang.sse.v2`。 |
| `session_id` / `run_id` | 校验该帧属于当前会话和执行。 |
| `seq` | 同一 run 的状态版本，只应用更大值。 |
| `ts` / `elapsed_ms` | 调试时序；不是消息创建时间。 |
| `message_id` | 助手消息稳定 ID；完成前可能为 `null`。 |
| `profile_*` / `branch_version` | 本轮生效档案及分支状态。 |
| `status` | 本轮业务生命周期。 |
| `assistant.content` | 截至当前帧的完整 Markdown 正文。 |
| `intent` | 可展示的执行进度，不是模型原始思维链。 |
| `form` | 当前 Facts/问卷表单。 |
| `path_options` | 路径选择卡。 |
| `skill_rooms` | Skill 推荐卡。 |
| `team_handoff` | 专家团转交确认卡。 |
| `expert` | 当前专家团、专家、默认承接标志及最近切换状态。 |
| `skill_transition` | Skill 进入/退出转场。 |
| `session.active_skill` | 页面当前 Skill 的唯一权威来源。 |
| `risk` | 输入/输出安全状态和可展示提示。 |
| `error` | 流内运行错误。 |

### 5.4 生命周期

| status | 终态 | 前端行为 |
| --- | --- | --- |
| `streaming` | 否 | 继续接收快照，显示生成中和停止按钮。 |
| `completed` | 是 | 结束 loading，保留正文和有效交互。 |
| `stopped` | 是 | 保留已收到正文，标记“已停止”。 |
| `superseded` | 是 | 旧 run 被新动作替代，清空旧 run 未完成交互。 |
| `blocked` | 是 | 只展示 `risk.message`，不恢复旧正文或卡片。 |
| `failed` | 是 | 展示 `error.code + error.message`，按 `retryable` 决定是否提供重试入口。 |

## 6. 前端状态机

### 6.1 推荐数据结构

```ts
type ActiveRun = {
  runId: string;
  lastSeq: number;
  transportDone: boolean;
  state: SseV2State | null;
};
```

### 6.2 消费规则

```ts
function onSseEvent(event: string, data: unknown) {
  if (event === "ping") return;

  const next = data as SseV2State;
  if (next.protocol !== "hailiang.sse.v2") return;
  if (next.session_id !== currentSessionId) return;
  if (next.run_id !== activeRun.runId) return;

  if (event === "state") {
    if (next.seq <= activeRun.lastSeq) return;
    activeRun.lastSeq = next.seq;
    activeRun.state = next; // 整体替换，不追加 assistant.content
    render(next);
    return;
  }

  if (event === "done") {
    activeRun.transportDone = true;
    finishLoading(next.status);
  }
}
```

不要执行下面的增量拼接：

```ts
assistantText += state.assistant.content;
```

因为 `assistant.content` 已经是完整累计正文，正确行为是直接赋值。

### 6.3 页面状态与消息状态

- 当前气泡：来自当前 run 的 `assistant`、`intent`、`form`、`path_options`、`team_handoff`、`skill_rooms`。
- 页面级当前专家：只取 `expert.active`。
- 页面级当前 Skill：只取 `session.active_skill`；历史气泡不得覆盖它。
- `message_id=null` 时卡片可以展示，但需要消息 ID 的动作暂不可点击。
- 固定气泡顺序：`intent → assistant → form → path_options → team_handoff → skill_rooms`。

## 7. 结构化交互

### 7.1 表单

`form={}` 时不渲染。非空表单的关键字段：

```json
{
  "form_id": "missing_facts_form",
  "title": "补充关键信息",
  "description": "",
  "status": "active",
  "interaction_id": "fact_form:missing_facts_form",
  "fields": [{
    "fact_key": "grade",
    "label": "孩子年级",
    "input_type": "single_select",
    "required": true,
    "placeholder": "请选择",
    "example": "例如：高一",
    "options": [{"label": "高一", "value": "高一"}],
    "submit_mode": "manual",
    "scope": "profile",
    "value_type": "string"
  }]
}
```

支持 `text`、`single_select`、`multi_select`；未知 `input_type` 降级成文本框。仅 `status=active` 可编辑。

正式聊天当前的完整提交链路是：

1. 把答案写入相应 Facts 接口。
2. 调用 `PATCH /api/v1/sessions/{session_id}/messages/{message_id}/interactions/{interaction_id}`，将历史交互标记为 `submitted`。
3. 使用新的 `run_id`，通过 `/api/v2/sessions/chat/stream` 再发一条普通 `chat`，让当前 Skill 消费答案并继续。

### 7.2 路径卡

只允许点击 `path_options.status=active` 且 `options[].enabled=true` 的选项。点击后使用服务端返回的
`prompt` 发普通 `chat`，不要发明新的 `select_path` action。

### 7.3 Skill 推荐卡

只允许点击 `skill_rooms[].enabled=true` 的最新卡片。调用 `enter_skill` 时原样透传：

- `skill_id → target_skill_id`
- `source_message_id`
- `source_interaction_id`
- `source=route_suggestion`

### 7.4 专家转交卡

只允许点击 `team_handoff.status=active` 的候选，提交 `confirm_team_handoff`。不要在前端先修改当前专家；等新的
`state.expert.active` 到达后再更新。

## 8. BFF 流式转发要求

### 8.1 必须满足

- 使用 HTTP/1.1 或支持可靠流式响应的 HTTP/2 上游实现。
- 收到算法服务字节后立即 flush，不等待完整 JSON、完整事件或完整响应。
- 保留 `event:`、多行 `data:`、空行分隔符和 UTF-8 字节边界。
- 禁止代理缓冲、响应缓存和动态压缩；不要设置固定 `Content-Length`。
- 上游读取超时应覆盖最长模型运行时间，建议至少 180 秒；保活 `ping` 不能被代理吞掉。
- 浏览器断开或 Abort 时，取消上游 fetch/HTTP 请求。
- 将算法服务建流前的 `4xx/429/5xx` 状态和 `Retry-After` 转发给前端。
- 不记录完整 `context_data`、SSE `data` 或授权信息。

### 8.2 Nginx/网关检查项

```nginx
proxy_http_version 1.1;
proxy_buffering off;
proxy_cache off;
gzip off;
proxy_read_timeout 180s;
```

实际配置可因网关产品而异，但最终行为必须是“不缓存、不断流、不改帧”。

### 8.3 重试边界

- BFF 不自动重试 POST SSE。
- 明确收到建流前 HTTP 错误时，由前端根据状态码决定是否重新提交；重新提交业务动作使用新的 `run_id`。
- 如果连接是否已到达算法服务无法确认，即使尚未收到 `state`，也不要透明重放同一 `run_id`。
- 收到任意 `state` 后断线，调用 `/api/v1/sessions/{session_id}/context` 恢复历史；不要断点续传或重放旧 run。

## 9. 错误处理

### 9.1 建流前 HTTP 错误

建流前错误是普通 JSON 响应，不会发送 SSE：

```json
{
  "code": "REQUEST_VALIDATION_ERROR",
  "message": "请求字段校验失败。",
  "detail": [{"loc": ["body", "input"], "msg": "Field required"}]
}
```

前端应优先展示顶层 `message`；为兼容本地直连或中间层，应依次兼容 `detail.message`、字符串 `detail` 及 FastAPI 校验数组中的 `loc + msg`。若响应不是 JSON，展示截断后的文本与 HTTP 状态；同时读取 `X-Request-Id` 用于排查。无论是哪一种错误形态，都必须结束本轮 loading，不能把建流失败伪装成持续生成。

| HTTP | detail 示例 | 建议动作 |
| --- | --- | --- |
| 422 | `INVALID_INPUT_JSON`、字段校验错误 | 修正请求，不自动重试。 |
| 409 | `RUN_ID_CONFLICT` | 不复用该 run；检查是否重复提交。 |
| 409 | `RUN_NOT_ACTIVE` | 停止本地 loading，刷新会话。 |
| 409 | `SESSION_ID_CONFLICT` | 阻断并重新校验登录态和会话归属。 |
| 409 | `ACTIVE_RUN_MUST_STOP` | 先停止原 run，再切换档案。 |
| 409 | `SKILL_ENTRY_BLOCKED_IN_EXPERT_TEAM` | 保持专家模式，由专家 Runtime 选择 Skill。 |
| 429 | `LLM_RATE_LIMITED` / 并发容量不足 | 遵循 `Retry-After`，提示稍后再试。 |
| 501 | `MODEL_OPENING_NOT_ENABLED` | 不调用预留动作。 |

### 9.2 流内错误

HTTP 200 建流后，模型和 Runtime 错误写入完整状态的 `error`：

```json
{
  "status": "failed",
  "error": {
    "code": "MODEL_TIMEOUT",
    "message": "模型响应超时，请稍后重试。",
    "upstream_detail": "",
    "retryable": true,
    "terminal": true
  }
}
```

生产前端只显示 `code` 和 `message`；`upstream_detail` 仅供受控调试使用。`terminal=false` 表示辅助调用失败，正文仍可能完成。

## 10. 历史恢复

SSE 不提供 `Last-Event-ID` 断点续传。页面打开、刷新或流中断后：

1. `GET /api/v1/sessions/{session_id}` 恢复会话摘要和当前 Skill/专家状态。
2. `GET /api/v1/sessions/{session_id}/context` 恢复消息列表及每条消息的 `presentation`。
3. 历史和实时共用同一套 Markdown、表单、路径卡、专家转交卡和 Skill 卡渲染组件。
4. 历史交互通常已变为 `selected`、`submitted` 或 `expired`，不得重新启用。

## 11. 联调验收清单

### 转发后端

- [ ] 算法服务路径使用 `/api/v2/sessions/chat/stream`。
- [ ] `input` 只 stringify 一次，非停止动作包含 `profile_id`。
- [ ] `user_id/profile_id/session_id` 已做可信注入和归属校验。
- [ ] `Content-Type`、`Accept`、`X-SSE-Protocol` 正确。
- [ ] 首个 `state` 能在模型完成前到达浏览器。
- [ ] `ping` 不被缓存，`done` 后连接关闭。
- [ ] 浏览器 Abort 能取消算法上游。
- [ ] 没有代理级 POST 自动重试。
- [ ] HTTP 错误、`Retry-After` 和请求 ID 能透传。
- [ ] 日志不记录完整消息、Facts、SSE 正文和密钥。

### 前端

- [ ] 用户气泡立即出现，助手气泡用完整快照替换。
- [ ] 只处理当前 `run_id` 且更大 `seq`。
- [ ] `done` 不重复追加正文。
- [ ] 六种终态都能停止 loading。
- [ ] Markdown、表单、路径卡、专家转交卡、Skill 卡正常渲染。
- [ ] `message_id` 与 `run_id` 不混用。
- [ ] 专家切换使用结构化 action，不解析 `@` 文本。
- [ ] 停止生成复用活动 run。
- [ ] 刷新/断线通过历史接口恢复，不重放旧 run。
- [ ] 风控拦截时只显示 `risk.message`。

## 12. 配置版本切换

正式会话绑定当前专家团部署的 `deployment_id + package_hash`。激活、回退、恢复或下线不会中断正在生成的回答；已有会话在下一次非停止操作时切换。该次 SSE 的 `profile_context` 事件增加：

```json
{
  "configuration_changed": true,
  "configuration": {
    "code": "CONFIGURATION_UPDATED",
    "previous_deployment_id": "dep_old",
    "deployment_id": "dep_new",
    "package_hash": "sha256"
  }
}
```

切换保留历史消息、已确认 Facts 和已提交答案；未完成表单、候选路径、转交卡及旧执行状态失效。专家切换不会被未完成表单阻断：服务端先将表单设为 `expired`，清除待完成问卷与活动 Skill 状态，并写入 `form_abandoned` 事件；前端须将旧表单保留为只读并清除本地草稿。提交旧表单或确认旧转交卡返回 HTTP `409 CONFIGURATION_UPDATED`，前端必须结束 loading、刷新 `/api/v1/expert-teams` 与会话状态，并提示用户重新操作。

## 13. 实现权威来源

协议发生争议时，按以下顺序核对：

1. 请求模型与路由：[chat_stream.py](../src/hailiang_skills/api/routes/chat_stream.py)
2. 状态归并与固定字段：[sse_protocol.py](../src/hailiang_skills/core/sse_protocol.py)
3. 前端类型：[streamEvents.ts](../frontend/src/types/streamEvents.ts)
4. 前端流解析：[sse.ts](../frontend/src/utils/sse.ts)
5. 协议测试：[test_sse_protocol.py](../tests/test_sse_protocol.py)、[test_single_chat_stream.py](../tests/test_single_chat_stream.py)

任何新增 action、状态字段或事件，都应同时更新上述实现、测试和本指南，避免前端、BFF 与算法服务各自维护一套口头协议。
