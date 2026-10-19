# SSE v3：Harness 多 Agent 公开协议严格契约

> **当前对外消费模型（2026-10-19）**：V3 已改为 V2 风格的完整状态帧。接入方只消费 `state`、`suspended`、`timeout`、`done` 与 `ping`；除 `ping` 外每帧 `data` 都是完整公开状态骨架，前端整体替换，不消费 `node.updated`、`message.delta` 或 `run.finalized`。`done` 是唯一可落库终态信号；`suspended` 与 `timeout` 携带 `resume_after`，用于续连同一个 run。本文后续若与本说明冲突，以本说明为准，详细章节将在同一协议版本内继续收敛。
> **回答恢复**：`POST /input-requests/{request_id}/answers` 带 `Accept: text/event-stream` 时，服务端先原子保存答案和恢复作业，再将该 HTTP 响应作为同一 run 的有限 SSE 流返回。BFF 直接透传，前端无需在回答成功后手工请求 run 事件。

> 协议：`hailiang.sse.v3`
>
> 接口前缀：`/api/v3/sessions/{session_id}`
>
> 适用对象：业务前端、BFF、联调与自动化测试
>
> 本文定义请求、公开实体、事件、状态和恢复语义。若示例与本文冲突，以本文和
> [`sse_v3/event.schema.json`](sse_v3/event.schema.json) 为准。

## 1. 与 SSE v2 的边界

SSE v2 和 v3 都使用 SSE 传输 JSON，但状态模型不同：

| 项目 | SSE v2 | Harness SSE v3 |
| --- | --- | --- |
| 入口 | `/api/v2/sessions/chat/stream` | `/api/v3/sessions/{id}/harness/*` |
| 更新方式 | 每帧 `state` 是完整回复快照 | 首次快照，之后按实体完整 upsert |
| 正文 | 包含在完整 `state` 中 | `message.delta` 增量 + `message.updated` 校准 |
| 多 Agent | 当前承接专家状态 | run、agent、Skill、tool 节点树 |
| 追问 | 旧会话交互结构 | 独立 `input_request`，支持排队和部分提交 |
| 恢复 | 以最新完整状态为准 | `seq`、游标重放、实体版本和 revision |
| 结束 | 旧流可出现 `done` | 没有 `done`，以 run/message 终态为准 |

两套协议不能在同一条流里混用。`context_policy_version` 也不是传输协议版本。

## 2. 身份与订阅范围

一个公开事件范围由 `user_id + session_id + profile_id` 决定。BFF 必须从可信登录态注入
`user_id`，并校验 `profile_id` 属于当前用户。浏览器不能自行声明可信身份。

- 切换用户、会话或档案后必须丢弃旧快照、实体和游标。
- 任务运行期间服务端可锁定档案范围；跨档案请求可能返回
  `409 ACTIVE_RUN_PROFILE_LOCKED`。
- 公开事件不包含内部推理、完整提示词、原始工具参数、完整事实库或调试 trace。

## 3. HTTP 接口

### 3.1 能力发现

```http
GET /api/v3/sessions/{session_id}/harness/capabilities
```

启用时返回：

```json
{
  "protocols": ["hailiang.sse.v3"],
  "streaming_text": true,
  "replay": true,
  "partial_answers": true,
  "finite_run_stream": true,
  "finite_run_stream_default_seconds": 290
}
```

### 3.2 启动 run

```http
POST /api/v3/sessions/{session_id}/harness/runs
Content-Type: application/json
```

```json
{
  "submission_id": "start-20261019-001",
  "goal": "请多位专家共同分析孩子的升学规划",
  "context_data": {
    "user_id": "user_001",
    "profile_id": "profile_001",
    "student_name": "小海"
  }
}
```

`submission_id` 必填，长度 1～100。网络重试必须复用同一个值：相同身份、会话、
`submission_id` 和请求内容返回原 run；同一 ID 携带不同内容返回冲突。成功返回 `202`：

```json
{"run_id":"run_xxx","runtime_mode":"harness_v1","status":"running","superseded_run_id":null}
```

可选字段为 `debug_session_id`、`expert_team_id`、`expert_id`、`clear_experts`。业务前端不应
用这些字段绕过服务端授权目录。

### 3.3 获取一致快照

```http
GET /api/v3/sessions/{session_id}/harness?user_id={user_id}&profile_id={profile_id}
```

返回当前订阅范围的一致公开快照及 `cursor`：

```json
{
  "protocol": "hailiang.sse.v3",
  "session_id": "session_001",
  "profile_id": "profile_001",
  "runtime_mode": "harness_v1",
  "cursor": 128,
  "runs": [],
  "nodes": [],
  "input_requests": [],
  "messages": []
}
```

### 3.4 订阅与重放

```http
GET /api/v3/sessions/{session_id}/harness/events?user_id={user_id}&profile_id={profile_id}&after=128
Accept: text/event-stream
Last-Event-ID: 128
```

