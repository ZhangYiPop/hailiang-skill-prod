# SSE v3：Harness 多 Agent 公开协议

协议版本：`hailiang.sse.v3`。本文件是业务前端与 BFF 的唯一公开契约；细粒度 outbox、模型 trace、原始工具参数和内部事件不属于本协议。

## 1. 消费模型

SSE v3 使用 V2 风格的**完整状态帧**。除 `ping` 外，所有帧的 `data.snapshot` 都是当时完整、可独立渲染的公开快照。客户端校验范围和游标后整体替换本地状态，不能自行推进任务、卡片或正文。

| event | 语义 | 连接行为 |
| --- | --- | --- |
| `state` | 任一公开状态改变 | 保持连接 |
| `done_suspended` | 无可运行节点且活动问题组等待用户，保存可续接记录 | 关闭本次有限流 |
| `done_timeout` | 有限流达到等待边界、run 仍在后台运行，保存可续接记录 | 关闭本次有限流 |
| `done` | run 已终态，公开记录已可落库 | 关闭本次有限流 |
| `ping` | SSE 注释保活 | 忽略 |

`node.updated`、`input_request.updated`、`message.delta`、`run.finalized` 仅存在于内部 outbox/诊断接口；BFF 和浏览器不得订阅或依赖它们。

## 2. 接口

细粒度 Harness 接口的完整前缀为：`/api/v3/sessions/{session_id}`。业务 BFF 默认使用下方的最小入口 `POST /api/v3/sessions/chat/stream`；其外层采用 V2 形状，`session_id` 放在 body，`input` 为 JSON 字符串。`input` 的每一种 action 都必须带 `expert_team_id`，只允许指定团队，不能指定团队成员、Skill、路由状态或任务树。资源标识放在 URL 路径；POST 的业务数据放在 JSON 请求体；读取身份、档案和游标放在 query 参数。POST 参数不应全部拼到 URL 中。

| 方法和完整路径 | 参数位置 | 行为 |
| --- | --- | --- |
| `POST /api/v3/sessions/chat/stream` | body：`session_id`、`run_id`、`input`、`context_data`；`input` 内必填 `expert_team_id` | BFF 唯一业务对话入口：启动、回答表单或停止，同一响应返回有限完整 SSE 流。 |


| 方法和完整路径 | 参数位置 | 行为 |
| --- | --- | --- |
| `POST /api/v3/sessions/{session_id}/harness/runs` | body：`submission_id`、`goal`、`context_data`，可选 `debug_session_id`、`expert_team_id`、`expert_id`、`clear_experts` | 启动 run，返回 `202` 和 `run_id`；`submission_id` 用于幂等 |
| `GET /api/v3/sessions/{session_id}/harness` | query：`user_id`、可选 `profile_id` | 获取订阅范围的一致完整快照 |
| `GET /api/v3/sessions/{session_id}/harness/runs/{run_id}/events` | path：`run_id`；query：`user_id`、可选 `profile_id`、`after`；也支持 `Last-Event-ID` | 单 run 有限 SSE 流，供不支持长连接的 BFF 使用 |
| `GET /api/v3/sessions/{session_id}/harness/events` | query：`user_id`、可选 `profile_id`、`after`；也支持 `Last-Event-ID` | 会话级调试/订阅入口；生产 BFF 默认使用单 run 流 |
| `POST /api/v3/sessions/{session_id}/input-requests/{request_id}/answers` | path：`request_id`；body：`user_id`、`profile_id`、`submission_id`、`expected_version`、`answers`、`text`、`skipped` | 提交部分答案或全部跳过；`Accept: text/event-stream` 时直接返回后续有限流 |
| `POST /api/v3/sessions/{session_id}/harness/runs/{run_id}/cancel` | path：`run_id`；body：`user_id`、可选 `profile_id` | 幂等取消 run 和后代 |
| `GET /api/v3/sessions/{session_id}/harness/runs/{run_id}/record` | path：`run_id`；query：`user_id`、可选 `profile_id` | 在 `done` 后读取不可变对话记录 |

