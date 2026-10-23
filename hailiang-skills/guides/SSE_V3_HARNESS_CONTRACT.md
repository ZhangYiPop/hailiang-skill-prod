# SSE v3 Harness 完整帧契约

> 协议：`hailiang.sse.v3`
> 产品入口：`POST /api/v3/sessions/chat/stream`
> 面向对象：BFF 与 App 前端

V3 的一帧是一个**完整助手气泡快照**。BFF 将最新帧整体替换暂存，前端以同一个 `assistant_message_id` 更新同一个气泡；不读取 Harness 调试接口、不拼接内部事件、不根据本地操作推断任务是否推进。

## 1. 对外关联 ID 与职责

| 字段 | 来源 | 含义 |
| --- | --- | --- |
| `session_id` | BFF | 会话 ID。请求和每个 SSE 帧原样一致。 |
| `run_id` | BFF | 一次 HTTP 请求的幂等键。BFF 创建，重试同一请求时复用；恢复或回答可使用新的值。字段名固定为 snake_case `run_id`，不是 `runId`。 |
| `assistant_message_id` | 算法服务 | 此次 Harness 任务的稳定助手气泡 ID。任务拆解、专家过程、表单、专家结论和最终正文都更新该气泡。 |
| `expert_team_id` | BFF | 专家团模式 `chat`、表单回答和重连的必填团队选择器。省略它的 `chat` 是普通通用对话。兼容单个字符串；推荐传字符串数组，为多专家团预留。 |
| `expert_team_ids` | 算法服务 | 每帧回显的规范化团队选择数组。当前与 `expert_team_id` 一一对应；未来多团队编排启用后才可能含多个值。 |
| `team_orchestration_version` | 算法服务 | 本 run 的团队编排兼容版本。当前固定为 `single_team_v1`；旧记录缺失时也按该单团队语义解释。 |

`assistant_message_id` 是同一任务和气泡的唯一关联键；BFF 改用新 `run_id` 不会创建新任务，只要 `answer_input_request` 或 `resume` 仍携带原 `assistant_message_id`。只有新的 `chat` 才创建新任务和新气泡。算法不在产品帧中暴露 `client_run_id`、`harness_run_id`、数据库运行 ID 或真实节点 ID；这些只属于受控调试接口。

算法不提供点赞、点踩或分享能力。BFF 可以用 `session_id + run_id + assistant_message_id` 关联自己的消息和互动记录。

## 2. 请求动作

外层固定使用 `session_id`、`run_id`、序列化 JSON 的 `input` 与 `context_data`。App 经 BFF 调用，不直接理解专家、Skill、节点或版本。

```json
{"action":"chat","content":"帮孩子规划升学路径","expert_team_id":["student_growth_expert_team"]}
```

```json
{
  "action":"answer_input_request",
  "assistant_message_id":"msg_xxx",
  "request_id":"input_req_xxx",
  "expert_team_id":["student_growth_expert_team"],
  "answers":{"province":"浙江"},
  "text":"补充说明",
  "skipped":false
}
```

```json
{"action":"resume","assistant_message_id":"msg_xxx","expert_team_id":["student_growth_expert_team"]}
```

`resume` 用于 `done_timeout`、App 重启或网络恢复后重新接入尚未结束的同一任务；使用新的 `run_id` 不会重新分派专家。若任务已终态，服务端只回放原 `done`。

停止仍走现有 BFF 停止接口（例如 `/api/v4/chat/stop`），由 BFF 使用原 `session_id`、`run_id` 转发：

```json
{"action":"stop","source":"composer"}
```

App 不直接调用 stream 入口的停止动作。普通 `chat` 可以省略 `expert_team_id`，并且只产生普通助手气泡；专家团 `chat`、`answer_input_request` 与 `resume` 缺少或改写它均应被拒绝。要换专家团，创建新的 `run_id` 并发起新的团队 `chat`。

`expert_team_id` 的推荐类型为 `string[]`，例如 `["student_growth_expert_team"]`；历史 `string` 仍兼容。当前 `orchestrator_head` 只具备**单团队**编排能力，因此数组必须恰好含一个、去重后的合法团队 ID。传入多个团队将返回 `422 MULTI_EXPERT_TEAMS_NOT_ENABLED`，不会静默选择第一个；该限制将在多团队协调能力实现后移除。`stop` 不传该字段。

