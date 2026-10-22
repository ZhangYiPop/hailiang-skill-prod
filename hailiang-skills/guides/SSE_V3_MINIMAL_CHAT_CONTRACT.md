# SSE v3 最小 Harness 对话入口

> 协议：`hailiang.sse.v3`
> 入口：`POST /api/v3/sessions/chat/stream`

前端（App 或 Web）只发送用户问题、当前追问卡答案或停止指令。`chat` 携带 `expert_team_id` 时进入专家团任务模式；省略它时进入普通 `general_chat` 模式。团队内的专家选择、Skill 选择、任务恢复、问题组推进和任务编排均由算法服务完成。

## 调用层次

前端只能调用 BFF 的同路径入口：`POST {BFF_BASE}/api/v3/sessions/chat/stream`。它的 body 只有 `session_id`、`run_id` 与 `input` 三项；不能信任前端提交的身份或画像。BFF 从登录态和档案服务注入 `context_data`，再将下面的完整请求转发到算法服务。

## 算法服务请求

BFF 到算法服务的请求外层与 v2 一致，`input` 必须是 JSON 字符串：

```json
{
  "session_id": "session_001",
  "run_id": "client_run_001",
  "input": "{\"action\":\"chat\",\"content\":\"帮孩子规划升学路径\",\"expert_team_id\":[\"student_growth_expert_team\"]}",
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

`context_data.user_id` 由 BFF 从登录态注入；前端不得直接提交它。`profile_id` 可在泛聊时省略。`profile_data` 是可选的增量画像字典，可随 `chat` 或表单答案提交：服务端保留原始结构、为叶子字段建立版本化 `profile.*` 事实投影，并仅在任务需要时交给模型。不存在的档案会自动创建；档案属于其他用户时请求被拒绝。`null` 不删除已有画像字段，删除应使用档案或事实接口。
`profile_data.name` 是 V3 的名称字段；为兼容尚未升级的 V2 转发端，顶层 `context_data.student_name` 也会在 `profile_data.name` 缺失时作为名称写入，但两者同时存在时以 `profile_data.name` 为准。

支持的 `input.action` 只有：

```json
{"action":"chat","content":"用户的新问题","expert_team_id":["student_growth_expert_team"]}
```

```json
{"action":"answer_input_request","assistant_message_id":"msg_xxx","request_id":"input_xxx","answers":{"province":"浙江"},"skipped":false,"expert_team_id":["student_growth_expert_team"]}
```

```json
{"action":"resume","assistant_message_id":"msg_xxx","expert_team_id":["student_growth_expert_team"]}
```

```json
{"action":"stop","source":"composer"}
```

`expert_team_id` 对团队模式的 `chat`、`answer_input_request` 与 `resume` 是必填团队边界；省略它的 `chat` 是普通通用对话。推荐类型是 `string[]`，例如 `["student_growth_expert_team"]`；单字符串兼容已有调用。服务端优先使用会话冻结的正式版本；该团队没有冻结版本时使用项目文件中的同 ID 兜底团队。回答必须带所属 run 的同一团队选择，团队不匹配返回 `409 EXPERT_TEAM_MISMATCH`。
回答卡不传 `submission_id`、`expected_version`，但必须传当前气泡的 `assistant_message_id`。服务端按 `request_id` 和规范化答案去重：相同答案幂等返回；不同答案在活动任务中作为用户更正写入新事实版本并重新分析依赖旧事实的节点。`resume` 用于在 `done_timeout`、App 重启或网络恢复后重新接入同一个尚未结束的任务；它不会重新拆分或分派专家。

## SSE 响应

转发端逐字节转发，不聚合、不重排、不改写。每个业务事件都包含可独立渲染的完整状态，并显式说明该帧应如何保存；转发端不必从事件名称或连接关闭状态猜测。

```text
event: state
data: {"protocol":"hailiang.sse.v3","run_id":"bff_run_xxx","state":{...}}

event: message.delta
data: {"protocol":"hailiang.sse.v3","assistant_message_id":"msg_xxx","revision":1,"offset":0,"text":"正在为您"}

event: message.reset
data: {"protocol":"hailiang.sse.v3","assistant_message_id":"msg_xxx","revision":2,"offset":0,"text":""}

event: done_suspended
data: {"protocol":"hailiang.sse.v3","run_id":"bff_run_xxx","state":{...}}

event: done_timeout
data: {"protocol":"hailiang.sse.v3","run_id":"bff_run_xxx","state":{...}}

