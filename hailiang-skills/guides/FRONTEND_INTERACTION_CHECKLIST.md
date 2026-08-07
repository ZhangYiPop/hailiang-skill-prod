# 前端 SSE v2 交互清单

前端聊天、进入/退出 Skill 和停止都只请求 `POST /api/v1/sessions/chat/stream`。
本地调试身份与项目后端转发端使用同一请求契约；生产环境由 BFF 注入真实学生身份。

字段定义、空结构、生命周期、表单和错误码以 [SSE_RESPONSE_CONTRACT.md](SSE_RESPONSE_CONTRACT.md)
为唯一权威；本清单只保留接入与验收动作。

## 请求

- 普通消息：`input.action=chat`、`source=chat`
- 工具栏进入：`enter_skill`、`source=toolbar`
- 推荐卡片进入：`enter_skill`、`source=route_suggestion`，带推荐卡片的 `source_message_id` 与 `source_interaction_id`
- 退出：`quit_skill`、`source=toolbar|exit_button`，传当前 `target_skill_id`
- 停止：`stop`、`source=composer`，复用当前活动的 `run_id`

每一次非停止动作都生成新的全局唯一 `run_id`。用户在流式输出中发送下一条消息时，先在 UI
将旧助手占位标为 `superseded` 并清空其未完成正文/表单/推荐，再发送新请求；后端会同步撤销旧 run。

## 消费 state

1. 接收 `event: state`（且 `protocol === "hailiang.sse.v2"`）和最终的 `event: done`。
2. 只处理当前 `run_id` 且 `seq` 大于已处理序号的帧。
3. 对当前助手占位消息用整帧状态替换 `assistant`、`intent`、`form`、`path_options`、`skill_rooms`、`skill_transition`、`session`、`risk`、`error`。
4. 读取 `session.active_skill` 更新页面顶部当前 Skill；`general_chat` 不显示为专项主题。历史消息不得覆盖该全局状态。
5. `completed`、`stopped`、`superseded`、`blocked`、`failed` 都是终态，关闭 loading 和本轮发送控制。
6. `event: done` 的 data 是最后一个完整状态快照，表示该次 SSE 的所有数据已经发送完成；转发端可直接落库，前端不必重复更新消息状态。

固定渲染顺序：`intent → assistant.content → form → path_options → skill_rooms`。

- `path_options.options[]` 点击后发送普通 `chat`，内容使用服务端返回的 `prompt`；不发送新的路径选择 action。
- 路径卡片只在当前最新助手消息且 `enabled=true` 时可点击；发送下一条用户消息后立即置灰上一轮路径卡片。

- `intent.steps[].label` 直接显示；禁止展示 `detail`、模型推理原文、Prompt 或工具参数。
- `form` 为空对象时不渲染。Facts 表单提交、Facts 写入和 interaction PATCH 仍沿用原逻辑。
- 仅最新助手消息里 `skill_rooms[].enabled=true` 的卡片可点击；所有历史、已选择、过期卡片置灰。
- `skill_transition` 用于即时展示转场结果；退出后立即把顶部状态设为 `general_chat`。

## 风控和异常

- `risk.status` 会在输入、输出、降级与拦截的每个阶段刷新。
- `risk.blocked=true` 或 `status=blocked` 时，只显示 `risk.message` 的通用提示；不得展示 provider、标签、case ID 或内部诊断。
- 输出拦截帧已经将正文、表单和 Skill 卡片清空；前端不得从旧帧恢复它们。
- `stopped` 保留已显示正文，显示“已停止”，不显示新表单/Skill 卡片。
- 建连前失败可重试；一旦收到 state 帧，不自动重放同一个请求，避免重复用户消息。
- `error.message` 和 `error.code` 用于展示错误；本地调试可展开 `upstream_detail`，生产界面默认隐藏它。

## Skill 场景锁

- 只有 `enter_skill`（工具栏或最新 `route_suggestion` 卡片点击）可以切换 Skill。
- 用户输入的场景名、显式切换文本、`1`/`2`/`第二个`、表单值和追问短回答均不会自动切换；必要时仅由本轮 `skill_rooms` 提供可点击建议。
- 新会话默认 `general_chat`。路由匹配只是后端给 general_chat 模型的候选证据；只有模型返回并经后端校验后的 `skill_rooms` 才展示，前端不得从用户文本或路由调试字段自行生成按钮。
- 工具栏请求 `GET /api/v1/skills` 的完整返回列表；当前服务不按 `grade` 或 `routing.school_stage_scope` 过滤。当前已激活的 Skill 不应显示为可进入按钮。
- `source_message_id` 是助手消息记录标识，`run_id` 是一次流运行标识；二者不可互换。

## 历史与验收

历史接口返回的旧 `blocks`/`route_suggestions` 已由后端规范化为相同的 `presentation` 模型；
前端只渲染这一套状态模型。验证至少包括：新会话无开场白、历史恢复、退出后连续聊天保持
`general_chat`、表单正常提交、推荐仅最新可点、停止、新输入 supersede、输入/输出风控与重复 run 冲突。
