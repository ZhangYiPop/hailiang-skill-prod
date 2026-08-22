# 算法服务 BFF 转发接口文档

> 基础地址示例：`http://10.30.6.45:8015`。以下路径均以 `/api/v1` 开头。

## 目录

1. [接入约定](#接入约定)
2. [接口总表](#接口总表)
3. [聊天、工具栏与停止生成](#聊天工具栏与停止生成)
4. [档案与会话](#档案与会话)
5. [Facts 与消息交互](#facts-与消息交互)
6. [Skill 埋点统计](#skill-埋点统计)
7. [统一错误规范](#统一错误规范)
8. [BFF 转发边界](#bff-转发边界)
9. [外部大模型测试接口](#外部大模型测试接口)

## 接入约定

- BFF 是鉴权与资源归属校验边界。`user_id` 由 BFF 从登录态注入，不能信任浏览器传入的值。
- 路径中的 `profile_id`、`session_id` 均须由 BFF 校验属于当前用户后再转发。
- JSON 接口使用 `Content-Type: application/json`；流接口额外使用 `Accept: text/event-stream` 与 `X-SSE-Protocol: hailiang.sse.v2`。
- 建议每次请求传入非空 `X-Request-Id`；服务会原样返回，方便关联日志。未传时服务端会生成。
- SSE 必须原样透传，禁止缓冲、合并事件或包装为普通 JSON。

## 接口总表

| 模块 | 方法 | 算法服务路径 | 业务前端可用 | 说明 |
| --- | --- | --- | --- | --- |
| Skill | GET | `/skills` | 是 | 工具栏 Skill 列表；当前不按学生年级或学段过滤。 |
| 聊天 | POST | `/sessions/chat/stream` | 是 | 唯一聊天、进出 Skill、停止生成入口。 |
| 档案 | GET / POST | `/users/{user_id}/profiles` | 是 | 列表、创建。 |
| 档案 | GET / PATCH | `/users/{user_id}/profiles/{profile_id}` | 是 | 查询、更新。 |
| 会话 | GET | `/users/{user_id}/profiles/{profile_id}/sessions` | 是 | 当前档案的历史会话。 |
| 会话 | GET / PATCH | `/sessions/{session_id}` | 是 | 会话摘要、修改标题。 |
| 会话 | GET | `/sessions/{session_id}/context` | 是 | 恢复消息与完整页面状态。 |
| 消息 | PATCH | `/sessions/{session_id}/messages/{message_id}/feedback` | 是 | 点赞或点踩。 |
| 消息 | PATCH | `/sessions/{session_id}/messages/{message_id}/interactions/{interaction_id}` | 是 | 提交 Fact 表单后的状态同步。 |
| Facts | GET | `/facts/form-config` | 是 | 动态 Fact 表单定义。 |
| Facts | GET / POST | `/users/{user_id}/facts` 及 `:batch-upsert`、`:reset`、`:clear-by-source` | 是 | 用户共享 Facts。 |
| Facts | GET / POST | `/users/{user_id}/profiles/{profile_id}/facts` 及 `:batch-upsert`、`:reset` | 是 | 档案 Facts。 |
| Facts | GET / POST | `/sessions/{session_id}/facts` 及 `:batch-upsert`、`:reset` | 是 | 会话 Facts。 |
| 运营统计 | GET | `/analytics/skills` | 仅 BFF/运营 | 按时间范围和 Skill 查询点击率、对话推荐进入率。 |
| 健康 | GET | `/health`、`/health/live`、`/health/ready` | 仅 BFF/监控 | 健康检查。 |

不应转发给业务前端：`/sessions/{session_id}/events`、`/sessions/{session_id}/logs/download`、`/security-quarantine/**`、`/assets/versions`。旧的非流式 `POST /sessions/{session_id}/messages` 仅兼容存量调用；新聊天一律使用流接口。

## 外部大模型测试接口

### 接口信息

`POST /api/v1/external/chat` 面向外部测评和联调调用。服务端每次请求自动创建全新用户、档案、会话和运行 ID；调用方只需要传入 API Key 与自行拼接的完整 `dialogue`，不需要管理已有会话。

请求头：

```http
Content-Type: application/json
Authorization: Bearer ${api_key}
```

请求体：

```json
{
  "model": "default",
  "max_tokens": 1024,
  "stream": false,
  "dialogue": [
    {"role": "user", "content": "请介绍一下北京。"},
    {"role": "model", "content": "北京是中国的首都。"},
    {"role": "user", "content": "请再介绍当地美食。"}
  ]
}
```

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `model` | 字符串 | 否 | `default` | 模型标识；未传时使用服务默认模型。 |
| `max_tokens` | 整数 | 否 | `1024` | 输出 token 上限，范围 `1-32768`。 |
| `stream` | 布尔 | 否 | `false` | `false` 返回 JSON；`true` 返回 SSE。 |
| `dialogue` | 数组 | 是 | 无 | 至少一条消息，最后一条必须是 `user`。 |
| `dialogue[].role` | `user/model/assistant` | 是 | 无 | `model` 在内部转换为 `assistant`。 |
| `dialogue[].content` | 非空字符串 | 是 | 无 | 消息内容。 |

完整 `dialogue` 会作为本次新会话的上下文，最后一条 `user` 消息作为当前问题。历史会话不会被复用。

非流式成功响应（HTTP 200）：

```json
{
  "content": "模型回答内容",
  "choices": [],
  "status": "success",
  "reason": "success",
  "session_id": "sess_external_xxx",
  "request_id": "req_xxx"
}
```

业务执行失败仍返回 HTTP 200：`status` 为 `failed`，`content` 为空，`reason` 返回失败原因。API Key 错误、请求校验错误和限流等建连前错误使用 HTTP 4xx/429。

流式调用：

```bash
curl -N "$ALGORITHM_BASE/api/v1/external/chat" \
  -H 'Authorization: Bearer ${HAILIANG_EXTERNAL_API_KEY}' \
  -H 'Content-Type: application/json' \
  --data-raw '{"stream":true,"dialogue":[{"role":"user","content":"请介绍北京。"}]}'
```

流式每个 `data` 事件包含 `choices[].delta` 增量文本，最后一个事件包含 `finish_reason: "stop"`。内部 SSE 状态、Facts、Skill 状态和调试字段不会向外部暴露。

## Skill 埋点统计

### 查询接口

`GET /api/v1/analytics/skills` 用于运营侧查询 Skill 漏斗数据。建议由 BFF 或运营后台调用，不面向普通业务前端直接透出。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `start_time` | ISO-8601 时间字符串 | 否 | 统计开始时间，包含该时刻；未传时取已有有效埋点中的最早时间。 |
| `end_time` | ISO-8601 时间字符串 | 否 | 统计结束时间，包含该时刻；未传时取服务端处理请求时刻。 |
| `skill_id` | 字符串 | 否 | 指定单个 Skill；未传时返回所有 Skill 明细和全量汇总。未知 ID 返回 `422 UNKNOWN_SKILL_ID`。 |

时间必须是带时区的 ISO-8601 格式。中国时间推荐写作 `2026-08-04T10:30:00+08:00`；UTC 可写作 `2026-08-04T02:30:00Z`。这两个示例表示同一时刻。服务端按实际时间点比较，不受调用方机器时区影响。

```bash
# 查询全部 Skill：从最早有效埋点到当前时间
curl -s "$ALGORITHM_BASE/api/v1/analytics/skills" \
  -H 'Accept: application/json' \
  -H 'X-Request-Id: req-skill-analytics-0001' | python -m json.tool

# 查询单个 Skill 的当天数据（中国时区）
curl -sG "$ALGORITHM_BASE/api/v1/analytics/skills" \
  -H 'Accept: application/json' \
  -H 'X-Request-Id: req-skill-analytics-0002' \
  --data-urlencode 'skill_id=subject_advisor' \
  --data-urlencode 'start_time=2026-08-04T00:00:00+08:00' \
  --data-urlencode 'end_time=2026-08-04T23:59:59+08:00' | python -m json.tool

# 等价的 UTC 查询
curl -sG "$ALGORITHM_BASE/api/v1/analytics/skills" \
  --data-urlencode 'skill_id=subject_advisor' \
  --data-urlencode 'start_time=2026-08-03T16:00:00Z' \
  --data-urlencode 'end_time=2026-08-04T15:59:59Z' | python -m json.tool
```

响应中的每个 `skills[]` 项和 `aggregate` 都包含：

- `skill_click_through_rate`：`activated / (toolbar_clicks + route_suggestion_items)`；同一回复推荐多个不同 Skill 时，每个 Skill 分别计一次推荐。
- `chat_recommendation_entry_rate`：`route_suggestion_activated / route_suggestion_turns`；仅计对话推荐卡片，不含 Toolbar。对于同一个 Skill，同一回复无论重复出现几次都只算一轮曝光。
- `rate`：分母为 0 时为 `null`，否则为 0 到 1 的小数。
- `historical_derived`：`true` 表示本次结果含由历史会话事件推导的数据；历史时期无法还原“点击 Toolbar 但未成功进入”的次数。

响应示例：

```json
{
  "start_time": "2026-08-04T00:00:00+00:00",
  "end_time": "2026-08-04T16:00:00+00:00",
  "skill_id": "subject_advisor",
  "historical_derived": false,
  "skills": [
    {
      "skill_id": "subject_advisor",
      "counts": {
        "toolbar_clicks": 12,
        "route_suggestion_items": 18,
        "route_suggestion_turns": 18,
        "activated": 15,
        "route_suggestion_activated": 9
      },
      "skill_click_through_rate": {"numerator": 15, "denominator": 30, "rate": 0.5},
      "chat_recommendation_entry_rate": {"numerator": 9, "denominator": 18, "rate": 0.5}
    }
  ],
  "aggregate": {"skill_id": "all"}
}
```

## 聊天、工具栏与停止生成

### 1. 获取工具栏 Skill

```bash
curl "$ALGORITHM_BASE/api/v1/skills" \
  -H 'Accept: application/json' \
  -H 'X-Request-Id: req-skills-0001'
```

无请求体。响应为 `{ "skills": [...] }`；前端用每项的 `skill_id`、`label`、`brief`、`info` 等字段渲染工具栏，
不应硬编码列表。当前实现**不使用** `grade` 参数，也不按 `routing.school_stage_scope` 过滤工具栏；
工具栏返回完整可进入 Skill 目录。当前已激活的 Skill 不应显示为可进入按钮。

新会话默认是隐式 `general_chat`，因此它不会出现在工具栏目录中。原
`main_planner` 的公开 ID 已改为 `career_plan_entity`，该 Skill 现在会像其他专项 Skill
一样出现在目录中；它仍负责画像判断、回答和引导其他专项 Skill。旧客户端传入
`main_planner` 时服务端会按兼容别名解析为 `career_plan_entity`，但新客户端必须使用新 ID。

### 2. 聊天流入口

```bash
curl -N "$ALGORITHM_BASE/api/v1/sessions/chat/stream" \
  -H 'Accept: text/event-stream' \
  -H 'Content-Type: application/json' \
  -H 'X-SSE-Protocol: hailiang.sse.v2' \
  -H 'X-Request-Id: req-chat-0001' \
  --data-raw '{
    "session_id":"session-d7c62cd1-cc1b-457f-9349-fb926a6f48f",
    "run_id":"run-1ebfd8d7-76eb-4b0a-9ff5-f5e1f56e388",
    "input":"{\"action\":\"chat\",\"content\":\"给孩子做一份生涯规划\",\"source\":\"chat\",\"enable_thinking\":false,\"return_reasoning\":false}",
    "context_data":{
      "student_name":"zz",
      "user_id":"test-0723-1",
      "profile_id":"pro-0723-1",
      "school_year":"2026",
      "grade":"高一"
    }
  }'
```

`-N` 会关闭 curl 缓冲。浏览器的 `Origin`、`Referer`、`sec-*` 请求头不是 BFF 调用的必需项。

#### 顶层请求字段

| 字段 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- |
| `session_id` | 非空字符串 | 是 | 首次请求可由 BFF 生成，后续复用。不能更换既有会话的用户或档案。 |
| `run_id` | 非空字符串 | 是 | 单次执行 ID。普通动作必须全局唯一且不能重用；停止时例外，必须复用活动 run ID。 |
| `input` | JSON 字符串 | 是 | 字符串内容必须是合法的 JSON 对象，见下表。 |
| `context_data` | JSON 对象 | 是 | 会话身份与首次建档信息，见下表。 |

顶层未知字段、类型不正确或缺少字段均返回 `422 REQUEST_VALIDATION_ERROR`。

#### `run_id`、`message_id`、`source_message_id` 的区别

这些字段在前端联调时最容易混淆，语义如下：

| 字段 | 出现位置 | 语义 | 能否复用 |
| --- | --- | --- | --- |
| `run_id` | 顶层请求、SSE 顶层 | 本次 `/sessions/chat/stream` 执行的唯一运行 ID。一次 `chat`、一次 `enter_skill`、一次 `quit_skill` 都各自对应一个新的 run。 | 普通动作不能复用；`stop` 必须复用被停止的活动 run。 |
| `message_id` | SSE `state/done`、`GET /sessions/{session_id}/context` 的 `messages[]` | 某一条后端消息的稳定主键，形如 `msg_xxx`。后续消息反馈、表单提交状态同步、推荐卡片来源绑定都使用它。 | 可长期保存，用于历史恢复和后续交互。 |
| `source_message_id` | `input.action="enter_skill"` 且 `source="route_suggestion"` | 本次点击的推荐卡片所属 assistant 消息的 `message_id`。不是 `run_id`，也不是前端本地消息数组索引。 | 只能引用当前最新、且推荐仍为 `active` 的那条 assistant 消息。 |
| `source_interaction_id` | `input.action="enter_skill"` 且 `source="route_suggestion"` | 当前推荐交互的 ID，现固定为 `route_suggestions`。 | 推荐跳转时按服务端返回原样透传。 |

- `run_id` 用于流控制、停止、去重。
- `message_id` 用于消息级交互。
- `source_message_id` 只能取自真实 assistant 消息的 `message_id`；不能把 BFF 自己的“上一轮请求 ID”或“上一轮 run ID”塞进来。

#### `context_data`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `student_name` | 非空字符串 | 是 | 新建档案时使用的孩子名称；恢复会话不覆盖已有名称。 |
| `user_id` | 非空字符串 | 是 | BFF 从登录态注入的稳定用户 ID。 |
| `profile_id` | 非空字符串 | 是 | 孩子档案 ID；与 `user_id` 一起决定归属。 |
| `school_year` | 非空字符串 | 否 | 如 `"2026"`、`"2026-2027"`。 |
| `grade` | 非空字符串 | 否 | 如 `"高一"`。 |
| `facts` | 对象 | 否 | 可扩展 Facts 信封；键必须在服务端 `facts_schema.yml` 注册并启用，例如 `{ "student_province": "浙江", "score_total": 580 }`。 |
| 其他字段 | 任意合法 JSON 值 | 否 | 允许业务扩展；接口接受但不会自动写入 Facts。 |

仅前三项必填。首次创建时，服务会在传入 `grade` 后立即初始化档案年级 Fact；若同时传入 `school_year`，还会写入完整的学年—年级记录。

首次创建时，`facts` 中已注册的字段会依据 Facts Schema 校验、归一化并按其 scope 保存；为兼容旧转发端，同样已注册的字段也可以直接放在 `context_data` 顶层。未注册字段只作为转发元数据保留，不会进入模型上下文。

#### `input` 中的对象

| `action` | 必填字段 | `source` | 说明 |
| --- | --- | --- | --- |
| `chat` | `content`：非空字符串 | `chat` | 普通聊天。 |
| `switch_team_member` | `target_expert_id`、`content`：非空字符串 | `toolbar` | 从当前专家团工具栏指定专家并携带问题；禁止从 `content` 解析专家名称。 |
| `confirm_team_handoff` | `source_message_id`、`target_expert_id` | `team_handoff` | 确认主协调专家消息中的有效候选卡片。 |
| `enter_skill` | `target_skill_id`：非空字符串 | `toolbar`、`route_suggestion` | 推荐跳转时额外需要 `source_message_id`、`source_interaction_id`。 |
| `quit_skill` | `target_skill_id`：非空字符串 | `toolbar`、`exit_button` | `target_skill_id` 必须等于当前活动 Skill。 |
| `stop` | 无 | `composer` | 取消当前 run。 |

`enable_thinking`、`return_reasoning` 均为可选布尔值，默认 `false`。未知字段、错误类型或枚举不匹配返回 `422 REQUEST_VALIDATION_ERROR`。

专家切换必须使用结构化 `target_expert_id`。`chat.content` 中即使出现 `@专家名称` 也只是普通对话文本，不触发路由；`context_data.expert_id` 也不能替代本轮结构化切换动作。

#### 专家团工具栏指定专家并携带问题

```bash
curl -N "$ALGORITHM_BASE/api/v1/sessions/chat/stream" \
  -H 'Accept: text/event-stream' -H 'Content-Type: application/json' \
  -H 'X-SSE-Protocol: hailiang.sse.v2' \
  --data-raw '{
    "session_id":"session-1","run_id":"run-expert-toolbar-001",
    "input":"{\"action\":\"switch_team_member\",\"source\":\"toolbar\",\"target_expert_id\":\"family_education_expert\",\"content\":\"孩子最近不愿意和我沟通，怎么办？\"}",
    "context_data":{"student_name":"zz","user_id":"test-0723-1","profile_id":"pro-0723-1"}
  }'
```

服务端只允许切换到当前专家团成员，并自行根据 `target_expert_id` 生成可见的 `@专家名称 + content` 历史记录。前端不得把专家名称拼进 `content` 作为路由依据。

#### 确认主协调专家推荐卡

```json
{
  "action": "confirm_team_handoff",
  "source": "team_handoff",
  "source_message_id": "msg_xxx",
  "target_expert_id": "family_education_expert"
}
```

两种来源的校验规则不同：`team_handoff` 只能选择来源卡片中的有效候选；`toolbar` 可以选择当前专家团任一成员，且不携带 `source_message_id`。

### 3. 工具栏进入与退出 Skill

点击工具栏后使用新的、未使用过的 `run_id`；`target_skill_id` 必须取自 `GET /skills` 的返回值。

```bash
curl -N "$ALGORITHM_BASE/api/v1/sessions/chat/stream" \
  -H 'Accept: text/event-stream' -H 'Content-Type: application/json' \
  -H 'X-SSE-Protocol: hailiang.sse.v2' \
  --data-raw '{
    "session_id":"session-1","run_id":"run-enter-001",
    "input":"{\"action\":\"enter_skill\",\"target_skill_id\":\"subject_advisor\",\"source\":\"toolbar\"}",
    "context_data":{"student_name":"zz","user_id":"test-0723-1","profile_id":"pro-0723-1"}
  }'
```

助手回复中的推荐跳转应将 `source` 改为 `route_suggestion`，并附带该回复的 `source_message_id` 与 `source_interaction_id`。退出时调用同一接口，传入 `action="quit_skill"`、当前 `target_skill_id` 和来源 `toolbar` 或 `exit_button`。

#### tool bar、推荐卡片、退出按钮三种动作的差异

| 场景 | `action` | `source` | 必填补充字段 | 说明 |
| --- | --- | --- | --- | --- |
| 工具栏进入 Skill | `enter_skill` | `toolbar` | 无 | 直接进入目标 Skill，使用 `facts_only` 上下文模式。 |
| assistant 推荐卡片 / skill room 进入 Skill | `enter_skill` | `route_suggestion` | `source_message_id`、`source_interaction_id` | 基于当前最新 assistant 消息做带上下文 handoff。 |
| 退出当前 Skill | `quit_skill` | `toolbar` 或 `exit_button` | `target_skill_id` 必须等于当前 `active_skill` | 切回 `general_chat`。 |

推荐卡片点击时，必须使用服务端返回在 `skill_rooms[].source_message_id` 和 `skill_rooms[].source_interaction_id` 里的值，不得自行拼接。

#### 推荐卡片进入 Skill 的示例

```bash
curl -N "$ALGORITHM_BASE/api/v1/sessions/chat/stream" \
  -H 'Accept: text/event-stream' -H 'Content-Type: application/json' \
  -H 'X-SSE-Protocol: hailiang.sse.v2' \
  --data-raw '{
    "session_id":"session-1","run_id":"run-enter-002",
    "input":"{\"action\":\"enter_skill\",\"target_skill_id\":\"interest_explore\",\"source\":\"route_suggestion\",\"source_message_id\":\"msg_001\",\"source_interaction_id\":\"route_suggestions\"}",
    "context_data":{"student_name":"zz","user_id":"test-0723-1","profile_id":"pro-0723-1"}
  }'
```

若 `source_message_id` 指向的不是当前最新 assistant 消息，或该消息上的推荐已不是 `active`，服务返回 `409`，常见 `detail` 为：

- `route suggestion is no longer current`
- `route suggestion is no longer active`

#### 退出 Skill 的示例

```bash
curl -N "$ALGORITHM_BASE/api/v1/sessions/chat/stream" \
  -H 'Accept: text/event-stream' -H 'Content-Type: application/json' \
  -H 'X-SSE-Protocol: hailiang.sse.v2' \
  --data-raw '{
    "session_id":"session-1","run_id":"run-exit-001",
    "input":"{\"action\":\"quit_skill\",\"target_skill_id\":\"interest_explore\",\"source\":\"exit_button\"}",
    "context_data":{"student_name":"zz","user_id":"test-0723-1","profile_id":"pro-0723-1"}
  }'
```

退出成功后，SSE 终态会把 `skill_transition.action` 设为 `exit`，并把 `session.active_skill.skill_id` 切回 `general_chat`。详见 [SSE_RESPONSE_CONTRACT.md](SSE_RESPONSE_CONTRACT.md)。

### 4. 取消当前生成

取消的是当前执行中的 **run**，不是删除会话。调用同一流接口，且必须复用要停止的活动 `run_id`：

```bash
curl -N "$ALGORITHM_BASE/api/v1/sessions/chat/stream" \
  -H 'Accept: text/event-stream' -H 'Content-Type: application/json' \
  -H 'X-SSE-Protocol: hailiang.sse.v2' \
  --data-raw '{
    "session_id":"session-1","run_id":"run-1ebfd8d7-76eb-4b0a-9ff5-f5e1f56e388",
    "input":"{\"action\":\"stop\",\"source\":\"composer\"}",
    "context_data":{"student_name":"zz","user_id":"test-0723-1","profile_id":"pro-0723-1"}
  }'
```

run 已结束、被替代或不属于会话时，返回 `409 RUN_NOT_ACTIVE`。停止保留已输出正文，并以 `stopped` 状态结束。当前没有 `DELETE /sessions/{session_id}`；如需删除会话，需另行定义数据保留和审计规则。

### 5. SSE 响应

成功建连返回 `200`、`Content-Type: text/event-stream; charset=utf-8` 和 `X-SSE-Protocol: hailiang.sse.v2`。
完整的状态字段、渲染时机、表单和错误语义以 [SSE_RESPONSE_CONTRACT.md](SSE_RESPONSE_CONTRACT.md)
为唯一权威；本节只说明接口层事件。业务过程帧均为 `event: state`，数据是完整状态快照。所有状态帧发送完成后，服务必定追加一帧 `event: done`，随后关闭连接：

```text
event: done
data: {"protocol":"hailiang.sse.v2","session_id":"session-1","run_id":"run-1","seq":12,"status":"completed", ...}
```

`done.data` 与最后一个 `state.data` 是同一份完整状态快照。转发后端应以 `done` 作为本次 SSE
已经完整发送的终止信号，并将 `done.data` 直接用于保存历史会话；其中 `status` 是该 run 的最终
业务状态（例如 `completed`、`stopped` 或 `failed`）。

建连后模型或运行时错误出现在 `data.error`，而不是 HTTP 错误：

```json
{"status":"failed","error":{"code":"MODEL_TIMEOUT","message":"模型响应超时，请稍后重试。","upstream_detail":"Timeout","retryable":true,"terminal":true}}
```

流内错误码：`MODEL_UNAVAILABLE`、`MODEL_TIMEOUT`、`MODEL_AUTHENTICATION_FAILED`、`MODEL_RATE_LIMITED`、`MODEL_INVALID_RESPONSE`、`MODEL_STREAM_INTERRUPTED`、`MODEL_UPSTREAM_ERROR`、`MODEL_RUNTIME_ERROR`。详见 [SSE_RESPONSE_CONTRACT.md](SSE_RESPONSE_CONTRACT.md#5-风控与错误) 与 [SSE_ERROR_CODES.md](SSE_ERROR_CODES.md)。

## 档案与会话

### 档案接口

```bash
# 档案列表
curl "$ALGORITHM_BASE/api/v1/users/$USER_ID/profiles"

# 创建：name 必填字符串；initialize_from_shared_facts 可选布尔值，默认 true
curl -X POST "$ALGORITHM_BASE/api/v1/users/$USER_ID/profiles" \
  -H 'Content-Type: application/json' \
  --data '{"name":"小明","initialize_from_shared_facts":true}'

# 查询与修改：name、is_default 均可选
curl "$ALGORITHM_BASE/api/v1/users/$USER_ID/profiles/$PROFILE_ID"
curl -X PATCH "$ALGORITHM_BASE/api/v1/users/$USER_ID/profiles/$PROFILE_ID" \
  -H 'Content-Type: application/json' \
  --data '{"name":"小明","is_default":true}'
```

创建响应的 `profile.profile_id` 是后续调用的档案 ID。不存在的档案返回 `404 PROFILE_NOT_FOUND`。

### 会话接口

```bash
# 当前孩子的历史会话
curl "$ALGORITHM_BASE/api/v1/users/$USER_ID/profiles/$PROFILE_ID/sessions"

# 会话摘要与完整恢复
curl "$ALGORITHM_BASE/api/v1/sessions/$SESSION_ID"
curl "$ALGORITHM_BASE/api/v1/sessions/$SESSION_ID/context"

# 修改标题：title 必填非空字符串
curl -X PATCH "$ALGORITHM_BASE/api/v1/sessions/$SESSION_ID" \
  -H 'Content-Type: application/json' \
  --data '{"title":"2026 生涯规划"}'
```

不存在的会话返回 `404 SESSION_NOT_FOUND`；档案会话列表的档案不属于当前用户时返回 `403 PROFILE_ACCESS_DENIED`。

#### 历史恢复时三类接口各自负责什么

前端恢复一个历史会话时，建议并行读取：

- `GET /sessions/{session_id}`：会话级状态，包含 `skill_display`、`candidate_paths`、`skill_states`
- `GET /sessions/{session_id}/context`：完整历史消息和 `messages[].presentation`
- `GET /sessions/{session_id}/events`：调试/事件时间线，仅调试端使用，不应转发给业务前端

典型恢复策略：

1. 用 `/sessions/{session_id}` 决定当前页面顶部的 `active_skill`、标题和候选路径等会话级状态。
2. 用 `/sessions/{session_id}/context` 恢复消息列表；每条 `assistant` 消息都可能已经带好 `presentation`，前端应直接按该结构渲染，而不是再根据旧 `blocks` 或旧事件重算。
3. 恢复历史时不要重放旧 SSE `run_id`；`run_id` 只属于当时那一轮执行。

## Facts 与消息交互

### 消息反馈与表单状态

```bash
# feedback 只能是 "like"、"dislike" 或 null
curl -X PATCH "$ALGORITHM_BASE/api/v1/sessions/$SESSION_ID/messages/$MESSAGE_ID/feedback" \
  -H 'Content-Type: application/json' --data '{"feedback":"like"}'

# status 固定为 submitted；submitted_fact_keys 是字符串数组
curl -X PATCH "$ALGORITHM_BASE/api/v1/sessions/$SESSION_ID/messages/$MESSAGE_ID/interactions/fact_form:$FORM_ID" \
  -H 'Content-Type: application/json' \
  --data '{"status":"submitted","submitted_fact_keys":["grade","student_province"]}'
```

`interaction_id` 必须是 `fact_form:{form_id}`。错误码依次为：消息不存在 `404 ASSISTANT_MESSAGE_NOT_FOUND`、交互不存在 `404 MESSAGE_INTERACTION_NOT_FOUND`、交互失效 `409 INTERACTION_INACTIVE`、交互类型不对 `422 INVALID_INTERACTION_TYPE`、提交字段不匹配 `422 INVALID_SUBMITTED_FACT_KEYS`。

#### Native Questionnaire 表单的完整提交链路

当前 Native Questionnaire 存在两层动作，不能混为一谈：

1. `PATCH /sessions/{session_id}/messages/{message_id}/interactions/{interaction_id}`
   - 作用：把表单交互状态同步为 `submitted`
   - 主要服务于 UI 状态和历史回放
2. `POST /sessions/chat/stream`
   - 作用：把用户答案继续发给当前 Skill，让后端消费并推进流程

对 `multi_path_planning` 这类 `skill_session` 问卷，前端常见续发内容是把答案拼成一条普通 `chat` 文本，例如：

```json
{"action":"chat","content":"高考省份：浙江；选科科目：物理、化学、生物；预估高考总分：620","source":"chat"}
```

如果前端暂时无法发送交互状态 PATCH，后端通常仍可从上述文本中解析答案并继续执行；但历史消息中的表单不会自动变成 `submitted`，因此 UI 状态会不完整。

### Facts 读取

```bash
curl "$ALGORITHM_BASE/api/v1/facts/form-config"
curl "$ALGORITHM_BASE/api/v1/users/$USER_ID/facts"
curl "$ALGORITHM_BASE/api/v1/users/$USER_ID/profiles/$PROFILE_ID/facts"
curl "$ALGORITHM_BASE/api/v1/sessions/$SESSION_ID/facts"
```

`/facts/form-config` 返回动态表单 `{ "fields": [...] }`；前端根据其 `scope`、选项和字段类型渲染，不能写死字段。

### Facts 写入与重置

三层写入或重置的请求体相同，路径决定实际作用域。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `scope` | 字符串 | 否 | 建议传 `shared`、`profile`、`session`；最终以 URL 层级为准。 |
| `source.type` | 字符串 | 否 | 默认 `user_form`。 |
| `source.source_id`、`source.source_label`、`source.turn_id` | 字符串或 `null` | 否 | 来源和追踪信息。 |
| `updates` | 数组 | 写入时否 | 每项是 `{ "key": 字符串, "value": 任意 JSON 值或 null }`。 |
| `fact_keys` | 字符串数组 | 重置时否 | 需要删除的 Fact key。 |

```bash
# 写入共享、档案、会话 Facts：仅替换 URL 和 scope
curl -X POST "$ALGORITHM_BASE/api/v1/users/$USER_ID/facts:batch-upsert" \
  -H 'Content-Type: application/json' \
  --data '{"scope":"shared","source":{"type":"user_form","source_id":"family-form"},"updates":[{"key":"parent_expectation","value":"稳妥升学"}]}'

curl -X POST "$ALGORITHM_BASE/api/v1/users/$USER_ID/profiles/$PROFILE_ID/facts:batch-upsert" \
  -H 'Content-Type: application/json' \
  --data '{"scope":"profile","updates":[{"key":"grade","value":"高一"}]}'

curl -X POST "$ALGORITHM_BASE/api/v1/sessions/$SESSION_ID/facts:reset" \
  -H 'Content-Type: application/json' \
  --data '{"scope":"session","source":{"type":"manual_reset"},"fact_keys":["current_goal"]}'

# 只有共享 Facts 支持按来源清理
curl -X POST "$ALGORITHM_BASE/api/v1/users/$USER_ID/facts:clear-by-source" \
  -H 'Content-Type: application/json' \
  --data '{"source":{"type":"user_form","source_id":"family-form"}}'
```

写入与重置响应返回 `fact_changes`、`current_facts`，可用于刷新页面。档案/会话不存在时分别返回 `404 PROFILE_NOT_FOUND`、`404 SESSION_NOT_FOUND`。

## 统一错误规范

### HTTP 错误（SSE 建连前及普通 JSON 接口）

所有 `4xx/429` 使用以下结构；`detail` 保留 FastAPI 的原始错误信息，BFF 应以 `code` 做程序判断。

```json
{"code":"RUN_ID_CONFLICT","message":"run_id 已使用，不能重复提交。","detail":"RUN_ID_CONFLICT"}
```

| HTTP 状态 | `code` | 典型触发条件 | 是否重试 |
| --- | --- | --- | --- |
| 422 | `REQUEST_VALIDATION_ERROR` | 请求字段缺失、空字符串、类型、枚举或未知字段不正确。 | 否 |
| 422 | `INVALID_INPUT_JSON` | `input` 不是 JSON 对象字符串。 | 否 |
| 422 | `UNSUPPORTED_ACTION` | 不支持的 `input.action`。 | 否 |
| 422 | `INVALID_ROUTE_SUGGESTION` | 推荐跳转缺少来源消息或交互 ID。 | 否 |
| 409 | `SESSION_ID_CONFLICT` | 会话归属的用户或档案不一致。 | 否 |
| 409 | `PROFILE_ID_CONFLICT` | 档案 ID 被其他用户占用。 | 否 |
| 409 | `RUN_ID_CONFLICT` | 普通动作重复使用 run ID。 | 否 |
| 409 | `RUN_NOT_ACTIVE` | 停止的 run 不是当前活动 run。 | 读取状态后决定 |
| 409 | `QUIT_SKILL_TARGET_MISMATCH` | 退出目标不是当前活动 Skill。 | 刷新状态后 |
| 409 | `TARGET_SKILL_ALREADY_ACTIVE` | `enter_skill` 的目标已经是当前活动 Skill。 | 不重试；保持当前 Skill。 |
| 409 | `SESSION_UPDATE_CONFLICT` | 并发更新冲突。 | 短暂退避后 |
| 404 | `SESSION_NOT_FOUND`、`PROFILE_NOT_FOUND` | 资源不存在。 | 否 |
| 429 | `SSE_CAPACITY_EXCEEDED` | 流式并发已满。 | 是，按 `Retry-After`（当前 5 秒） |
| 500 | `HTTP_ERROR` | 未处理服务端异常。 | 有限重试并记录请求 ID |

## BFF 转发边界

- 业务前端只访问 BFF；算法服务仅对 BFF 所在内网开放。
- BFF 负责认证、注入 `user_id`、校验档案/会话归属、生成 `X-Request-Id`。
- 透传算法服务的 HTTP 状态、错误 JSON、`Retry-After`、`X-Request-Id` 和 SSE 数据。
- 客户端断开 SSE 时，BFF 应取消上游请求；SSE 代理禁用响应缓冲与压缩。