event: done
data: {"protocol":"hailiang.sse.v3","run_id":"bff_run_xxx","state":{...}}
```

- `state`：任务、画像、专家、工具、问题组、结论或正文发生变化。只更新 BFF 的暂存快照，不作为正式对话历史；其中 `state.assistant_message.content` 是断线恢复时的正文基线。
- `message.delta`：同一 `assistant_message_id` 的用户可见正文增量。前端按 `revision + offset` 追加，首个增量必须立即显示；不得把推理、工具输出或专家中间结果写入该事件。
- `message.reset`：仅在已经流出部分正文后需要受控完整回退时出现。前端清空该气泡草稿并切换到新 `revision`，随后接收新的 delta 或终态 `done`。
- `done_suspended`：当前存在活动表单，且没有其他可运行节点。它是等待用户操作的检查点，不是任务终态。表单出现时若仍有其他专家或工具可运行，服务端继续发送 `state`，不会提前发送本事件。
- `done_timeout`：本次有限 SSE 连接达到等待上限，但后台任务仍在执行。它同样不是任务终态。
- `done`：真正终态。完成、受限完成或失败均由 `outcome` 区分，事件名称不分叉。停止任务时仍为 `done`，但保存指令会要求丢弃暂存记录，沿用既有停止语义。

每个事件的数据都是完整可渲染状态；没有 `snapshot` 包装，也不暴露内部 Harness ID：

```json
{
  "protocol": "hailiang.sse.v3",
  "session_id": "session_001",
  "run_id": "bff_run_001",
  "assistant_message_id": "msg_xxx",
  "expert_team_id": "student_growth_expert_team",
  "expert_team_ids": ["student_growth_expert_team"],
  "team_orchestration_version": "single_team_v1",
  "record_version": 18,
  "cursor": 128,
  "stream_status": "running",
  "outcome": null,
  "persistence": {"should_store_snapshot": true, "should_save_history": false},
  "state": {
    "phase": "experts_running",
    "dispatch": {},
    "nodes": [],
    "inputs": [],
    "input_groups": [],
    "jump_cards": []
  }
}
```

同一任务所有帧的 `assistant_message_id` 不变。任务树、表单、专家结论与最终正文都更新一个助手气泡，最终正文不能另建气泡。`done.state.result` 是终态正文唯一权威，应覆盖累计 delta，处理模型降级或断线重连的差异。`run_id` 是 BFF 为**本次 HTTP 请求**生成的幂等键，响应中原样回显；它不能判断气泡归属或恢复任务。`assistant_message_id` 才是恢复和关联同一气泡的稳定键；不返回 `client_run_id` 或 `harness_run_id`。`expert_team_id` 是当前实际单团队 ID，`expert_team_ids` 是规范化的请求选择列表，`team_orchestration_version` 当前为 `single_team_v1`。旧记录缺少该字段时也按 `single_team_v1` 处理。

### 专家执行树：前端如何读取“某位专家正在做什么”

不需要增加专家详情接口。收到任意完整帧后，从 `state.dispatch.experts[]` 找到目标专家的公开 `node_id`，再在 `state.nodes[]` 按 `id` 找到专家根节点，并递归读取 `parent_id=node_id` 的后代。树固定为“主任务 → 专家 → Skill／工具”。专家节点的 `phase`、`action`、`status`、`waiting_on` 与 `result` 是当前动作、等待原因与已提交结论的公开来源；子节点只补充 Skill/工具步骤。完整帧替换后重新计算树，不能根据上一帧自行补齐或推进。

`state.dispatch.experts[]` 既展示编排首的计划，也建立与实际节点的关联：`subtask_id`、可空的 `node_id`、业务 `brief`、本次 `task_brief`、`completion_criteria`、依赖子任务/节点、`execution_status` 与 `can_run_now`。`parallel_task_count` 是无依赖计划任务数；实际展示并行度应读取 `ready_task_count`、`blocked_task_count` 与 `max_parallelism`。

推荐状态为 `not_started`、`running`、`completed`、`empty`、`unavailable`、`failed`。只有 `completed` 才表示 `jump_cards` 非空；没有命中卡片是 `empty`，推荐失败不影响专家结论和最终回复。

V3 的默认等待上限为 **600 秒（10 分钟）**，可由算法服务环境变量 `HAILIANG_HARNESS_SSE_WAIT_SECONDS` 覆盖。到达上限后只发送 `done_timeout` 检查点，后台任务继续运行。

### 表单与动态问题组：前端怎么显示、怎么推进

表单和专家进度在同一助手气泡的 `state` 中。`state.inputs[]` 每项是一张表单卡片，`fields[]` 才是卡片里的字段；一个卡片可含多个字段。前端按 `state.input_queue.active_request_id` 找到 `state.inputs[]` 中同 ID 且 `status="pending"` 的请求，并展示其 `actor`、`title` 和 `fields`。`actor` 是当前发问的专家；需要关联任务树时，用 `owner_node_id` 对应 `state.nodes[].id`。

`state.input_groups[]` 是服务端管理的问题组。组的 `total_count` / `completed_count` 统计组内表单卡片请求数，**不是字段数，也不是本次 run 的全部问题总数**。前端可显示“本组已完成 X/Y 张卡片”；如果要显示字段数，按组的 `request_ids` 关联 `state.inputs[]` 后累加各请求的 `fields.length`。整个 run 的问题总数不固定：专家恢复执行后可能继续追问，所以不能预先显示固定总题数。

`state.input_queue` 是队列快捷摘要：`active_group_id`、`queued_group_ids`、`active_request_id` 指示当前组、排队组与当前卡片；`active_group_total`、`active_group_completed` 是当前组卡片计数；`pending_count`、`queued_count`、`queued_request_ids` 也是卡片请求计数。只有 `status="pending"` 且 ID 等于 `active_request_id` 的卡片可提交；`queued` 卡片只能作为排队提示，不能提前让用户填写。

用户提交当前卡片后，服务端标记该卡片 `answered` 或 `skipped`。若当前组还有请求，下一张依 `active_request_id` 激活；当前组全部解决后，组变为 `completed`，服务端提升最早的排队组。专家任务在答案写入事实账本后恢复；它可能继续分析，也可能生成下一轮追问。因此前端每次都应以最新完整帧替换状态，再重新查找活动卡片，不能在客户端自行推进。

回答请求继续调用同一 `/api/v3/sessions/chat/stream`，携带新的或重试中的 `run_id`、`session_id`、`expert_team_id`、当前 `assistant_message_id`，动作是 `answer_input_request`，`request_id` 使用活动输入的 `id`。多字段表单传 `answers`；单个文本字段可传 `text`；整张卡片跳过时传 `skipped=true` 且答案为空。V3 支持部分答案，未提供的字段会被记为跳过；部分字段本身不会触发多次恢复。服务端对相同答案幂等处理。

活动卡片出现时，如果其他专家或工具还在运行，流仍会持续以 `state` 更新；只有没有可运行任务且存在待答卡片时才会结束本次流并发 `done_suspended`。前端应每次收到 `state` 就检查 `input_queue` 和 `inputs`，不要等 `done_suspended` 才展示表单。表单状态由完整快照决定，`event` 名称只决定持久化动作。

### 转发端保存规则

| 事件 | `persistence` 指令 | BFF 行为 |
|---|---|---|
| `state` | `should_store_snapshot=true`、`should_save_history=false` | 覆盖暂存快照；不写正式历史。 |
| `done_suspended` | `should_save_history=true`、`record_kind=waiting_for_user` | 用完整快照写入或覆盖同一 `assistant_message_id` 的可继续对话记录。 |
| `done_timeout` | `should_save_history=true`、`record_kind=running` | 用完整快照写入或覆盖同一助手记录，展示“仍在处理中”。 |
| `done`（完成／受限／失败） | `should_save_history=true`、`terminal=true` | 用最终快照覆盖同一助手记录并落正式历史。`outcome` 说明业务结果。 |
| `done`（已停止） | `should_discard_staged=true` | 删除或不写该任务的暂存记录，不新增对话历史。 |

### App 或转发链路中断后的续接

本期 BFF 不需要、也不要求调用 Harness 快照读取接口。BFF 在收到每个 `state` 后保留最新完整帧、原始 `chat` 请求、`assistant_message_id` 和团队选择。用户回到 App 且记录为 `running` 时，BFF 用新的 `run_id` 和该 `assistant_message_id` 发 `resume`；服务端返回既有任务并先发送新的完整 `state`，不会重复分派专家。若记录为 `waiting_for_user`，App 直接展示该帧中的活动卡片；提交答案以同一 `assistant_message_id` 继续原任务。

前端到 BFF 的完整 cURL 场景（新问题、部分回答、跳过、超时续接、新问题打断和停止）见 [SSE_V3_CURL_TEST_PLAYBOOK.md](SSE_V3_CURL_TEST_PLAYBOOK.md)。
