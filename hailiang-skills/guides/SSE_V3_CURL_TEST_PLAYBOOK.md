# SSE v3：前端与转发端 cURL 联调手册

> 对外测试入口：`POST {BFF_BASE}/api/v3/sessions/chat/stream`
>
> 本文只描述 **前端 → BFF** 的包装接口。BFF 再向算法服务的转发、Harness 的 `/harness/*` 调试接口和数据库查询都不属于前端联调；不要把它们复制到 App。

## 1. 三层职责与请求形状

```text
App / 前端                    BFF                         算法服务
POST /api/v3/sessions/  →    注入可信身份与档案      →    POST /api/v3/sessions/
chat/stream                   context_data                    chat/stream

仅传 session_id、run_id、       不改写 SSE 帧；                  编排、专家、表单、
input                          原样转发完整快照                恢复和汇总
```

前端请求只有三个字段：

```json
{
  "session_id": "session_xxx",
  "run_id": "client_run_xxx",
  "input": "{\"action\":\"chat\",\"content\":\"帮我规划升学路径\",\"expert_team_id\":\"student_growth_expert_team\"}"
}
```

- `session_id`：BFF 创建或复用的会话 ID。
- `run_id`：BFF 创建的客户端幂等键。一次新问题使用一个新值；`done_timeout` 后续接同一任务时，必须复用原值。
- `input`：JSON **字符串**。每种动作都必须带 `expert_team_id`，团队内专家、Skill、任务树和表单版本均不由前端传入。

前端**不得**传 `user_id`、`profile_id` 或 `profile_data`。BFF 从登录态及档案服务取得它们，组装后才向算法服务发送：

```json
{
  "context_data": {
    "user_id": "可信登录用户",
    "profile_id": "已授权的孩子档案",
    "profile_data": {"grade": "高一", "province": "浙江"}
  }
}
```

本地 BFF 参考实现为 [examples/sse_v3/bff.py](../examples/sse_v3/bff.py)：它也会丢弃前端伪造的 `context_data`。

## 2. 本地准备

先启动算法服务，再启动本地 BFF 参考。生产联调时只需把 `BFF_BASE` 换成实际 BFF 域名。

```bash
# 终端 1：算法服务
cd /Users/ayi/Project/hailiang-skill_sensitive_v02121/hailiang-skills
export HAILIANG_SSE_V3=true
export HAILIANG_HARNESS_SSE_WAIT_SECONDS=600
./run.sh

# 终端 2：本地 BFF。真实 BFF 应从登录态和档案服务取值。
cd /Users/ayi/Project/hailiang-skill_sensitive_v02121/hailiang-skills
export BFF_UPSTREAM="http://127.0.0.1:8016"
export BFF_DEMO_COOKIE="local-only"
export BFF_DEMO_USER="user-v3-curl"
export BFF_DEMO_PROFILE="profile-v3-curl"
export BFF_DEMO_PROFILE_DATA='{"name":"小海","grade":"高一","province":"浙江"}'
./.venv/bin/uvicorn examples.sse_v3.bff:app --port 8090
```

以下命令只调用 BFF：

```bash
export BFF_BASE="http://127.0.0.1:8090"
export SESSION_ID="session-v3-$(date +%s)"
export CLIENT_RUN_ID="client-v3-$(date +%s)"
export EXPERT_TEAM_ID="student_growth_expert_team"
export BFF_COOKIE="local-only"
```

`Origin` 仅适用于使用 Cookie 的 Web BFF；原生 App 由 BFF 的 Token／签名认证策略处理，不应伪造 Web 同源语义。仓库的本地 BFF 示例采用 Cookie，因此 cURL 需要模拟同源请求。`-N` 禁用 cURL 缓冲，`--max-time 610` 给服务端 600 秒有限流留下余量。

## 3. 场景一：发送新问题，展示拆分后的专家树