身份必须由可信 BFF 登录态注入。浏览器提交的 `user_id`、`profile_id` 只能作为服务端校验参数，不能建立身份信任。

## 3. 完整帧

```text
id: 128
event: state
data: {
  "protocol": "hailiang.sse.v3",
  "seq": 128,
  "session_id": "session_xxx",
  "profile_id": "profile_xxx",
  "run_id": "run_xxx",
  "status": "running",
  "outcome": null,
  "reason": "updated",
  "snapshot": {
    "protocol": "hailiang.sse.v3",
    "cursor": 128,
    "runs": [], "nodes": [], "input_requests": [],
    "input_groups": [], "messages": []
  }
}
```

`seq` 与 SSE `id` 相同。断线后使用最后一个有效 `id` 作为 `after` 或 `Last-Event-ID` 续传；同一会话范围中其他 run 被过滤后，序号可以不连续。游标无效时服务端发送 `resync_required` 并关闭，客户端重新获取 `/harness` 快照。

`done` 额外含 `record_version`、`record_url` 和最终消息版本。每帧另含 `client_run_id`、稳定的 `assistant_message_id` 与 `persistence`：转发端以它明确决定只暂存、写/覆盖同一对话记录或丢弃停止任务的暂存。`done_suspended` 和 `done_timeout` 不是任务失败。


### BFF 保存指令

每个事件的 `persistence` 包含 `should_store_snapshot`、`should_save_history`、`replace_staged`、`should_discard_staged`、`terminal` 与 `record_kind`。`state` 只暂存；`done_suspended` 保存“等待回答”记录；`done_timeout` 保存“仍在处理”记录；`done` 在完成、受限完成或失败时保存最终记录，`outcome=cancelled` 时丢弃暂存且不写历史。所有任务进度、表单、专家结论和最终正文使用相同的 `assistant_message_id` 更新一个助手气泡。

## 4. 公开快照

`snapshot` 至少包含 `runs`、`nodes`、`input_requests`、`input_groups`、`messages` 和 `cursor`。

- `runs[].input_queue`：`active_group_id`、`queued_group_ids`、`active_request_id`、活动组总题数/完成数及旧兼容计数。
- `runs[].round_experts`：当前轮专家 ID 的 `assigned/running/completed/failed` 列表与汇总状态；同一专家只保留最近任务状态。
- `runs[].jump_cards`：服务端校验过的推荐卡片，含稳定 `id`、intent、对象类型、筛选条件、标题、说明和推荐原因；没有推荐时为 `[]`。
- `runs[].dispatch`：任务分派摘要。`source=orchestrator_head` 表示编排首已验证的计划，`source=main_agent_delegate` 表示主 agent 已直接创建专家任务。每项含稳定 `subtask_id`、实际创建后的 `node_id`、专家名称、业务人员编写的专家 `brief`、本次 `task_brief`、`completion_criteria`、依赖子任务/节点、`execution_status` 和 `can_run_now`。`brief` 来自已发布专家定义。`ready_task_count`、`blocked_task_count` 与 `max_parallelism` 才是前端展示实际调度情况的依据。
- `runs[].phase`、`runs[].action`：当前主任务阶段和面向用户的动作说明。典型顺序为 `planning → dispatching_completed → experts_running → experts_completed → synthesizing → recommending → completed`。
- `runs[].synthesis_status`、`runs[].recommendations_status`：汇总和推荐的独立状态；专家完成不等于最终回复完成。推荐状态为 `not_started | running | completed | empty | unavailable | failed`；只有 `completed` 表示存在推荐卡片。
- `input_groups[]`：稳定组 ID、`active | queued | completed`、创建时间、题目 ID、当前题、题数、完成数和专家摘要。
- `input_requests[]`：单题字段、`request_version`、状态、回答、绑定、专家和所属组。只有 `input_queue.active_request_id` 对应且状态为 `pending` 的卡片可填写。
- `nodes[]`：主任务、专家、Skill 和工具树。用 `dispatch.experts[].node_id → nodes[].id` 定位专家根节点，再按 `parent_id` 递归取后代，即可展示该专家正在执行的步骤。专家、Skill 与其工具节点含稳定 `expert_id`；`actor` 仅为展示名称。工具完成不等于所属专家完成。节点还可包含 `brief`、`phase`、`action`、`waiting_on`、子节点计数和受控的 `result` 摘要（`conclusion`、`findings`、`limitations`、`unresolved`），用于展示任务书、当前动作、依赖和专家结论。
- `messages[]`：阶段说明与主 agent 最终正文；前端直接使用 final 消息当前完整 `content`。

