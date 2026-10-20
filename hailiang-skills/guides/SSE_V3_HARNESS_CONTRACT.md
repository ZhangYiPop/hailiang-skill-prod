# SSE v3：Harness 多 Agent 公开协议

协议版本：`hailiang.sse.v3`。本文件是业务前端与 BFF 的唯一公开契约；细粒度 outbox、模型 trace、原始工具参数和内部事件不属于本协议。

## 1. 消费模型

SSE v3 使用 V2 风格的**完整状态帧**。除 `ping` 外，所有帧的 `data.snapshot` 都是当时完整、可独立渲染的公开快照。客户端校验范围和游标后整体替换本地状态，不能自行推进任务、卡片或正文。

| event | 语义 | 连接行为 |
| --- | --- | --- |
| `state` | 任一公开状态改变 | 保持连接 |
| `suspended` | 无可运行节点且活动问题组等待用户 | 关闭本次有限流 |
| `timeout` | 有限流达到等待边界，run 仍在后台运行 | 关闭本次有限流 |
| `done` | run 已终态，公开记录已可落库 | 关闭本次有限流 |
| `ping` | SSE 注释保活 | 忽略 |

`node.updated`、`input_request.updated`、`message.delta`、`run.finalized` 仅存在于内部 outbox/诊断接口；BFF 和浏览器不得订阅或依赖它们。

## 2. 接口

所有接口的完整前缀为：`/api/v3/sessions/{session_id}`。资源标识放在 URL 路径；POST 的业务数据放在 JSON 请求体；读取身份、档案和游标放在 query 参数。POST 参数不应全部拼到 URL 中。

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

`done` 额外含 `record_version`、`record_url` 和最终消息版本。转发端以 `run_id + record_version` 幂等保存历史消息。`suspended` 和 `timeout` 只是传输边界，绝不表示任务失败。

## 4. 公开快照

`snapshot` 至少包含 `runs`、`nodes`、`input_requests`、`input_groups`、`messages` 和 `cursor`。

- `runs[].input_queue`：`active_group_id`、`queued_group_ids`、`active_request_id`、活动组总题数/完成数及旧兼容计数。
- `runs[].round_experts`：当前轮专家 ID 的 `assigned/running/completed/failed` 列表与汇总状态；同一专家只保留最近任务状态。
- `runs[].jump_cards`：服务端校验过的推荐卡片，仅含 intent、对象类型、筛选条件、标题、说明和推荐原因；没有推荐时为 `[]`。
- `runs[].dispatch`：编排首完成后的任务分派摘要，包含计划任务数、专家数、可并行数、专家名称、业务人员编写的专家 `brief`、本次 `task_brief` 和依赖。`brief` 来自已发布专家定义，`task_brief` 来自本轮编排计划。它表示“计划调用”，不代表节点已经运行。
- `runs[].phase`、`runs[].action`：当前主任务阶段和面向用户的动作说明。典型顺序为 `planning → dispatching_completed → experts_running → experts_completed → synthesizing → recommending → completed`。
- `runs[].synthesis_status`、`runs[].recommendations_status`：汇总和推荐的独立状态；专家完成不等于最终回复完成。
- `input_groups[]`：稳定组 ID、`active | queued | completed`、创建时间、题目 ID、当前题、题数、完成数和专家摘要。
- `input_requests[]`：单题字段、`request_version`、状态、回答、绑定、专家和所属组。只有 `input_queue.active_request_id` 对应且状态为 `pending` 的卡片可填写。
- `nodes[]`：主任务、专家、Skill 和工具树。专家、Skill 与其工具节点含稳定 `expert_id`；`actor` 仅为展示名称。工具完成不等于所属专家完成。节点还可包含 `brief`、`phase`、`action`、`waiting_on`、子节点计数和受控的 `result` 摘要（`conclusion`、`findings`、`limitations`、`unresolved`），用于展示任务书、当前动作、依赖和专家结论。
- `messages[]`：阶段说明与主 agent 最终正文；前端直接使用 final 消息当前完整 `content`。

公开数据不含专家内部推理、完整提示词、原始工具参数、完整事实库或 trace。

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

同一个 `submission_id` 和相同内容幂等；旧版本或非活动卡片返回 `409`。带 `Accept: text/event-stream` 的成功响应不是普通 JSON：它先持久化答案，再在**同一 HTTP 响应**中继续发送 `state → … → suspended/timeout/done`。前端和 BFF 不得本地改卡片状态，也不需要额外调用“恢复”接口。

## 7. 状态规则

| 对象 | 状态 |
| --- | --- |
| run | `queued → running ↔ waiting_for_user → completed/failed/cancelled`；仍有独立节点可运行时保持 `running` |
| agent / Skill | `queued → running → waiting_for_user/waiting_for_dependency → queued`，最终进入终态 |
| tool | 排队、运行、等待、重试和终态；结果失败不必然终止专家 |
| input group | `active → completed`；`queued → active → completed` |
| input request | `queued → pending → answered/skipped`；取消或失效后为 `expired` |
| message | `streaming → completed/interrupted/blocked` |

`outcome=partial` 是受限业务结论，不是技术错误。只有没有可运行节点、且活动组等待用户时才发送 `suspended`。

编排首成功返回 `consult` 后，服务端先发送 `state(reason=dispatch_completed)`，前端应立即显示已规划的专家列表和任务 brief；随后再根据 `nodes[]` 显示实际启动、Skill/工具和专家结论。`clarify` 或 `reject_out_of_scope` 不产生专家分派摘要。

## 8. 兼容与诊断

SSE v2 继续使用旧入口；`context_policy_version` 与传输版本独立。旧 Harness run 可用 V3 快照建立当前状态，不虚构历史对外增量。

内部诊断资产应带明确 “internal outbox” 标识。对外 BFF 联调样例和 cURL 测试以 [SSE_V3_CURL_TEST_PLAYBOOK.md](SSE_V3_CURL_TEST_PLAYBOOK.md) 为准。