```bash
cat >/tmp/v3-bff-chat.json <<JSON
{
  "session_id":"$SESSION_ID",
  "run_id":"$CLIENT_RUN_ID",
  "input":"{\"action\":\"chat\",\"content\":\"请为浙江高一学生规划选科、升学和职业探索。\",\"expert_team_id\":\"$EXPERT_TEAM_ID\"}"
}
JSON

curl -sS -N --max-time 610 -X POST "$BFF_BASE/api/v3/sessions/chat/stream" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "Origin: $BFF_BASE" \
  -H "Cookie: demo_session=$BFF_COOKIE" \
  --data-binary @/tmp/v3-bff-chat.json | tee /tmp/v3-bff-chat.sse
```

BFF 原样转发的每个业务帧都包含完整 `data.snapshot`。前端收到
`event: state` 且 `reason: "dispatch_completed"` 后立刻展示：

- `snapshot.runs[0].dispatch`：已规划专家数、并行任务数、每位专家的业务 `brief`、`task_brief` 与依赖；
- `dispatch.experts[].node_id`：该专家实际节点的 ID；
- `snapshot.nodes`：按 `parent_id` 递归得到“专家 → Skill／工具”执行树；
- 专家节点的 `phase`、`action`、`waiting_on` 和 `result`：此刻正在做什么、在等谁和已提交结论。

同一 `assistant_message_id` 的任务拆分、专家状态、表单、专家结论、汇总、推荐和最终正文都更新**同一个**助手气泡，不能在最终正文时创建第二个气泡。

## 4. 场景二：活动表单的部分回答

当帧中出现 `snapshot.runs[0].input_queue.active_request_id` 且对应
`input_requests[]` 的状态为 `pending`，它是当前唯一可填写的卡片。把该 ID 写入：

```bash
export REQUEST_ID="input_req_from_latest_snapshot"
cat >/tmp/v3-bff-answer.json <<JSON
{
  "session_id":"$SESSION_ID",
  "run_id":"$CLIENT_RUN_ID",
  "input":"{\"action\":\"answer_input_request\",\"request_id\":\"$REQUEST_ID\",\"answers\":{\"province\":\"浙江\"},\"text\":\"孩子目前高一\",\"skipped\":false,\"expert_team_id\":\"$EXPERT_TEAM_ID\"}"
}
JSON

curl -sS -N --max-time 610 -X POST "$BFF_BASE/api/v3/sessions/chat/stream" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "Origin: $BFF_BASE" \
  -H "Cookie: demo_session=$BFF_COOKIE" \
  --data-binary @/tmp/v3-bff-answer.json | tee /tmp/v3-bff-answer.sse
```

普通“提交”允许部分填写：空白字段由服务端标记为跳过，不覆盖已有事实。服务端会复用相同事实身份的答案并恢复所有已绑定专家；前端只应用随此响应主动返回的完整帧，不能自行推进下一张卡片或下一组。

## 5. 场景三：跳过当前卡片

```bash
cat >/tmp/v3-bff-skip.json <<JSON
{
  "session_id":"$SESSION_ID",
  "run_id":"$CLIENT_RUN_ID",
  "input":"{\"action\":\"answer_input_request\",\"request_id\":\"$REQUEST_ID\",\"answers\":{},\"skipped\":true,\"expert_team_id\":\"$EXPERT_TEAM_ID\"}"
}
JSON

curl -sS -N --max-time 610 -X POST "$BFF_BASE/api/v3/sessions/chat/stream" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "Origin: $BFF_BASE" \
  -H "Cookie: demo_session=$BFF_COOKIE" \
  --data-binary @/tmp/v3-bff-skip.json
```

`skipped:true` 只跳过当前卡片。活动组仍有其他题时，下一张卡片由服务端激活；活动组结束后才切到下一组。

## 6. 场景四：收到等待、超时或完成帧

