# Harness SSE v3 协议与联调草案

> 接入文档导航：字段、状态和恢复的权威约束见
> [SSE_V3_HARNESS_CONTRACT.md](SSE_V3_HARNESS_CONTRACT.md)；前端与 BFF 的实现步骤见
> [SSE_V3_HARNESS_INTEGRATION_GUIDE.md](SSE_V3_HARNESS_INTEGRATION_GUIDE.md)。本文保留为设计总览和联调资产索引。

协议 `hailiang.sse.v3` 与上下文策略 v2 独立。保留旧 SSE v2。服务开关 `HAILIANG_SSE_V3=true`；前端 `VITE_HAILIANG_SSE_V3=true`，默认均关闭。

## 接口
前缀 `/api/v3/sessions/{session_id}`：POST `/harness/runs` 启动（202），GET `/harness` 快照，GET `/harness/events` 订阅，POST `/input-requests/{id}/answers` 回答，POST `/harness/runs/{id}/cancel` 取消。
启动体沿用 v2 Harness，增加必填 submission_id。同一身份、会话、提交 ID 的相同请求返回原 run，不同内容 409。回答接口保留 submission_id/expected_version/answers/skipped；空白字段由服务端归为本次跳过。已填写类型错误仍拒绝。

## 状态表
|实体|转换|
|---|---|
|run|queued → running ↔ waiting_for_user → completed/failed/cancelled|
|agent/Skill|queued → running → waiting_for_dependency/waiting_for_user → queued，最终 completed/failed/cancelled|
|tool|queued/running/waiting_for_dependency/waiting_for_user/completed/failed/skipped/cancelled|
|input_request|queued → pending → answered/skipped；关闭后 expired|
|message|streaming → completed/interrupted/blocked|
执行 status 与 outcome 分开，partial 不是技术失败。独立节点仍可运行时 run 保持 running。用户部分提交后卡片 answered，answer.skipped_fields 明示跳过字段；任务按实际绑定恢复。

## 事件与恢复
统一事件信封包含 protocol/event_id/seq/session_id/profile_id/run_id/type/entity_id/entity_version/timestamp/data。SSE event 与 type 相同，id 为 seq。实体更新为完整 upsert；快照包含 runs/nodes/input_requests/messages/cursor。订阅范围是用户、会话、档案；切档必须清空游标。版本用于单实体去重，seq 用于范围重放。
无游标先发 snapshot；有 after 或 Last-Event-ID 时取较大有效值。越界游标发送 resync_required 并关闭，客户端重新获取快照。心跳是注释；没有代表会话流关闭的 done。每个 run 收敛后发布一次 run.finalized，供 BFF 获取不可变记录并落库；断线不取消任务。首版事件不自动清理，后续清理需维护最早可恢复游标。
message.delta 的 revision/offset/text 中 offset 按 Unicode 码点计数（JS 用 Array.from）。重复片段忽略，缺片重同步。message.updated 可完整替换文本。重启重新生成时 revision 增加，不能拼接两次输出。只有主 agent 最终正文流式公开，所有片段经风控后发布，最终再检查整文。

## BFF
身份从可信登录态注入；禁止相信浏览器提交的 user_id。样例仅示范本地固定身份会话，不能直接充当生产认证。逐字节转发 SSE，关闭缓冲/压缩，不改写事件；透传 Last-Event-ID 与建流前错误。断开订阅不取消 run，不自动重试 POST。启动和回答重试复用 submission_id。

## 成本与兼容
finalize_response 后额外一次无工具流式模型调用，计入预算、usage 和 trace。不得宣称降低调用次数。旧 Harness 通过快照建立起点，不虚构历史 delta；旧 SSE v2 继续旧入口。正式放量前要求真实 PostgreSQL/SQLite、恢复、前端、模拟 BFF 和旧接口回归通过。

## 实体字段与前端处理
- run：id/run_id、goal、root_task_id、status、outcome、allowed_actions、completed_count/task_count/waiting_count、elapsed_ms、phase、error。任务非终态禁止切换档案、专家和发起新任务。error 仅公开安全错误码，不公开脚本 stderr。
- node：id/run_id/parent_id、kind（main/expert/skill/tool）、actor/title、status/outcome、started_at/ended_at、stale、error。标题是业务动作，不是原始工具参数。重试通过状态和 attempts 展示。
- input_request：id/run_id/owner_task_id/actor、round、fields、status、version、request_version、answer。version 是公开实体版本；提交 expected_version 必须使用 request_version。answer 包含 answers/skipped/skipped_fields；不推断用户永久拒绝。
- message：id/run_id、kind（phase/final）、revision、version、content、status。phase 与 final 用不同稳定 ID。增量事件 data 为 id/version/revision/offset/text，完整快照仍保存累计 content。
- run.finalized：在该 run 最终 message.updated 之后发布一次，包含 record_version、record_url 和最终消息版本；表示可落库，不关闭 SSE。
- snapshot/resync_required 是控制事件，run_id/entity_id 为 null；snapshot 的 seq 等于快照 cursor。不存在“订阅结束即任务结束”的语义。

