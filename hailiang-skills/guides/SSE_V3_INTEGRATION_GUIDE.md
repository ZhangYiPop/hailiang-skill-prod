# Harness SSE v3 协议与联调索引

SSE v3 是 Harness 多 agent 的公开传输协议，与 `context_policy_version` 独立；SSE v2 保持原入口。服务端开关 `HAILIANG_SSE_V3=true`，前端开关 `VITE_HAILIANG_SSE_V3=true`。

## 当前公开协议

对外使用单 run 有限 SSE 流。除 `ping` 外，`state`、`suspended`、`timeout`、`done` 的每一帧都携带完整 `snapshot`；前端整体替换状态。`done` 是唯一可写入对话历史的终态信号。回答请求带 `Accept: text/event-stream` 后，同一 HTTP 响应会继续推送状态，无需单独恢复接口。

公开快照包含：`runs`、`nodes`、`input_requests`、`input_groups`、`messages`。问题队列由 `run.input_queue` 和 `input_groups` 表示；只允许 `active_request_id` 对应的 pending 卡片提交。相同事实身份且输入约束兼容的问题由服务端复用答案和绑定，不由前端推断。

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
export HAILIANG_SSE_V3_RUN_STREAM_SECONDS=290
./run.sh
```

BFF 本地参考：[examples/sse_v3/bff.py](../examples/sse_v3/bff.py)。生产 BFF 必须替换样例固定 Cookie 为真实登录态与档案授权。