当前新 run 写入 `team_orchestration_version="single_team_v1"`。它只标记兼容语义，不代表当前已经执行多团队协作；旧 run 与旧对话快照缺失该字段时，客户端和服务端均按 `single_team_v1` 处理。未来启用真实多团队运行时才会引入新的版本值及团队级执行树，现有 `run_id`、`assistant_message_id` 和历史对话记录均不需要迁移。

## 3. 完整帧骨架

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
  "cursor": 42,
  "reason": "task_started",
  "stream_status": "running",
  "outcome": null,
  "profile_id": "profile_001",
  "profile_name": "小海",
  "context_scope": "profile",
  "context_label": "小海",
  "context_notice": {
    "type": "profile_context_activated",
    "text": "本轮回答将结合 **小海** 的档案数据。"
  },
  "profile_context_status": "matched",
  "persistence": {
    "should_store_snapshot": true,
    "should_save_history": false,
    "replace_staged": false,
    "should_discard_staged": false,
    "terminal": false,
    "record_kind": "progress"
  },
  "state": {
    "status": "running",
    "phase": "experts_running",
    "action": "专家正在并行分析",
    "dispatch": {},
    "nodes": [],
    "inputs": [],
    "result": "",
    "recommendations_status": "not_started",
    "jump_cards": [],
    "safety": {"replacement_applied": false, "replacement_source": null}
  }
}
```

`record_version` 单调递增；收到版本不高于已渲染版本的帧可忽略。`reason` 是展示刷新原因，不能替代 `state` 的业务状态。

## 4. 专家树：如何知道某个专家正在做什么

`state.nodes` 是扁平的树表，通过 `id` 与 `parent_id` 建树：

```text
main（主编排器）
├─ expert（升学规划专家）
│  ├─ skill（选科分析能力）
│  │  └─ tool（读取档案 / 检索资料 / 请求补充）
│  └─ tool（专家直接调用的工具）
└─ expert（学习指导专家）
   └─ tool（读取档案）