首次读取 `/harness/capabilities` 获取 protocols/streaming_text/replay/partial_answers。需要同时启用服务端与客户端开关；开关不自动切换已有 legacy 会话。历史 Harness 使用快照建立起点，已存在的非 v3 run 不强制改成流式生成。

## 可运行联调

原项目目录中设置以下环境后用 `./run.sh` 启动（会执行 Alembic）：
```bash
export HAILIANG_SSE_V3=true
export VITE_HAILIANG_SSE_V3=true
./run.sh
```
保持现有模型、数据库、context policy 配置；不要将生产密钥写入样例。迁移新增三个 public 表。关闭开关回退客户端与接口，不删除表或运行数据。

直接调用算法服务的启动示例：
```http
POST /api/v3/sessions/demo-session/harness/runs
Content-Type: application/json

{"submission_id":"start-001","goal":"帮孩子做规划","context_data":{"user_id":"demo-user","profile_id":"demo-profile"}}
```
```http
GET /api/v3/sessions/demo-session/harness?user_id=demo-user&profile_id=demo-profile
GET /api/v3/sessions/demo-session/harness/events?user_id=demo-user&profile_id=demo-profile&after=0
```
生产浏览器应走 BFF，不自行声明可信用户身份。BFF 本地样例：
```bash
BFF_DEMO_COOKIE=local-only BFF_DEMO_USER=demo-user BFF_DEMO_PROFILE=demo-profile \
  PYTHONPATH=. .venv/bin/uvicorn examples.sse_v3.bff:app --host 127.0.0.1 --port 8090
```
模拟提交时附带 `Cookie: demo_session=local-only` 和 `Origin: http://127.0.0.1:8090`。默认上游为 8016，可用 BFF_UPSTREAM 覆盖。此固定 cookie 仅供本地联调，实际 BFF 必须替换为自己的登录态校验与授权档案列表。

## 完整样例与故障演练

- `sse_v3/parallel-partial-stream.sse`：真实 runtime、虚构输入生成的完整 wire 流，涵盖两位专家、内部 Skill、追问排队、部分回答、全部跳过、partial 汇总和中文/emoji 正文。
- `sse_v3/parallel-partial-stream.events.json`：同一事件序列的 JSON，按 event.schema.json 校验。
- `sse_v3/snapshot.json`：该流最终快照，前端重放后的实体应与其一致。
- 生成命令：`PYTHONPATH=src .venv/bin/python scripts/generate_sse_v3_examples.py`，不访问模型、不使用真实用户资料。
- 断线演练：消费至任意 seq=N 后断开，用 Last-Event-ID:N 重连；终态与完整重放一致。
- 重复演练：重复投递同一帧不追加正文。缺片演练：丢掉一帧后前端检测 seq/offset 缺口，拉快照后重订阅。
- 过期答案：使用旧 request_version 提交返回 409；同提交 ID 相同内容重发返回原结果。
- 停止演练：正文期间 POST cancel，保留已发布文本、消息 interrupted，关闭待答卡片；后续旧执行 token 写入被拒绝。
- 重启演练：租约过期后恢复正文生成，发布更高 revision 的 message.updated 清空旧草稿，之后增量从 offset=0 开始。
- 风控演练：后续检查拦截时 message.updated 清空正文并标记 blocked；客户端不得恢复此前增量。

## 边界

默认入口仍关闭 v3。自动测试中的模型为确定性替身；真实供应商逐字输出速度、额外生成成本及业务质量需灰度回放后验收。100ms 是片段合并目标，不是首字延迟承诺；风控耗时和模型输出间隔会影响实际发布频率。正常事件不触发全量拉取，首次加载、回答确认及缺片恢复允许读取快照。

验收状态与已知限制见 [验收记录](sse_v3/ACCEPTANCE.md)，模拟成本记录见 [measurement.json](sse_v3/measurement.json)。