- 无 `after` 且无 `Last-Event-ID`：先发送 `snapshot`，再继续推送。
- 两者同时存在：服务端使用较新的有效游标。
- 游标无效、过期或超出当前范围：发送 `resync_required` 后关闭连接。
- 心跳是 SSE 注释 `: heartbeat`，不是业务事件。
- 浏览器断开只关闭订阅，不取消后台 run。

### 3.4.1 单 run 有限流

不支持会话级长连接的转发端使用：

```http
GET /api/v3/sessions/{session_id}/harness/runs/{run_id}/events?user_id={user_id}&profile_id={profile_id}&after=128
Accept: text/event-stream
Last-Event-ID: 128
```

- 无游标时先发送只包含该 run 的 `snapshot`。
- run 终态时发送或重放 `run.finalized`，然后关闭连接。
- run 暂无可运行节点且等待用户时，发送 `run.suspended`，然后关闭连接。
- 默认到 290 秒发送 `stream.timeout`，然后关闭连接。可通过
  `HAILIANG_SSE_V3_RUN_STREAM_SECONDS` 调整，但服务端上限为 295 秒。
- `run.suspended.data.resume_after` 和 `stream.timeout.data.resume_after` 是下一次连接使用的游标。
- 有限流关闭不取消后台任务。用户回答后或超时后，转发端用 `after` 或 `Last-Event-ID` 续传。
- 返回头 `X-SSE-Mode: run` 用于和会话级流区分。

`run.suspended` 与 `stream.timeout` 是传输边界，不是 run 终态。只有 `run.finalized` 表示可以读取
不可变 `/record`。同一范围的游标仍是全局序号；有限流消费者只要求序号单调递增，并以
`resume_after` 续传，不应把其他 run 被过滤形成的序号间隔判断为丢包。

### 3.5 回答追问

```http
POST /api/v3/sessions/{session_id}/input-requests/{request_id}/answers
Content-Type: application/json
```

```json
{
  "user_id": "user_001",
  "profile_id": "profile_001",
  "submission_id": "answer-001",
  "expected_version": 2,
  "answers": {"province": "浙江"},
  "skipped": false
}
```

`expected_version` 必须取当前卡片的 `request_version`，不能使用公开实体的 `version`。

- 部分填写：保存已填字段，其余字段进入 `answer.skipped_fields`。
- 非跳过提交但所有字段均为空：等同全部跳过。
- 全部跳过：`skipped:true`，且 `answers` 和 `text` 必须为空。
- 单一文本字段可用 `text`；它不能与 `answers` 同时提交。
- 同一 `submission_id` 的相同提交幂等；过期版本返回 `409`。
- 前端不得自行把下一张卡片改为 `pending`，只应用服务端事件。

### 3.6 取消 run

```http
POST /api/v3/sessions/{session_id}/harness/runs/{run_id}/cancel
Content-Type: application/json

{"user_id":"user_001","profile_id":"profile_001"}
```

取消是幂等操作。正文已发布的部分保留，消息进入 `interrupted`；迟到结果不能重新激活 run。

## 4. SSE 事件信封

所有业务事件都使用固定 JSON 信封：

```json
{
  "protocol": "hailiang.sse.v3",
  "event_id": "evt_xxx",
  "seq": 128,
  "session_id": "session_001",
  "profile_id": "profile_001",
  "run_id": "run_001",
  "type": "node.updated",
  "entity_id": "task_001",
  "entity_version": 7,
  "timestamp": "2026-10-19T10:00:00+00:00",
  "data": {}
}
```

SSE 的 `id` 等于 `seq`，`event` 等于 `type`。支持的类型：

- `snapshot`
- `run.updated`
- `run.finalized`
- `node.updated`
- `input_request.updated`
- `message.updated`
- `message.delta`
- `resync_required`

`run.finalized` 总是在本 run 的最终节点、卡片和 `message.updated` 投影之后发布。它表示该轮已经
形成不可变历史记录，BFF 可以按 `run_id + record_version` 幂等落库；它不关闭会话级 SSE。
收到后可用事件中的 `record_url`，或调用
`GET /api/v3/sessions/{session_id}/harness/runs/{run_id}/record` 获取完整记录。

除 `message.delta` 外，实体更新都携带该实体当时的完整公开状态，客户端执行 upsert，不能按
JSON Patch 解释。完整 Schema 位于 [`sse_v3/event.schema.json`](sse_v3/event.schema.json)。

## 5. 公开实体

### run

关键字段：`id`、`goal`、`root_task_id`、`status`、`outcome`、`allowed_actions`、
`completed_count`、`task_count`、`waiting_count`、`elapsed_ms`、`phase`、`error`、`version`。

- `status` 表示执行状态。
- `outcome` 表示业务结果：`completed | partial | failed | cancelled | null`。
- `error` 只公开安全错误码和通用文案。
- `allowed_actions` 由服务端计算，前端不自行推断。

