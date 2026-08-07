# SSE v2 前端 Mock 示例

> 完整字段定义与渲染规则以 [SSE_RESPONSE_CONTRACT.md](SSE_RESPONSE_CONTRACT.md) 为准；本文件只提供 Mock 片段。

聊天接口只使用 `POST /api/v1/sessions/chat/stream`。业务帧为 `event: state`，以下每一帧都应被
前端作为**完整状态**替换当前 run 的消息，而非按事件类型拼装。最后一个状态帧之后固定追加
一帧 `event: done`，表示本次 SSE 的全部数据已经发送完成。

## 普通聊天

```text
event: state
data: {"protocol":"hailiang.sse.v2","session_id":"sess_001","run_id":"run_001","seq":1,"ts":"2026-07-24T09:00:00Z","elapsed_ms":10,"message_id":null,"status":"streaming","assistant":{"content":"","status":"streaming"},"intent":{},"form":{},"skill_rooms":[],"skill_transition":{},"session":{"active_skill":{}},"risk":{"status":"checking","stage":"input","blocked":false,"message":""},"error":{"code":"","message":"","upstream_detail":"","retryable":false,"terminal":false}}

event: state
data: {"protocol":"hailiang.sse.v2","session_id":"sess_001","run_id":"run_001","seq":2,"ts":"2026-07-24T09:00:00Z","elapsed_ms":180,"message_id":null,"status":"streaming","assistant":{"content":"","status":"streaming"},"intent":{"status":"streaming","steps":[{"id":"intent","label":"正在识别本轮需求","status":"active","detail":""}]},"form":{},"skill_rooms":[],"skill_transition":{},"session":{"active_skill":{"skill_id":"general_chat","title":"general_chat","description":"","scene_name":""}},"risk":{"status":"passed","stage":"input","blocked":false,"message":""},"error":{"code":"","message":"","upstream_detail":"","retryable":false,"terminal":false}}

event: state
data: {"protocol":"hailiang.sse.v2","session_id":"sess_001","run_id":"run_001","seq":3,"ts":"2026-07-24T09:00:01Z","elapsed_ms":900,"message_id":null,"status":"streaming","assistant":{"content":"可以，我们先梳理孩子目前的情况。","status":"streaming"},"intent":{"status":"completed","steps":[{"id":"intent","label":"正在识别本轮需求","status":"completed","detail":""}]},"form":{},"skill_rooms":[],"skill_transition":{},"session":{"active_skill":{"skill_id":"general_chat","title":"general_chat","description":"","scene_name":""}},"risk":{"status":"checking","stage":"output","blocked":false,"message":""},"error":{"code":"","message":"","upstream_detail":"","retryable":false,"terminal":false}}

event: state
data: {"protocol":"hailiang.sse.v2","session_id":"sess_001","run_id":"run_001","seq":4,"ts":"2026-07-24T09:00:02Z","elapsed_ms":1300,"message_id":"msg_001","status":"completed","assistant":{"content":"可以，我们先梳理孩子目前的情况。","status":"completed"},"intent":{"status":"completed","steps":[{"id":"intent","label":"正在识别本轮需求","status":"completed","detail":""}]},"form":{},"skill_rooms":[],"skill_transition":{},"session":{"active_skill":{"skill_id":"general_chat","title":"general_chat","description":"","scene_name":""}},"risk":{"status":"passed","stage":"output","blocked":false,"message":""},"error":{"code":"","message":"","upstream_detail":"","retryable":false,"terminal":false}}

event: done
data: {"protocol":"hailiang.sse.v2","session_id":"sess_001","run_id":"run_001","seq":4,"ts":"2026-07-24T09:00:02Z","elapsed_ms":1300,"message_id":"msg_001","status":"completed","assistant":{"content":"可以，我们先梳理孩子目前的情况。","status":"completed"},"intent":{"status":"completed","steps":[{"id":"intent","label":"正在识别本轮需求","status":"completed","detail":""}]},"form":{},"skill_rooms":[],"skill_transition":{},"session":{"active_skill":{"skill_id":"general_chat","title":"general_chat","description":"","scene_name":""}},"risk":{"status":"passed","stage":"output","blocked":false,"message":""},"error":{"code":"","message":"","upstream_detail":"","retryable":false,"terminal":false}}
```

## 表单和 Skill 推荐

终态帧只需在同一结构中填入 `form` 或 `skill_rooms`：