公开数据不含专家内部推理、完整提示词、原始工具参数、完整事实库或 trace。

### 4.1 每个完整帧的展示规则

前端在收到任意 `state`、`done_suspended`、`done_timeout` 或 `done` 后，先用 `assistant_message_id` 找到同一个助手气泡，再以整个 `data.snapshot` 替换该气泡的任务状态。**事件名只表示本次连接应如何处理；具体展示内容只依据当前快照字段。**

| 当前快照条件 | 可立即展示 | 不应提前展示 |
| --- | --- | --- |
| `run.phase=planning`，`dispatch=null` | “正在理解问题并拆分任务”、主任务 `action`；旧快照兼容时可退回根主节点的 `phase/action` | 专家列表、专家结论、推荐卡。 |
| `run.dispatch` 非空，或帧 `reason=dispatch_completed` | “已分派 N 个专家任务”、每位专家的名称、业务 `brief`、本次 `task_brief`、完成条件、依赖和可执行/阻塞数量；同时标明 `source` | 将 `plan_status=assigned` 解释为该专家已经开始执行。实际状态看 `execution_status` 和 `nodes`。 |
| `run.phase=experts_running`，存在专家/Skill/工具节点 | 专家树、节点 `phase`、`action`、重试次数、失败原因；`waiting_on` 非空时显示具体等待对象 | 工具节点完成时显示“专家已完成”。只有专家节点有合法 `result` 且进入 `completed` 或 `partial` 才算专家完成。 |
| 专家节点 `result` 非空 | 该专家的结论、关键依据、限制与未解决事项；同时仍显示“等待编排器汇总”，直到 `synthesis_status=completed` | 专家内部推理、完整工具输入输出或未校验的中间文本。 |
| `input_queue.active_request_id` 指向状态为 `pending` 的 `input_request` | 该请求的表单，标明 `actor` 与所属专家；活动组的“第 N/M 题” | 同组其他请求或后续 queued 组的输入控件。它们只显示题数和专家摘要。 |
| 有 `input_groups`，但 `active_request_id=null` 或请求为 `queued` | “后续待回答问题组”及每组题数、专家摘要 | 任何可填写表单。前端不能自行激活下一题或下一组。 |
| `run.status=waiting_for_user` 且事件为 `done_suspended` | 当前表单和“等待你补充信息”；按 `persistence` 保存为可继续记录 | “任务失败”或“任务已结束”。其他专家停止前可能只收到 `state`，不会有 `done_suspended`。 |
| `synthesis_status=running` 或 `run.phase=synthesizing` | “编排器正在汇总专家结论” | 最终正文已完成。 |
| `recommendations_status=running` 或 `run.phase=recommending` | “正在生成相关推荐” | 推荐卡或“已生成推荐”。 |
| `recommendations_status=completed` 且 `jump_cards` 非空 | 推荐卡；以 `jump_cards[].id` 作为稳定渲染 key | 在 `empty`、`unavailable`、`failed` 状态渲染空白推荐卡。 |
| `recommendations_status=empty` / `unavailable` / `failed` | 分别显示“本轮无匹配推荐”／“当前未配置推荐”／“推荐暂不可用，专家结论已保留” | 将推荐问题升级为整轮失败。 |
| `messages[]` 中 final 消息 `status=completed`，或收到终态 `done` | 最终正文；`done` 依 `persistence` 覆盖同一气泡并写入历史 | 为最终正文新建第二个助手气泡。 |
| `done_timeout` | 已有完整快照与“仍在处理中”；按 `persistence` 保存可续接记录 | 将运行中任务标记失败或本地推进任务。 |
| `done` 且 `outcome=cancelled` | 结束本地加载状态，并依 `should_discard_staged` 清理暂存 | 写入用户历史或继续显示未完成最终回复。 |

