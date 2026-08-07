# 对话 SSE v2 数据流

## 主链路

```text
本地调试前端 / 项目 BFF
  -> POST /api/v1/sessions/chat/stream
  -> 校验或恢复 (session_id, user_id, profile_id)
  -> run 台账与并发协调
  -> StreamingRunner / MainPlannerOrchestrator
  -> 风控、正文、Facts 表单、Skill 路由
  -> event: state (hailiang.sse.v2 完整状态快照)
  -> event: done (本次 SSE 数据全部发送完成)
  -> 前端按 run_id + seq 替换当前消息状态
```

`input.action` 决定执行 `chat`、`enter_skill`、`quit_skill` 或 `stop`。前三者使用转发端
生成的新 run ID；`stop` 必须复用活动 run ID。普通新动作会 supersede 同会话的活动 run。

## 后端状态累加

运行器不将内部的 `skill_status`、文本 delta、Facts、转场和风控作为不同 wire event 暴露。
它们统一写入以 `session_id + run_id` 标识的状态累加器后，发送完整状态：

```text
run_started -> risk/input
skill_status -> intent.steps
final_text_delta -> assistant.content
skill_action -> form + skill_rooms
skill_transition -> session.active_skill + skill_transition
security -> risk
run_completed/run_cancelled -> status 终态
```

业务状态帧为 `event: state`，序号严格递增。所有状态帧之后追加一帧不带 `seq` 的 `event: done`，
表示本次 SSE 数据已全部发送完成。空值保持稳定对象/数组，避免前端通过字段是否存在推断状态。

## 会话与持久化

首次收到未知 session 时，服务只创建/同步学生档案初始 Facts，并立刻执行当前 action；不产生
开场白。恢复既有 session 时不覆盖已由对话提取的 Facts。

正常完成后，助手消息会持久化 v2 `presentation`。历史旧消息中的 `blocks` 和
`route_suggestions` 在读取时由后端转换为同一 presentation，且历史 Skill 卡片均禁用。

退出 Skill 会立即持久化 `general_chat` 与退出提示。此后的普通聊天只读取会话当前状态，不会被
历史的专项 Skill 顶部标记覆盖。

## 并发、停止和风控

- 同一普通 `run_id` 重复调用返回 `409 RUN_ID_CONFLICT`。
- 新用户动作会原子标记旧 run 为 `superseded`，未完成助手回答不持久化。
- `stop` 只允许当前活动 run，终态 `stopped` 保留已发送正文，不再添加表单或 Skill 卡片。
- 风控每阶段立即写入 `risk`；输出拦截会清空本轮正文、表单和 Skill 卡片，并输出通用提示。

字段级规范与前端行为见 [SSE_RESPONSE_CONTRACT.md](../../guides/SSE_RESPONSE_CONTRACT.md)
和 [FRONTEND_INTERACTION_CHECKLIST.md](../../guides/FRONTEND_INTERACTION_CHECKLIST.md)。