```json
{
  "form": {
    "form_id": "missing_facts_form",
    "title": "补充关键信息",
    "description": "",
    "status": "active",
    "interaction_id": "fact_form:missing_facts_form",
    "fields": [{
      "fact_key": "grade", "label": "孩子目前年级", "input_type": "single_select",
      "required": true, "placeholder": "", "example": "",
      "options": [{"label":"高一","value":"高一"}], "submit_mode": "manual",
      "scope": "profile", "value_type": "string"
    }]
  },
  "skill_rooms": [{
    "skill_id": "interest_explore", "title": "兴趣探索",
    "brief": "帮助孩子探索兴趣特长与适合长期发展的方向。",
    "info": "进入兴趣探索后，我会结合孩子的经历、兴趣和阶段特点，逐步收敛值得尝试的特长方向。",
    "description": "进一步了解兴趣方向",
    "status": "enterable", "enabled": true,
    "source_message_id": "msg_001", "source_interaction_id": "route_suggestions"
  }]
}
```

只有这条最新助手消息的 `enabled=true` 卡片可点击。历史回放中的相同卡片必须改为 `false`。

### 推荐卡片点击后的请求

`skill_rooms` 不会直接切换页面状态；前端点击后仍然调用同一个 `POST /api/v1/sessions/chat/stream`，但请求体必须使用：

```json
{
  "session_id": "sess_001",
  "run_id": "run_enter_001",
  "input": "{\"action\":\"enter_skill\",\"target_skill_id\":\"interest_explore\",\"source\":\"route_suggestion\",\"source_message_id\":\"msg_001\",\"source_interaction_id\":\"route_suggestions\"}",
  "context_data": {
    "student_name": "zz",
    "user_id": "test-0723-1",
    "profile_id": "pro-0723-1"
  }
}
```

补充规则：

- `run_id` 是本次点击动作自己的新 run
- `source_message_id` 必须取自 `skill_rooms[].source_message_id`
- 不能把 `run_id` 当成 `source_message_id`

如果 `source_message_id` 指向的不是当前最新 assistant 消息，服务端会返回 `409 route suggestion is no longer current`。

### 表单提交后的后续动作

当前前端常见联调链路是两步：

1. 先同步表单状态：

```http
PATCH /api/v1/sessions/{session_id}/messages/{message_id}/interactions/fact_form:{form_id}
Content-Type: application/json

{"status":"submitted","submitted_fact_keys":["grade","student_province"]}
```

2. 再把答案作为普通 `chat` 文本续给当前 Skill：

```json
{
  "session_id": "sess_001",
  "run_id": "run_form_002",
  "input": "{\"action\":\"chat\",\"content\":\"孩子目前年级：高一；高考省份：浙江\",\"source\":\"chat\"}",
  "context_data": {
    "student_name": "zz",
    "user_id": "test-0723-1",
    "profile_id": "pro-0723-1"
  }
}
```

如果联调时暂时跳过第 1 步，后端通常仍可从第 2 步的文本里继续解析答案，但历史表单不会自动变成 `submitted`。

## 退出、停止和风控

退出 Skill 的终态：

```json
{
  "status": "completed",
  "assistant": {"content":"已为你退出 AI 咨询室，如有问题可以继续提问。","status":"completed"},
  "skill_transition": {"action":"exit","from_skill_id":"interest_explore","to_skill_id":"general_chat","source":"exit_button"},
  "session": {"active_skill":{"skill_id":"general_chat","title":"general_chat","description":"","scene_name":""}}
}
```

停止为 `status=stopped`，保留 `assistant.content`。风控拦截为 `status=blocked`，且
`assistant.content=""`、`form={}`、`skill_rooms=[]`；前端只显示 `risk.message`。

### 退出 Skill 的请求

退出按钮点击后，仍调用 `POST /api/v1/sessions/chat/stream`：

```json
{
  "session_id": "sess_001",
  "run_id": "run_exit_001",
  "input": "{\"action\":\"quit_skill\",\"target_skill_id\":\"interest_explore\",\"source\":\"exit_button\"}",
  "context_data": {
    "student_name": "zz",
    "user_id": "test-0723-1",
    "profile_id": "pro-0723-1"
  }
}
```

补充规则：

- `run_id` 是这次退出动作自己的新 run
- `target_skill_id` 必须等于当前 `session.active_skill.skill_id`
- 退出终态应把 `session.active_skill` 切回 `general_chat`

### 历史恢复时应看到什么

退出成功后，前端刷新页面不应依赖旧 SSE 重放，而应通过历史接口恢复：

- `GET /sessions/{session_id}`：恢复当前顶部 `active_skill`
- `GET /sessions/{session_id}/context`：恢复退出前后的消息列表

退出后的历史消息通常会包含：

1. 一条 synthetic user 消息：`退出AI咨询室`
2. 一条 assistant 转场消息：`message_type = skill_transition`

因此刷新后仍应显示退出转场卡，且顶部主题保持为 `general_chat`。