| 最后一帧 | 意义 | BFF 保存动作 | 前端动作 |
| --- | --- | --- | --- |
| `done_suspended` | 没有节点可继续，活动卡片等待用户 | 按 `persistence` 覆盖同一助手气泡的暂存历史，`record_kind=waiting_for_user` | 直接显示该快照的活动卡片。用户操作后调用场景二或三。 |
| `done_timeout` | 600 秒有限流结束，但后台任务仍在运行 | 覆盖同一助手气泡，`record_kind=running` | 显示“仍在处理中”。App 回来后用场景一的**相同请求**（同一 `run_id`、同一 `input`）重新连接；不得新建任务。 |
| `done` 且 `outcome=completed/partial/failed` | 任务终态 | 以 `assistant_message_id + record_version` 覆盖并保存最终历史 | 显示最终正文、专家结论摘要与推荐；不再恢复。 |
| `done` 且 `outcome=cancelled` | 用户已停止 | 根据 `persistence.should_discard_staged=true` 丢弃暂存 | 沿用现有停止交互，不存该任务的历史。 |

`state` 仅更新暂存快照。BFF 必须以帧中的 `persistence` 决定是否保存，不能仅凭 HTTP 连接关闭或事件名称自行猜测。

## 7. 场景五：用户在运行中发送新问题

新的用户问题必须使用新的 `run_id`；服务端先停止旧任务，再创建新的单气泡任务。旧任务的中间分析、工具结果和未完成正文不进入新任务上下文。

```bash
export NEXT_CLIENT_RUN_ID="client-v3-next-$(date +%s)"
curl -sS -N --max-time 610 -X POST "$BFF_BASE/api/v3/sessions/chat/stream" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "Origin: $BFF_BASE" \
  -H "Cookie: demo_session=$BFF_COOKIE" \
  --data-raw "{\"session_id\":\"$SESSION_ID\",\"run_id\":\"$NEXT_CLIENT_RUN_ID\",\"input\":\"{\\\"action\\\":\\\"chat\\\",\\\"content\\\":\\\"改为先帮我判断孩子是否适合竞赛路线。\\\",\\\"expert_team_id\\\":\\\"$EXPERT_TEAM_ID\\\"}\"}"
```

## 8. 场景六：用户停止任务

停止同样只经过 BFF，使用当前任务的 `run_id` 与相同专家团：

```bash
curl -sS -N --max-time 30 -X POST "$BFF_BASE/api/v3/sessions/chat/stream" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -H "Origin: $BFF_BASE" \
  -H "Cookie: demo_session=$BFF_COOKIE" \
  --data-raw "{\"session_id\":\"$SESSION_ID\",\"run_id\":\"$CLIENT_RUN_ID\",\"input\":\"{\\\"action\\\":\\\"stop\\\",\\\"expert_team_id\\\":\\\"$EXPERT_TEAM_ID\\\"}\"}"
```

预期最后一帧为 `event: done`、`outcome: "cancelled"`，且 `persistence.should_discard_staged=true`。

## 9. BFF 实现检查表

- 前端只访问 BFF；算法服务地址不得暴露给 App。
- BFF 从 Cookie／Token 注入 `user_id`，校验并注入 `profile_id`，按需读取档案并注入 `profile_data`。
- BFF 丢弃前端提供的 `context_data`、专家、Skill、节点、版本和恢复字段。
- BFF 逐字节、低延迟转发 SSE，不缓冲、聚合、重排或改写帧。
- BFF 不因前端连接断开而取消上游 run，不自动重试新的 `chat`。
- BFF 将每帧完整 `snapshot` 暂存到 `assistant_message_id`；只按 `persistence` 写入或清理对话历史。

完整字段、状态树与保存语义见 [SSE_V3_HARNESS_CONTRACT.md](SSE_V3_HARNESS_CONTRACT.md)；算法服务的细粒度 Harness 调试接口仅供工作台和排障使用，不属于此手册。
