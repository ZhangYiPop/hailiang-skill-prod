# SSE v3 Harness 前端与 BFF 接入说明

> **当前对外消费模型（2026-10-19）**：V3 已改为 V2 风格的完整状态帧。接入方只消费 `state`、`suspended`、`timeout`、`done` 与 `ping`；除 `ping` 外每帧 `data` 都是完整公开状态骨架，前端整体替换，不消费 `node.updated`、`message.delta` 或 `run.finalized`。`done` 是唯一可落库终态信号；`suspended` 与 `timeout` 携带 `resume_after`，用于续连同一个 run。本文后续若与本说明冲突，以本说明为准，详细章节将在同一协议版本内继续收敛。
> **回答恢复**：`POST /input-requests/{request_id}/answers` 带 `Accept: text/event-stream` 时，服务端先原子保存答案和恢复作业，再将该 HTTP 响应作为同一 run 的有限 SSE 流返回。BFF 直接透传，前端无需在回答成功后手工请求 run 事件。

> 面向业务前端与转发服务。字段级权威定义见
> [SSE_V3_HARNESS_CONTRACT.md](SSE_V3_HARNESS_CONTRACT.md)。

## 1. 接入方只需实现的主流程

```text
能力发现
  → GET 快照
  → 从 snapshot.cursor 订阅事件
  → POST 启动 run
  → reducer 按 seq/version 更新实体
  → pending 卡片提交答案或全部跳过
  → 主 agent message.delta 展示正文
  → run/message 进入终态
```

V3 不是把 V2 的完整 `state` 换一个版本号。前端需要维护四个实体集合：
`runs`、`nodes`、`input_requests`、`messages`。普通更新只 upsert 一个实体，不要在每个事件后重新拉全量状态。

## 2. 前端与 BFF 的职责

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| 浏览器前端 | 生成稳定提交 ID、渲染实体树和卡片、消费 reducer、触发重同步 | 声明可信用户、推断下一张卡片、判断工具权限 |
| BFF | 校验登录态和档案权限、注入身份、原样转发 JSON/SSE、透传游标 | 缓冲/重排 SSE、改写正文、断线时取消 run、自动重试启动 |
| Harness 服务 | 调度、状态机、卡片队列、幂等、公开投影、事件顺序 | 向普通流公开内部提示词和推理轨迹 |

浏览器建议只访问同源 BFF。仓库中的可运行参考为
[`../examples/sse_v3/bff.py`](../examples/sse_v3/bff.py)，其固定 Cookie 仅用于本地联调，生产必须替换为真实认证。

不支持会话级长连接的转发端使用单 run 有限流：

```text
POST /harness/runs
→ GET /harness/runs/{run_id}/events
→ run.finalized / run.suspended / stream.timeout
→ 当前 HTTP 连接关闭
```

默认等待上限为 290 秒，给 5 分钟网关留出关闭余量。`run.suspended` 后等待用户提交答案，再从
`resume_after` 重新连接；`stream.timeout` 后任务继续在后台执行，可立即续传。有限流消费者不得使用
会话级 reducer 的“全局 seq 必须逐一连续”规则，因为服务端会过滤掉其他 run 的事件。

## 3. 前端启动顺序

1. 调用 `/harness/capabilities`，确认包含 `hailiang.sse.v3`。
2. 请求 `/harness`，以响应整体替换本地 V3 状态。
3. 使用快照 `cursor` 作为 `after` 创建 EventSource。
4. 收到事件后只通过统一 reducer 更新状态；把 `run.finalized` 作为该轮可以落库的信号。
5. 连接断开只显示“正在重连”，不要把 run 标成失败。

仓库已有参考实现：

- [`../frontend/src/utils/harnessV3.ts`](../frontend/src/utils/harnessV3.ts)：类型、reducer、重同步订阅器。
- [`../frontend/src/hooks/useHarnessConversation.ts`](../frontend/src/hooks/useHarnessConversation.ts)：业务对话接入。
- [`../frontend/src/components/workbench/HarnessDebugPanel.tsx`](../frontend/src/components/workbench/HarnessDebugPanel.tsx)：调试台接入。

## 4. 启动任务与安全重试

用户点击发送时生成一次 `submission_id`，在该次请求结果未知的所有重试中复用它：

```ts
const submissionId = crypto.randomUUID();
await fetch(`/api/v3/sessions/${sessionId}/harness/runs`, {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({submission_id: submissionId, goal, context_data}),
});
```

只有用户明确发起一个新任务，才生成新的提交 ID。收到 `202` 后无需轮询 run 详情，实体会通过 SSE 到达；
请求超时可用相同提交 ID 重发。

任务非终态时，UI 依据 `run.allowed_actions` 控制动作。V3 的活动 run 仍允许 `start`：用户发送新消息时，
服务端会中止旧轮并启动新轮。档案和专家切换保持禁用，追问回答和取消仍可使用。发送按钮应明确提示
“发送并切换到新问题”，避免用户误以为内容会追加到正在运行的旧任务。

## 5. reducer 规则

每个订阅范围只维护一个状态对象：

```ts
type State = {
  protocol: 'hailiang.sse.v3';
  session_id: string;
  profile_id: string | null;
  cursor: number;
  runs: Entity[];
  nodes: Entity[];
  input_requests: Entity[];
  messages: Entity[];
};
```

处理顺序：

1. 校验 `protocol/session_id/profile_id`。
2. `snapshot` 整体替换状态。
3. `seq <= cursor`：忽略重复帧。
4. `seq !== cursor + 1`：触发重同步。
5. `*.updated`：按 `entity_id` 和 `entity_version` upsert 完整实体。
6. `message.delta`：校验 revision 和 Unicode 码点 offset 后追加。
7. `resync_required` 或任意缺片：拉快照恢复。

