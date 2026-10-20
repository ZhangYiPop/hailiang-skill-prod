# SSE v3 最小 Harness 对话入口

> 协议：`hailiang.sse.v3`
> 入口：`POST /api/v3/sessions/chat/stream`

业务前端只发送用户问题、当前追问卡答案或停止指令。专家选择、Skill 选择、任务恢复、问题组推进和任务编排由算法服务完成。

## 请求

请求外层与 v2 一致，`input` 必须是 JSON 字符串：

```json
{
  "session_id": "session_001",
  "run_id": "client_run_001",
  "input": "{\"action\":\"chat\",\"content\":\"帮孩子规划升学路径\"}",
  "context_data": {
    "user_id": "user_001",
    "profile_id": "profile_001",
    "profile_data": {
      "name": "小明",
      "grade": "高一",
      "province": "浙江",
      "recent_exam": {"total_score": 620}
    }
  }
}
```

`context_data.user_id` 由 BFF 从登录态注入；`profile_id` 可在泛聊时省略。`profile_data` 是可选的增量画像字典，可随 `chat` 或表单答案提交：服务端保留原始结构、为叶子字段建立版本化 `profile.*` 事实投影，并仅在任务需要时交给模型。不存在的档案会自动创建；档案属于其他用户时请求被拒绝。`null` 不删除已有画像字段，删除应使用档案或事实接口。

支持的 `input.action` 只有：

```json
{"action":"chat","content":"用户的新问题"}
```

```json
{"action":"answer_input_request","request_id":"input_xxx","answers":{"province":"浙江"},"skipped":false}
```

```json
{"action":"stop"}
```

回答卡不传 `submission_id`、`expected_version`。服务端按 `request_id` 和规范化答案去重：相同答案幂等返回；不同答案在活动任务中作为用户更正写入新事实版本并重新分析依赖旧事实的节点。

## SSE 响应

转发端逐字节转发，不聚合、不重排、不改写。每个业务事件都包含可独立渲染的完整状态，并显式说明该帧应如何保存；转发端不必从事件名称或连接关闭状态猜测。

```text
event: state
data: {"protocol":"hailiang.sse.v3","run_id":"run_xxx","snapshot":{...}}

event: done_suspended
data: {"protocol":"hailiang.sse.v3","snapshot":{...}}

event: done_timeout
data: {"protocol":"hailiang.sse.v3","snapshot":{...}}

event: done
data: {"protocol":"hailiang.sse.v3","snapshot":{...}}
```

- `state`：任务、画像、专家、工具、问题组、结论或正文发生变化。只更新 BFF 的暂存快照，不作为正式对话历史。
- `done_suspended`：当前存在活动表单，且没有其他可运行节点。它是等待用户操作的检查点，不是任务终态。表单出现时若仍有其他专家或工具可运行，服务端继续发送 `state`，不会提前发送本事件。
- `done_timeout`：本次有限 SSE 连接达到等待上限，但后台任务仍在执行。它同样不是任务终态。
- `done`：真正终态。完成、受限完成或失败均由 `outcome` 区分，事件名称不分叉。停止任务时仍为 `done`，但保存指令会要求丢弃暂存记录，沿用既有停止语义。

每帧均包含以下外层字段：

```json
{
  "protocol": "hailiang.sse.v3",
  "session_id": "session_001",
  "run_id": "run_server_001",
  "harness_run_id": "run_server_001",
  "client_run_id": "client_run_001",
  "assistant_message_id": "msg_harness_run_server_001",
  "record_version": 18,
  "seq": 128,
  "stream_status": "running",
  "outcome": null,
  "persistence": {
    "should_store_snapshot": true,
    "should_save_history": false,
    "replace_staged": false,
    "should_discard_staged": false,
    "terminal": false,
    "record_kind": "progress"
  },
  "snapshot": {
    "protocol": "hailiang.sse.v3",
    "cursor": 128,
    "runs": [], "nodes": [], "input_requests": [],
    "input_groups": [], "messages": []
  }
}
```

`assistant_message_id` 在同一个 Harness 任务的所有帧中保持不变。任务树、表单、专家结论和最终主回复都更新这一个助手气泡，最终回复不能另建气泡。

`event: state` 是事件名称；`data.snapshot` 才是完整状态载体。最小入口与 `/harness/runs/{run_id}/events` 使用相同的公开快照、SSE 游标和 public outbox。单 run 帧中的 `runs`、`nodes`、`input_requests`、`input_groups` 与 `messages` 都只包含该 `harness_run_id` 的实体。

`dispatch.experts[]` 既展示编排首的计划，也建立与实际节点的关联：`subtask_id`、可空的 `node_id`、业务 `brief`、本次 `task_brief`、`completion_criteria`、依赖子任务/节点、`execution_status` 与 `can_run_now`。`parallel_task_count` 是无依赖计划任务数；实际展示并行度应读取 `ready_task_count`、`blocked_task_count` 与 `max_parallelism`。

推荐状态为 `not_started`、`running`、`completed`、`empty`、`unavailable`、`failed`。只有 `completed` 才表示 `jump_cards` 非空；没有命中卡片是 `empty`，推荐失败不影响专家结论和最终回复。

V3 的默认等待上限为 **600 秒（10 分钟）**，可由算法服务环境变量 `HAILIANG_HARNESS_SSE_WAIT_SECONDS` 覆盖。到达上限后只发送 `done_timeout` 检查点，后台任务继续运行。

### 转发端保存规则

| 事件 | `persistence` 指令 | BFF 行为 |
|---|---|---|
| `state` | `should_store_snapshot=true`、`should_save_history=false` | 覆盖暂存快照；不写正式历史。 |
| `done_suspended` | `should_save_history=true`、`record_kind=waiting_for_user` | 用完整快照写入或覆盖同一 `assistant_message_id` 的可继续对话记录。 |
| `done_timeout` | `should_save_history=true`、`record_kind=running` | 用完整快照写入或覆盖同一助手记录，展示“仍在处理中”。 |
| `done`（完成／受限／失败） | `should_save_history=true`、`terminal=true` | 用最终快照覆盖同一助手记录并落正式历史。`outcome` 说明业务结果。 |
| `done`（已停止） | `should_discard_staged=true` | 删除或不写该任务的暂存记录，不新增对话历史。 |

### App 或转发链路中断后的续接

本期 BFF 不需要、也不要求调用 Harness 快照读取接口。BFF 在收到每个 `state` 后保留最新完整快照，同时保存原始 `chat` 启动请求和 `client_run_id`。用户回到 App 且记录为 `running` 时，BFF 用相同的启动请求重试本入口；服务端按 `session_id + user_id + profile_id + client_run_id` 返回既有任务并先发送新的完整 `state`，不会重复分派专家。若记录为 `waiting_for_user`，前端直接展示该快照中的活动卡片；提交答案仍调用同一入口。

请求中的 `run_id` 是 BFF 的客户端关联与幂等键；服务端另行生成 `harness_run_id`，不接受客户端指定真实任务 ID。响应为兼容既有 V2 外层仍保留 `run_id`（服务端 Harness ID），同时提供不歧义的 `client_run_id` 与 `harness_run_id`。
