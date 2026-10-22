# Harness SSE v3 协议与联调索引

SSE v3 是 Harness 多 agent 的公开传输协议，与 `context_policy_version` 独立；SSE v2 保持原入口。服务端开关 `HAILIANG_SSE_V3=true`，前端开关 `VITE_HAILIANG_SSE_V3=true`。

## 当前公开协议

对外使用单 run 有限 SSE 流。除 `ping` 外，`state`、`done_suspended`、`done_timeout`、`done` 的每一帧都携带完整 `state`；最终正文在 `message.delta` 中增量输出，发生受控回退时用 `message.reset` 切换 revision，`done.state.result` 为唯一终态权威。`persistence` 是 BFF 保存对话历史的唯一指令：`state` 只暂存，`done_suspended`、`done_timeout` 保存可续接记录，`done` 按 outcome 保存最终记录或在取消时丢弃暂存。回答请求带 `Accept: text/event-stream` 后，同一 HTTP 响应会继续推送状态，无需单独恢复接口。

公开 `state` 包含：运行阶段、`dispatch`、`nodes`、`inputs`、`input_groups`、推荐与最终结果。问题队列由 `state.input_queue`、`state.inputs` 与 `state.input_groups` 表示；只允许 `active_request_id` 对应的 pending 卡片提交。相同事实身份且输入约束兼容的问题由服务端复用答案和绑定，不由前端推断。

`chat` 与 `answer_input_request` 的 `input.expert_team_id` 推荐传单元素字符串数组；服务端同时回显 `expert_team_id`、`expert_team_ids` 和 `team_orchestration_version="single_team_v1"`。这是多团队接口预留，当前编排头仍只支持一个团队；传多个团队返回 `422 MULTI_EXPERT_TEAMS_NOT_ENABLED`。

编排首返回 `consult` 后，第一帧 `state` 会携带 `state.dispatch`，前端应立即展示“已分派 N 位专家”和每个专家的 `brief`。随后按 `state.nodes[]` 渲染“专家 → Skill/工具”树；专家节点的 `result` 摘要包含 `conclusion`、`findings`、`limitations` 和 `unresolved`。`state.synthesis_status` 表示编排器汇总阶段，`state.recommendations_status` 表示推荐阶段。最终正文在 `done` 的非 cancelled outcome 下写入对话历史；同一任务始终使用 `assistant_message_id` 更新一个助手气泡。

| 文档 | 用途 |
| --- | --- |
| [SSE_V3_HARNESS_CONTRACT.md](SSE_V3_HARNESS_CONTRACT.md) | 接口、帧、状态、问题组和恢复的公开权威契约 |
| [SSE_V3_HARNESS_INTEGRATION_GUIDE.md](SSE_V3_HARNESS_INTEGRATION_GUIDE.md) | BFF/前端接入流程和 reducer 规则 |
| [SSE_V3_CURL_TEST_PLAYBOOK.md](SSE_V3_CURL_TEST_PLAYBOOK.md) | 启动、分组追问、回答、超时、终态的 cURL 联调 |
| [sse_v3/ACCEPTANCE.md](sse_v3/ACCEPTANCE.md) | 验收记录与已知限制 |

## 内部诊断资产

`sse_v3/event.schema.json`、`parallel-partial-stream.events.json` 与 `.sse` 是旧的**内部 outbox**重放资产，包含 `node.updated`、`message.delta` 等事件；它们用于存储投影/trace 回归，不能作为 BFF 或浏览器 wire 协议样例。对外有限流样例见 [sse_v3/public/grouped-input-run-stream.events.json](sse_v3/public/grouped-input-run-stream.events.json)，其校验约束见同目录的 `run-stream.schema.json`。

## 本地启动

```bash
export HAILIANG_SSE_V3=true
export VITE_HAILIANG_SSE_V3=true
export HAILIANG_SSE_V3_RUN_STREAM_SECONDS=600
./run.sh
```

BFF 本地参考：[examples/sse_v3/bff.py](../examples/sse_v3/bff.py)。生产 BFF 必须替换样例固定 Cookie 为真实登录态与档案授权。