不要把 SSE TCP 分片当作事件边界；应使用 EventSource 或符合 SSE 规范的解析器。中文和 emoji 可能横跨网络字节分片。

## 6. UI 投影

推荐把一个 run 渲染成一个持续更新的工作气泡：

```text
主任务 run
├─ 主 agent 节点
├─ 专家 A
│  ├─ Skill A1
│  └─ 工具调用
├─ 专家 B
└─ 最终消息
```

- `nodes.parent_id` 构建层级；同级节点按服务端时间或稳定 ID 排序。
- 显示 `actor/title/status/outcome/attempts/error`，不展示内部 prompt 或原始工具参数。
- run 的 `completed_count/task_count/waiting_count/elapsed_ms` 直接使用服务端值，不计算虚构百分比或 ETA。
- `outcome=partial` 表示形成了受限业务结论，不应渲染成系统崩溃。
- 工具节点 `completed` 后，专家节点仍可能是 `running`。

## 7. 追问卡片

只渲染 `input_request.status=pending` 的卡片为可编辑状态；`queued` 卡片可显示“等待回答”，但不能提交。
卡片标题要显示 `actor`，让用户知道是哪位专家在追问。同一专家的后续 `round` 由服务端在上一轮完成后发布。

提交按钮语义为“提交已填写内容”：

```json
{
  "user_id": "user_001",
  "profile_id": "profile_001",
  "submission_id": "answer-7d36",
  "expected_version": 3,
  "answers": {
    "province": "浙江"
  },
  "skipped": false
}
```

未填写字段不需要前端枚举，服务端会写入 `skipped_fields`。全部跳过按钮发送：

```json
{
  "user_id": "user_001",
  "profile_id": "profile_001",
  "submission_id": "answer-skip-7d36",
  "expected_version": 3,
  "answers": {},
  "skipped": true
}
```

提交后不要本地把卡片直接改成 answered，也不要唤醒任务；等待 `input_request.updated` 和后续节点事件。
请求超时重试复用相同 `submission_id`。409 时拉快照，旧卡通常已经回答、跳过或失效。

## 8. 主 agent 正文

最终消息 ID 稳定为 run 对应的 final 消息。`message.updated` 提供完整正文，`message.delta` 提供低延迟追加。

```ts
function appendByCodePoint(content: string, delta: Delta) {
  if (Array.from(content).length !== delta.offset) throw new Error('V3_TEXT_GAP');
  return content + delta.text;
}
```

- `streaming`：显示正在生成。
- `completed`：正常结束。
- `interrupted`：用户取消或生成被中断，保留已发布内容。
- `blocked`：清空旧草稿，显示通用提示。

新 revision 到达时，以完整 `message.updated` 替换旧草稿；不能把两个 revision 拼接。

## 9. 重连与刷新

推荐统一恢复函数：

```text
关闭旧 EventSource
→ GET /harness
→ 整体替换本地状态
→ GET /harness/events?after=snapshot.cursor
```

网络暂时断开时可先让 EventSource 自行重连；收到 `resync_required`、发现 `seq` 断档、正文 offset 不连续，
或切换档案时必须执行完整恢复。刷新页面不会取消后台 run。

## 10. BFF 转发要求

JSON 接口：

- 从会话登录态取得 `user_id`；校验并注入/覆盖 `profile_id`。
- 保留浏览器的 `submission_id`，不要替换。
- 原样透传上游状态码与稳定错误码。
- 对有副作用的 POST 做同源/CSRF 防护。

SSE 接口：

- 透传 `Last-Event-ID`，并允许 `after`。
- 使用流式字节转发，禁用代理缓冲与响应压缩。
- 不拆包、合帧、重排、翻译事件名或修改 `data`。
- 浏览器断开时只关闭上游连接，不调用 cancel。
- 不把心跳注释转换成业务事件。

生产反向代理需设置 `X-Accel-Buffering: no` 或等价配置，并确保连接空闲超时大于心跳间隔。

## 11. 联调检查清单

- 同一启动请求重试不会产生两个 run。
- 两个专家并行更新时节点不互相覆盖。
- 多个专家追问只出现一张 `pending` 卡片，其余保持 `queued`。
- 部分填写后，已填字段保存，空白字段显示于 `skipped_fields`。
- 全部跳过后，只恢复绑定节点并推进下一张卡片。
- 重复事件不重复追加正文。
- 丢失一帧能检测并通过快照恢复。
- 中文和 emoji 在逐字节代理分片下不乱码。
- 取消后消息为 `interrupted`，迟到事件不恢复 run。
- 页面刷新、SSE 断线和 worker 重启后状态不倒退。
- 跨用户或跨档案请求被拒绝。

可直接使用以下资产：

- [SSE v3 cURL 联调手册](SSE_V3_CURL_TEST_PLAYBOOK.md)：逐状态直连与 BFF 验证命令。
- [`sse_v3/parallel-partial-stream.sse`](sse_v3/parallel-partial-stream.sse)：完整 SSE 流。
- [`sse_v3/parallel-partial-stream.events.json`](sse_v3/parallel-partial-stream.events.json)：等价 JSON 事件序列。
- [`sse_v3/snapshot.json`](sse_v3/snapshot.json)：最终快照。
- [`sse_v3/ACCEPTANCE.md`](sse_v3/ACCEPTANCE.md)：当前验收状态和限制。

## 12. 本地启用

```bash
export HAILIANG_SSE_V3=true
export VITE_HAILIANG_SSE_V3=true
./run.sh
```

两个开关都应在进程启动前设置。默认仍关闭 V3；真实 BFF 接入和模型流式质量需在灰度环境验证。

