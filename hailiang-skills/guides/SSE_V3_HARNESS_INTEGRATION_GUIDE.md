# SSE v3 Harness 前端与 BFF 接入说明

字段权威定义见 [SSE_V3_HARNESS_CONTRACT.md](SSE_V3_HARNESS_CONTRACT.md)。本指南只说明接入方式。

## 1. 产品最小流程

```text
前端 POST BFF /api/v3/sessions/chat/stream
  body: session_id + run_id + input(JSON 字符串)
→ BFF 校验登录态和孩子归属，注入 context_data
→ BFF POST 算法服务同路径，逐字节转发有限 SSE
→ 每个 state/done_suspended/done_timeout/done 整体替换同一 assistant_message_id 气泡
→ pending 卡片仍 POST BFF 同一路径；其响应主动继续推流
```

前端不得调用 `/harness/*`、`/events`、`/answers` 或 `/record`。这些细粒度接口只供算法工作台和排障使用。BFF 负责可信身份、档案授权和原样转发；它不得缓冲、重排、改写 SSE、关闭浏览器订阅时取消 run，或自动重试新的聊天请求。

浏览器 body 中只允许 `session_id`、`run_id`、`input`。`user_id`、`profile_id`、`profile_data` 必须由 BFF 注入；专家成员、Skill、任务树、卡片版本和恢复指令都不是前端参数。具体 cURL 见 [SSE_V3_CURL_TEST_PLAYBOOK.md](SSE_V3_CURL_TEST_PLAYBOOK.md)。

## 2. 有限流规则

生产转发端使用单 run 流，而非会话级长连接。`state` 保持连接；`done_suspended`、`done_timeout`、`done` 结束本次连接。

- `done_suspended`：活动卡片正在等待用户且没有其他节点可继续；按 `persistence` 保存并显示当前卡片。
- `done_timeout`：后台任务仍在运行；按 `persistence` 保存“处理中”记录。用户返回 App 后，BFF 用已保存的相同 `session_id`、`run_id` 和原始 `chat` input 重发同一最小入口，服务端附着既有 run，不能显示为失败或创建新任务。
- `done`：终态；依照 `persistence` 落库。完成、受限完成和失败保存最终记录，已停止丢弃暂存。
- 回答成功：答案 POST 的响应本身就是下一段流，不需要先 GET 事件流。

## 3. 快照 reducer

每个有效帧携带完整 `snapshot`。客户端只做：校验 `protocol/session_id/profile_id`，丢弃 `seq <= cursor` 的重复帧，将状态替换为 `snapshot`。出现 `resync_required`、范围不匹配或解析失败时保留最近可用快照；下次 BFF 重发同一最小 `chat` 请求即可取得服务端当前完整状态，不调用 `/harness`。

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

提交不传 `request_version` 或 `submission_id`。普通“提交”允许部分填写，空白字段由服务端记为跳过；“全部跳过”只跳过当前卡片。409 后由 BFF 返回当前错误，前端保留最近完整快照。

## 5. 新问题与终态记录

活动 run 中用户发送新问题时，服务端会终止旧 run 并创建新 run；旧 run 的已发布正文为 `interrupted`，其专家中间态不能带入新 run 的上下文。档案切换由 BFF 的已授权档案上下文控制；专家团队只能由当前 `input.expert_team_id` 指定，前端不选择团队成员。

收到 `done` 后，BFF 依照该帧的 `persistence` 与 `record_version` 覆盖同一 `assistant_message_id` 的历史记录；本期不要求 BFF 调用 `/record` 或 `/harness` 拉取快照。不要把公开任务树当作下一轮模型上下文。

## 6. 本地联调

```bash
export HAILIANG_SSE_V3=true
export VITE_HAILIANG_SSE_V3=true
export HAILIANG_SSE_V3_RUN_STREAM_SECONDS=600
./run.sh
```

可运行 BFF 参考为 [examples/sse_v3/bff.py](../examples/sse_v3/bff.py)。完整 cURL 场景见 [SSE_V3_CURL_TEST_PLAYBOOK.md](SSE_V3_CURL_TEST_PLAYBOOK.md)。