### node

关键字段：`id`、`run_id`、`parent_id`、`kind`、`actor`、`title`、`status`、
`outcome`、`attempts`、`started_at`、`ended_at`、`stale`、`error`、`version`。

`kind` 可表示 `main`、`expert`、`skill`、`tool`。工具完成不代表所属专家完成。

### input_request

关键字段：`id`、`run_id`、`owner_task_id`、`actor`、`round`、`fields`、`status`、
`version`、`request_version`、`answer`。

只有 `status=pending` 的卡片可回答；其他专家的请求可保持 `queued`。`answer` 可以包含
`answers`、`skipped` 和 `skipped_fields`。

### message

关键字段：`id`、`run_id`、`kind`、`revision`、`content`、`status`、`version`。

`kind` 为 `phase | final`；状态为 `streaming | completed | interrupted | blocked`。
主 agent 最终正文才公开增量，专家和 Skill 内部输出不逐字推送。

## 6. 状态转换

| 对象 | 合法主路径 |
| --- | --- |
| run | `queued → running ↔ waiting_for_user → completed/failed/cancelled`，随后发布一次 `run.finalized` |
| agent/Skill | `queued → running → waiting_for_user/waiting_for_dependency → queued`，再进入终态 |
| tool | `queued → running → waiting/retry → completed/failed/skipped/cancelled` |
| input_request | `queued → pending → answered/skipped`；取消或失效后为 `expired` |
| message | `streaming → completed/interrupted/blocked` |

一个节点等待用户时，若其他独立节点仍能执行，run 保持 `running`；只有没有可运行节点且存在待答
请求时，run 才进入 `waiting_for_user`。

## 7. 正文增量与去重

`message.delta.data` 至少包含：

```json
{
  "id": "run_001:final",
  "version": 8,
  "revision": 1,
  "offset": 12,
  "text": "下一段😀"
}
```

- `offset` 按 Unicode 码点计数；JavaScript 使用 `Array.from(content).length`。
- 仅当 `revision` 相同且 `offset` 等于当前正文码点长度时追加。
- `seq <= cursor` 是重复事件，忽略。
- 同一实体 `entity_version` 不高于本地版本时忽略实体内容，但仍推进连续游标。
- `seq` 断档、revision 不匹配或 offset 不连续时，停止应用增量并重新拉快照。
- `message.updated` 是权威完整内容，可替换本地正文。
- worker 恢复生成时发布更高 revision，并从 offset 0 开始，不能拼接旧草稿。

## 8. 错误与恢复规则

HTTP 错误体可能是 FastAPI 的 `detail` 字段或 `{code,message}` 对象；客户端以稳定错误码分支，
不要解析文案。至少处理：

| 错误码 | 客户端处理 |
| --- | --- |
| `SSE_V3_DISABLED` | 回退到已配置协议；不要把 v2/v3 流混接 |
| `SUBMISSION_ID_REQUIRED` | 修正请求，不自动生成第二个业务提交 |
| `SUBMISSION_ID_REUSED` | 使用新 ID 或恢复原提交内容 |
| `ACTIVE_HARNESS_RUN_EXISTS` | 拉取快照并展示当前 run |
| `ACTIVE_RUN_PROFILE_LOCKED` | 保持当前档案，待 run 终态后再切换 |
| `HARNESS_ACCESS_DENIED` / `PROFILE_ACCESS_DENIED` | 停止重试并重新鉴权 |
| `INVALID_EVENT_CURSOR` | 拉取快照并以新 cursor 重订阅 |
| 过期回答版本 | 拉取快照，显示当前卡片状态，不重复提交旧答案 |

收到 `resync_required` 的固定恢复步骤：关闭 EventSource → 清空该范围的临时增量状态 → GET
快照 → 用快照整体替换 → 从快照 `cursor` 重订阅。

## 9. 安全与兼容

- BFF 不得重排、聚合、解压后改写或自动解释事件。
- 禁止把内部 trace 暴露到普通用户接口。
- POST 启动不能由 BFF 自动生成新 `submission_id` 重试，否则会重复启动任务。
- V3 允许活动 run 期间启动新 run。服务端在同一事务中把旧 run 标为 `cancelled`，设置
  `cancellation_reason=superseded_by_new_run`，使旧消息进入 `interrupted`，再创建新 run。
- 替代启动后的新 run 只继承 `completed` 轮次的正式问答历史。旧轮草稿、专家中间结果、工具证据、
  未提交追问和非确认的候选/派生事实不得进入新轮上下文；它们仅保留在旧 run 审计中。
- SSE v3 默认通过 `HAILIANG_SSE_V3` 开关控制；旧 SSE v2 继续使用原入口。
- 老 Harness run 没有历史 v3 事件时，通过当前快照建立恢复起点，不虚构旧增量。

