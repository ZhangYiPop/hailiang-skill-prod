# SSE v3 Harness 前端与 BFF 接入说明

字段权威定义见 [SSE_V3_HARNESS_CONTRACT.md](SSE_V3_HARNESS_CONTRACT.md)。本指南只说明接入方式。

## 1. 最小流程

```text
GET /harness（完整快照）
→ POST /harness/runs（202，得到 run_id）
→ GET /harness/runs/{run_id}/events（有限 SSE 流）
→ 每个 state/done_suspended/done_timeout/done 整体替换快照
→ pending 卡片 POST /answers + Accept: text/event-stream
→ 同一回答响应继续接收完整帧
→ done 后 GET /record 并按 run_id + record_version 落库
```

BFF 负责可信身份、档案授权和原样转发。它不得缓冲、重排、改写 SSE、关闭浏览器订阅时取消 run，或自动重试启动请求。浏览器只显示服务端快照，不推断任务推进。

## 2. 有限流规则

生产转发端使用单 run 流，而非会话级长连接。`state` 保持连接；`done_suspended`、`done_timeout`、`done` 结束本次连接。

- `done_suspended`：活动卡片正在等待用户且没有其他节点可继续；按 `persistence` 保存并显示当前卡片。
- `done_timeout`：后台任务仍在运行；按 `persistence` 保存“处理中”记录，用最后 SSE `id` 续连同一 run，不能显示为失败。
- `done`：终态；依照 `persistence` 落库。完成、受限完成和失败保存最终记录，已停止丢弃暂存。
- 回答成功：答案 POST 的响应本身就是下一段流，不需要先 GET 事件流。

## 3. 快照 reducer

每个有效帧携带完整 `snapshot`。客户端只做：校验 `protocol/session_id/profile_id`，丢弃 `seq <= cursor` 的重复帧，将状态替换为 `snapshot`。出现 `resync_required`、范围不匹配或解析失败时重新 GET `/harness`。

不要实现实体 upsert、delta 文本拼接、`message.delta` offset 校验或 `run.finalized` 消费；这些是内部 outbox 的行为，不是公开协议。

仓库参考：[frontend/src/utils/harnessV3.ts](../frontend/src/utils/harnessV3.ts)。

## 4. 任务气泡与问题组

渲染顺序固定为“任务拆解 → 主任务/专家/Skill/工具树 → 专家结论 → 汇总与推荐”。编排首完成后，先读取 `runs[].dispatch` 显示已分派的专家数量、业务 brief、具体 `task_brief`、并行关系和依赖；再读取 `nodes` 展示实际执行。节点可显示 `phase`、`action`、`waiting_on`、`outcome`、`result` 和安全错误码。`round_experts` 只用于快速汇总，不替代节点树。

主任务阶段使用 `runs[].phase` 和 `runs[].action`：`planning` 表示拆题，`dispatching_completed` 表示已经知道专家计划，`experts_running` 表示专家执行中，`synthesizing` 表示编排尾汇总，`recommending` 表示推荐生成。专家 `result` 只包含结论、依据、限制和未解决事项，不包含内部提示词或思考。

追问以 `runs[].input_queue` 与 `input_groups` 驱动：

- 只有 `active_request_id` 对应的 `pending` 卡片可编辑。
- 活动组显示“第 N/M 题”；同组其余题只显示进度。
- `queued_group_ids` 显示后续组的专家摘要与题数，不能展示输入控件。
- 收到完整帧后整体替换；前端不能本地把下一题/下一组设为 pending。
- 相同事实的跨专家复用由服务端 bindings、事实投影和工具结果分发处理；前端无需也不得做语义去重。

提交使用当前 `request_version`。普通“提交”允许部分填写，空白字段由服务端记为跳过；“全部跳过”只跳过当前卡片。409 后重新加载快照。

## 5. 新问题与终态记录

活动 run 中用户发送新问题时，服务端会终止旧 run 并创建新 run；旧 run 的已发布正文为 `interrupted`，其专家中间态不能带入新 run 的上下文。档案与专家切换由 `allowed_actions` 控制。

收到 `done` 后，BFF 读取 `/record`。保存 `user_message`、`assistant_message`、`execution`、`agent_summary`、`input_summary`；不要把公开任务树当作下一轮模型上下文。

## 6. 本地联调

```bash
export HAILIANG_SSE_V3=true
export VITE_HAILIANG_SSE_V3=true
export HAILIANG_SSE_V3_RUN_STREAM_SECONDS=600
./run.sh
```

可运行 BFF 参考为 [examples/sse_v3/bff.py](../examples/sse_v3/bff.py)。完整 cURL 场景见 [SSE_V3_CURL_TEST_PLAYBOOK.md](SSE_V3_CURL_TEST_PLAYBOOK.md)。