当快照不含某个可选字段时，前端应隐藏对应区域，不用旧帧残留内容补齐。断线重连、答案提交后的续流和重复帧都遵循同一规则：按 `seq` 去重后整体替换，不做本地状态机推断。

## 5. 问题组、部分回答与去重

首个追问立即创建活动单题组。活动组存在时到达的新问题进入一个后续 `queued` 组；该组激活后，再到达的问题只能进入新的后续组，不能插入或改变当前组。每次只展示活动组的 `active_request_id` 一张卡片；本组剩余题和后续组只显示进度摘要。

用户点击“提交”可只填写部分字段；未填字段记录在 `answer.skipped_fields`，不覆盖已确认事实。点击“全部跳过”则跳过当前卡片的全部字段。服务端原子完成：保存答案、写入事实、分发绑定工具结果、推进组内下一题或下一组、创建恢复作业和发布完整状态。

同一用户/事实字段/作用域对象/适用时间且输入约束兼容的问题共享答案。相同完整问题共用一张卡片和多个 binding；部分重叠表单只展示尚未被其他卡片承接的字段。回答或跳过后，服务端将结果注入所有相关专家/Skill 的工具结果；后续同一事实请求直接从事实或问题账本读取，不再次向用户提问。对象、时间、作用域或输入约束不同则不复用。

## 6. 回答与恢复

提交必须使用当前快照中的 `request_version`：

```http
POST /api/v3/sessions/{session_id}/input-requests/{request_id}/answers
Accept: text/event-stream
Content-Type: application/json
```

同一个 `submission_id` 和相同内容幂等；旧版本或非活动卡片返回 `409`。带 `Accept: text/event-stream` 的成功响应不是普通 JSON：它先持久化答案，再在**同一 HTTP 响应**中继续发送 `state → … → done_suspended/done_timeout/done`。前端和 BFF 不得本地改卡片状态，也不需要额外调用“恢复”接口。

## 7. 状态规则

| 对象 | 状态 |
| --- | --- |
| run | `queued → running ↔ waiting_for_user → completed/failed/cancelled`；仍有独立节点可运行时保持 `running` |
| agent / Skill | `queued → running → waiting_for_user/waiting_for_dependency → queued`，最终进入终态 |
| tool | 排队、运行、等待、重试和终态；结果失败不必然终止专家 |
| input group | `active → completed`；`queued → active → completed` |
| input request | `queued → pending → answered/skipped`；取消或失效后为 `expired` |
| message | `streaming → completed/interrupted/blocked` |

`outcome=partial` 是受限业务结论，不是技术错误。只有没有可运行节点、且活动组等待用户时才发送 `done_suspended`。

编排首成功返回 `consult` 后，服务端先发送 `state(reason=dispatch_completed)`，前端应立即显示已规划的专家列表和任务 brief；随后再根据 `nodes[]` 显示实际启动、Skill/工具和专家结论。`clarify` 或 `reject_out_of_scope` 不产生专家分派摘要。

## 8. 兼容与诊断

SSE v2 继续使用旧入口；`context_policy_version` 与传输版本独立。旧 Harness run 可用 V3 快照建立当前状态，不虚构历史对外增量。

内部诊断资产应带明确 “internal outbox” 标识。对外 BFF 联调样例和 cURL 测试以 [SSE_V3_CURL_TEST_PLAYBOOK.md](SSE_V3_CURL_TEST_PLAYBOOK.md) 为准。