```

前端应先按 `parent_id` 建索引，再将根节点和子节点递归渲染。`id` 是产品公开投影 ID，只可用于本帧树关联；不可传回算法，也不可当作真实数据库 ID 保存。

| `kind` | 含义 | 展示位置 |
| --- | --- | --- |
| `main` | 主编排器 / 最终汇总节点 | 根节点 |
| `expert` | 一个已分派的业务专家 | 主节点下 |
| `skill` | 专家或主编排器授权调用的专项能力 | 所属专家或主节点下 |
| `tool` | 当前节点的具体工具动作 | 所属 task / skill 下 |

每个节点的稳定公开字段如下：

| 字段 | 用途 |
| --- | --- |
| `title`、`actor` | 节点名称和执行者名称。 |
| `brief` | 面向用户的当前任务书摘要。专家的业务职责说明在 `agent_brief`，来源是业务人员维护的 agent brief，不是模型临时生成。 |
| `status` | `queued`、`running`、`waiting_for_user`、`waiting_for_dependency`、`completed`、`failed`、`cancelled`。 |
| `phase`、`action` | 当前阶段与可直接展示的业务动作；展示“正在执行：{action}”，不要显示抽象的“等待任务结果”。 |
| `waiting_on` | 等待的公开节点 ID；据此展示“等待某专家/步骤”。 |
| `children_count`、`completed_children_count` | 本节点直接子步骤数量及完成数。 |
| `attempts`、`error` | 工具的重试次数及公开失败原因。 |
| `result` | 已完成专家的结构化结论；未完成时为 `null`。 |
| `outcome` | `completed`、`partial` 或 `failed`；与技术状态分开表达业务结果。 |

专家完成后，前端即可用 `node.result` 展示专家结论卡；不要等待最终总结。`result` 的固定形状为：

```json
{
  "conclusion": "结论正文",
  "findings": ["可选关键发现"],
  "limitations": ["适用限制"],
  "unresolved": ["仍未解决事项"]
}
```

专家结论至少保证 `conclusion`；数组不存在或为空时不渲染对应小节。专家中间结论仍是整段提交，不逐字流式。最终主回复在 `message.delta` 中按 `assistant_message_id + revision + offset` 逐步发送；发生受控完整回退时服务端先发送 `message.reset`，终态始终以 `done.state.result` 覆盖累计正文。

## 5. 编排首分派、汇总与推荐何时展示

`state.dispatch` 是“编排首已经规划的任务”，`nodes` 是“实际已创建并执行的节点”。两者必须同时使用，不能把已经计划误展示为已经运行。

```json
{
  "status": "completed",
  "planned_task_count": 2,
  "planned_expert_count": 2,
  "parallel_task_count": 2,
  "experts": [
    {
      "node_id": "node_2",
      "expert_id": "academic_coach",
      "name": "学习指导师",
      "brief": "分析学习习惯并提出改进方案",
      "agent_brief": "负责学习规划与习惯培养",
      "status": "assigned",
      "depends_on": []
    }
  ]
}
```

| 完整帧中的条件 | 可以展示的内容 |
| --- | --- |
| `state.phase=planning` 且 `dispatch.status` 不是 `completed` | “正在理解问题并拆分任务”。 |
| `dispatch.status=completed` | “已拆分为 N 个专家任务”、每个专家名、任务 `brief`、依赖和可并行数量。此时专家可以仍是 `assigned/queued`。 |
| 专家节点 `status=running` 或 `phase=analyzing/using_skill/using_tool` | 专家执行树，以及该节点 `action`。 |
| 节点 `waiting_for_dependency` | `waiting_on` 对应的专家或步骤。 |
| 节点 `waiting_for_user` 或当前活动表单存在 | “等待你补充信息”和表单；其他仍在运行的专家继续显示。 |
| 专家节点有 `result` | 该专家的结论卡，并显示“等待编排器汇总”直到 `state.phase=synthesizing` 或最终完成。 |
| `state.phase=synthesizing` | “编排器正在汇总专家结论”。 |
| `state.recommendations_status=running` | “正在生成相关推荐”。 |
| `state.recommendations_status=completed` 且 `jump_cards` 非空 | 渲染推荐卡。 |
| `state.status=completed` 或 `event: done` | 最终正文、最终专家结论、推荐和完整任务记录。 |

## 6. `jump_cards` 推荐卡

`state.jump_cards` 是编排尾在生成最终回复后输出的推荐卡数组，对旧推荐路由决策表做公开投影。它不是任务树节点，也不依赖任何内部路由接口；前端直接按数组顺序渲染即可。

```json
{
  "recommendations_status": "completed",
  "jump_cards": [
    {
      "id": "rec_5ffc06b319fc6d1f",
      "title": "学科潜能测评",
      "description": "了解物理、化学和技术学科的匹配度",
      "reason": "可先完成测评，再结合结果调整选科策略。",
      "intent": "assessment",
      "object_type": "single_assessment",
      "sub_type": "subject_aptitude",
      "filter": {
        "region": "浙江",
        "grade": "高一",
        "keyword": "",
        "category": ""
      }
    }
  ]
}
```

| 字段 | 含义 | 前端处理 |
| --- | --- | --- |
| `id` | 服务端生成的稳定推荐卡 ID | 列表 key 与 BFF 记录关联键。 |
| `title`、`description` | 卡片标题和补充说明 | 直接展示；`description` 可为空。 |
| `reason` | 本轮建议该卡的原因 | 展示为推荐理由；不能当作最终正文的一部分重复拼接。 |
| `intent` | 推荐意图，例如 `assessment` | 传给现有推荐跳转逻辑。 |
| `object_type` | 跳转对象类型，例如 `single_assessment` | 与既有推荐路由的对象类型一一对应。 |
| `sub_type` | 对象子类型，例如 `subject_aptitude` | 与既有推荐路由的子类型一一对应。 |
| `filter` | `{region, grade, keyword, category}` 查询条件 | 原样传给既有推荐页；空字符串表示不限制该维度。 |

`recommendations_status` 的渲染规则：

| 值 | 说明 | UI |
| --- | --- | --- |
| `not_started` | 编排尾尚未进入推荐阶段 | 不展示推荐区。 |
| `running` | 正在生成推荐 | 展示“正在生成相关推荐”。 |
| `completed` | 推荐成功完成 | 渲染非空 `jump_cards`。 |
| `empty` | 推荐过程完成但没有适用卡片 | 不展示推荐区。 |
| `failed` | 推荐生成失败 | 不影响最终回答或专家结论；不展示推荐区。 |
| `not_available` / `unavailable` | 当前专家团或环境未接通推荐能力 | 不展示推荐区。 |

只有 `recommendations_status="completed"` 且 `jump_cards.length > 0` 时展示卡片。完整帧后续更新会替换整个数组，前端不要根据上一次帧追加或保留已消失的推荐卡。

## 7. 表单、问题组与动态追问

表单由专家或主编排器通过 `request_user_input` 创建。一个输入请求（`state.inputs[]` 中的一项）对应一张表单卡片；卡片的 `fields[]` 可以包含一个或多个字段。服务端按到达顺序将输入请求组织成问题组（`state.input_groups[]`）并排队；不同专家的请求可以落在同一队列批次。**一个问题组不是一题，也不是整个任务的全部问题；`total_count` / `completed_count` 统计的是卡片请求数，不是 `fields[]` 的字段数。**

问题是动态产生的：专家可能在用户回答当前卡片后继续分析，再提出下一轮问题。因此服务端无法在任务开始时知道整个 run 最终会问几题，前端不得显示“总共 N 题”作为全任务固定总数。前端可以显示“本组第 X 张卡片，共 Y 张”，其中 X/Y 来自组内请求卡片进度；若需要显示字段数，可对该组 `request_ids` 对应的输入请求累加 `fields.length`，不要把它与卡片数混为一谈。

### 前端读取字段

完整帧中的表单状态位于 `state`：

- `state.inputs[]`：每张输入卡片。`id` 是提交时的 `request_id`；`fields` 是本卡片字段；`status` 为 `pending` 时才可填写，`queued` 时尚未轮到用户；`answered`、`skipped` 表示已解决。`actor` 是发问专家名称；`owner_node_id` 可关联到 `state.nodes[]` 中的专家节点；`round` 是所属专家的追问轮次；`group_id`、`group_position` 表示该卡片所在组及组内顺序；`request_version` 是服务端请求版本。
- `state.input_groups[]`：问题组快照。`id`、`status`、`request_ids` 表示组身份、状态及组内卡片；`active_request_id` 是当前应展示的卡片；`total_count` 是组内卡片总数；`completed_count` 是已回答、已跳过或已过期的卡片数；`actors` 是组内涉及的专家；`version` 用于判断组状态是否更新。
- `state.input_queue`：便于显示队列进度的摘要。`active_group_id` / `queued_group_ids` 表示当前组与后续组；`active_request_id` 是当前卡片；`active_group_total` / `active_group_completed` 是当前组的卡片总数与已解决数；`queued_request_ids`、`pending_count`、`queued_count` 是当前等待队列摘要。`pending_count` 和 `queued_count` 统计请求卡片，不统计字段。

示例：

```json
{
  "input_queue": {
    "active_group_id": "group_1",
    "queued_group_ids": ["group_2"],
    "active_request_id": "input_1",
    "active_group_total": 2,
    "active_group_completed": 0,
    "queued_request_ids": ["input_3"],
    "pending_count": 1,
    "queued_count": 1
  },
  "inputs": [
    {"id":"input_1","title":"学生情况","status":"pending","actor":"升学规划专家","group_id":"group_1","group_position":1,"fields":[{"key":"province","label":"所在省份","input_type":"single_select","options":[{"label":"浙江","value":"浙江"}],"required":true,"scope":"profile"}]},
    {"id":"input_2","title":"目标偏好","status":"queued","actor":"升学规划专家","group_id":"group_1","group_position":2,"fields":[{"key":"goal","label":"目标","input_type":"text","required":false,"scope":"task"}]},
    {"id":"input_3","title":"家庭情况","status":"queued","actor":"家庭教育专家","group_id":"group_2","group_position":1,"fields":[{"key":"schedule","label":"可投入时间","input_type":"text","required":false,"scope":"task"}]}
  ],
  "input_groups": [
    {"id":"group_1","status":"active","request_ids":["input_1","input_2"],"active_request_id":"input_1","total_count":2,"completed_count":0,"actors":["升学规划专家"],"version":1},
    {"id":"group_2","status":"queued","request_ids":["input_3"],"active_request_id":null,"total_count":1,"completed_count":0,"actors":["家庭教育专家"],"version":1}
  ]
}
```

查找活动卡片时，优先读取 `state.input_queue.active_request_id`，再在 `state.inputs[]` 中按 `id` 匹配，并确认 `status="pending"`。也可以读取 `state.input_groups[]` 中 `status="active"` 的组，再用其 `active_request_id` 查找卡片。两者都指向同一活动请求；没有活动 ID 或没有对应的 `pending` 请求时，不显示可提交表单。不要把所有 `queued` 输入同时呈现为可填写卡片。

展示追问时读取该卡片的 `actor`；若需要点亮任务树中的所属节点，使用 `owner_node_id` 与 `state.nodes[].id` 关联。所属节点可能是专家，也可能是主编排器。不要从正文、工具名或本地规则推断发问者。

单张卡片字段兼容现有 `AIChatDynamicFormField` 的核心形状：

```json
{
  "key":"province",
  "fact_key":"student_province",
  "label":"所在省份",
  "input_type":"single_select",
  "options":[{"label":"浙江","value":"浙江"}],
  "required":true,
  "scope":"profile",
  "min":null,
  "max":null
}
```

- `fact_key` 缺省时使用 `key`；`options` 固定为 `{label,value}` 数组。
- 输入类型为 `text`、`single_select`、`multi_select`、`integer`、`number`；选项和数值边界以服务端字段定义为准。
- 服务端维护问题组和跨专家复用：同一事实身份的答案只填写一次，所有匹配专家得到相同工具结果。完全相同的问题可能绑定多个专家任务，用户提交一次即可恢复所有绑定任务。
- 前端在一张卡片内收集答案后，按 `request_id` 提交一次。可以部分填写；未填字段按本次跳过字段处理，不覆盖已有事实。全空提交等同跳过整张卡片。提交一部分字段并不会逐字段触发模型或推进卡片。
- 当前卡片提交后，服务端更新完整状态帧：若当前组还有卡片，下一张会变为 `pending`；若当前组所有卡片已解决，当前组变为 `completed` 并提升最早的 `queued` 组；相关专家任务随后恢复分析。恢复后专家可能创建新卡片，新卡片进入新的活动组或队列组。
- 前端始终以最新完整帧替换气泡状态，再按 `input_queue.active_request_id` 重新选择卡片；不要自行切换同组下一题、推进组、移除其他专家的追问，或假设提交后任务已经完成。
- 如果卡片仍为活动输入，但还有其他专家或工具可运行，SSE 会继续发送 `state`；只有没有可运行任务且仍等待用户时才发送 `done_suspended`。因此前端应在每个 `state` 帧检查活动卡片，不能只在 `done_suspended` 后才显示表单。

### 回答请求

调用 `answer_input_request` 时使用任意新的或重试中的 `run_id`，并携带同一 `session_id`、`expert_team_id`、`assistant_message_id`；`request_id` 必须是当前活动卡片的 `id`。多字段卡片使用 `answers`，单个文本字段可以使用 `text`。跳过整张卡片时传 `skipped=true` 且不传答案。V3 当前允许部分答案：已提供字段被保存，缺失字段记为跳过。服务端按规范化答案幂等处理重复提交；用户在 run 仍活动时更正已提交答案，会更新事实版本并让依赖旧答案的任务重新分析。

```json
{"action":"answer_input_request","assistant_message_id":"msg_xxx","request_id":"input_1","expert_team_id":["student_growth_expert_team"],"answers":{"province":"浙江"},"skipped":false}
```

回答后继续消费 SSE 事件并用每个新帧覆盖同一助手气泡。可能依次出现同组下一张卡、下一问题组、专家运行过程、由恢复后的专家提出的新一轮卡片，最后才出现 `done`。如为 `done_timeout`，任务仍在运行；按合同用 `assistant_message_id` 发起 `resume` 重新接入。`done_suspended` 是可以等待用户的检查点，不代表专家团任务已经完成。

## 8. 事件、保存、超时与恢复

| 事件 | 条件 | BFF 行为 |
| --- | --- | --- |
| `state` | 任务、分派、节点、表单、结论、画像或正文更新 | 覆盖暂存完整快照，不写正式历史。 |
| `done_suspended` | 有活动表单，且没有其他可运行节点 | 用完整快照保存为 `waiting_for_user`。 |
| `done_timeout` | 到达 BFF 的有限连接等待边界 | 用完整快照保存为 `running`。后台可能仍在执行；本期不自动续连。 |
| `done` | 完成、受限完成、失败或停止 | 按 `persistence` 保存最终记录；停止时丢弃暂存，不写历史。 |

`state` 没有固定推送周期：它只在公开任务状态发生变化时发送，模型或工具长时间执行而状态未改变时可能没有新的 `state`。服务端在连接空闲 15 秒后发送与 V2 相同的 `event: ping`、`data: {}` 保活，可通过 `HAILIANG_HARNESS_SSE_HEARTBEAT_SECONDS` 调整。`ping` 是传输事件，BFF 与前端不保存、不渲染，也不应把它当作状态变化。

BFF 需要保存：最新完整快照、原始 `chat` 请求、`assistant_message_id` 和团队选择。App 被系统杀进程后回到会话，BFF 用新的 `run_id` 和保存的 `assistant_message_id` 发送 `resume`，服务端先返回此任务的最新完整快照并避免重复分派。前端不需要了解 Harness 查询接口。

`done_timeout` 没有自动续接次数：**本期为 0 次**。它只是让 BFF 在自己的时限前可落库的检查点；UI 保持同一气泡为“仍在处理中”，等待 App 下次进入会话触发 BFF 重连。

出现表单不必然发 `done_suspended`：只要仍有其他专家或工具可运行，流继续以 `state` 推送。只有没有可运行节点、活动表单等待用户时才结束本次有限流并发送 `done_suspended`。

## 9. 个性化与风控

V3 沿用 V2 的 `profile_id`、`profile_name`、`context_scope`、`context_label`、`context_notice` 和 `profile_context_status`。仅在：

```text
context_scope = "profile" && profile_context_status = "matched"
```

时展示“本次回答结合某孩子的个性化数据”。`context_notice.text` 是独立提示，不拼入正文。

风控只放在两个业务边界：

- 用户输入进入会话前（`stage=input`）；命中后不启动任务、专家、工具或脚本。
- 最终用户可见回复落地前（`stage=output`）；任务编排、专家中间答复、表单、工具参数、脚本输入/输出和推荐卡生成均不调用阿里云风控。

为避免尚未审核的文字泄露，最终模型调用仍从模型端流式读取，但在完成输出审核后才以 `message.delta` 分段发送到浏览器。这样不会把中间任务节点误送审，也不会把被拦截的最终草稿先显示再撤回。

用户可见最终输出被审核拦截时，算法先取阿里云 `Data.Advice[].Answer` 中第一条非空代答；没有代答则使用项目安全兜底文案。仍发送可保存的 `done`：

```json
{
  "outcome":"partial",
  "state":{
    "result":"安全代答或项目兜底文案",
    "safety":{"replacement_applied":true,"replacement_source":"aliyun_advice"}
  }
}
```

审核服务、会话审计与候选测试导出会记录 `stage`、供应商、风险级别、标签、供应商请求 ID、云服务失败原因、隔离案件 ID 及“是否替换为代答”；日志不写原始命中正文。不要以 `message.status=blocked` 推断风控，不公开命中词、原始模型文本、审核原始响应或内部推理。

## 10. 2026-10-21 测试服务器验证结果

以 `POST http://10.30.6.45:8011/api/v3/sessions/chat/stream` 使用独立会话进行真实验证：首轮出现身份表单；提交后，服务端确实产生了 `main → expert → tool` 层级、编排首 `dispatch`、专家 `brief/action` 和输入组。

但该测试环境返回的是部署前兼容格式：顶层使用 `snapshot`，并外露 `harness_run_id`、`client_run_id` 和真实节点 ID，且 `run_id` 被替换为内部运行 ID；其 `stop` 输入还要求 `expert_team_id`，并拒绝本文约定的 `source:"composer"`。这与本文第 1–3 节的最小 V3 产品契约不一致，**不能让前端按该现网帧接入**。部署包含最小入口适配器的版本后，应重新执行同一回归；验收条件是：只出现本文规定的三个对外关联 ID，完整数据位于 `state`，保持 `run_id` 原样回显，并接受本文第 2 节的 stop 输入。
