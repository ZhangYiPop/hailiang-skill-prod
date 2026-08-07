# SSE v2 前端对齐协议

> 权威范围：`POST /api/v1/sessions/chat/stream` 的**成功 SSE 响应**。请求体、建流前
> HTTP 错误见 [API_DOCUMENTATION.md](API_DOCUMENTATION.md)。前端只需按本文件渲染聊天。

## 1. 核心原则

- 协议固定为 `hailiang.sse.v2`，不兼容旧的 `system/message/card/done` 业务事件和
  `message_blocks` 数组。
- `event: state` 的 `data` 是本轮助手消息的**完整当前快照**，不是增量补丁。收到较新帧后，
  前端以该帧整体替换当前消息的展示状态。
- 同一 `run_id` 只应用 `seq` **严格大于**本地已处理序号的状态帧；重复帧、旧帧直接丢弃。
- 所有顶层字段始终存在；无内容时使用 `{}`、`[]`、空字符串或 `null`，不省略字段。
- 固定渲染顺序：`intent → assistant.content → form → path_options → skill_rooms`。

## 2. SSE 事件层

| 事件 | 是否业务数据 | 前端动作 |
| --- | --- | --- |
| `state` | 是 | 校验 `protocol/run_id/seq` 后替换当前助手占位消息；这是唯一持续更新 UI 的事件。 |
| `done` | 是，终止确认 | `data` 与最后一个 `state` 完全相同，不增加 `seq`。确认本次流已完整发送；不要再重复渲染。 |
| `ping` | 否 | 保活帧，`data: {}`；不改变状态、不增加 `seq`，直接忽略。 |

```text
event: state
data: {"protocol":"hailiang.sse.v2", "run_id":"run_xxx", "seq":3, ...}

event: ping
data: {}

event: done
data: {"protocol":"hailiang.sse.v2", "run_id":"run_xxx", "seq":12, "status":"completed", ...}
```

`done` 前不会再有新的业务状态帧；随后连接关闭。若网络在收到 `state` 后断开，**不要**自动重放
相同 `run_id`，避免重复保存用户消息；前端应通过历史接口恢复或由用户重新发起新 run。

## 3. 完整状态对象

```json
{
  "protocol": "hailiang.sse.v2",
  "session_id": "sess_xxx",
  "run_id": "run_xxx",
  "seq": 12,
  "ts": "2026-07-24T08:30:00+00:00",
  "elapsed_ms": 820,
  "message_id": "msg_xxx",
  "status": "streaming",
  "assistant": { "content": "已生成的完整正文", "status": "streaming" },
  "intent": {},
  "form": {},
  "path_options": {},
  "skill_rooms": [],
  "skill_transition": {},
  "session": { "active_skill": {} },
  "risk": { "status": "passed", "stage": "input", "blocked": false, "message": "" },
  "error": { "code": "", "message": "", "upstream_detail": "", "retryable": false, "terminal": false }
}
```

### 3.1 顶层字段

| 字段 | 类型 / 空值 | 何时变化 | 前端展示与用途 |
| --- | --- | --- | --- |
| `protocol` | 固定字符串 | 不变 | 必须等于 `hailiang.sse.v2`，否则丢弃。 |
| `session_id` | 字符串 | 不变 | 会话归属校验、日志关联；不在聊天气泡展示。 |
| `run_id` | 字符串 | 不变 | 本轮流标识、`seq` 的比较域；不是 `message_id`。 |
| `seq` | 正整数 | 每个 `state` 严格递增 | 去重和乱序保护；不展示。 |
| `ts` | ISO 8601 字符串 | 每帧刷新 | 调试/时序记录；不作为用户消息时间来源。 |
| `elapsed_ms` | 整数 | 每帧刷新 | 本轮耗时；默认不展示。 |
| `message_id` | 字符串或 `null` | 助手消息创建/完成后写入 | 后续表单、反馈、卡片点击的消息标识；不要用 `run_id` 代替。 |
| `status` | 枚举 | 整轮生命周期 | 控制 loading、停止、失败及终态，见下表。 |
| `assistant` | 对象 | 正文流式累加或终态变更 | 对话正文。`content` 是**截至当前帧的完整文本**，不可自行追加旧 delta。 |
| `intent` | 对象或 `{}` | 推理状态开始、步骤更新、完成 | 显示在正文顶部的“推理进度”；无内容不渲染。 |
| `form` | 对象或 `{}` | 缺失 Facts 时出现、提交后状态更新 | 显示在正文底部；无内容不渲染。 |
| `path_options` | 对象或 `{}` | 高中多元路径 Skill 产出多个可继续展开的路径时出现 | 显示路径选择卡片；点击后发送普通 `chat` 文本，不切换 Skill。 |
| `skill_rooms` | 数组 | general_chat 模型给出合法推荐后出现 | 显示在表单后；只有满足可点击条件的卡片可操作。 |
| `skill_transition` | 对象或 `{}` | 进入/退出 Skill | 即时更新顶部当前主题；可展示转场提示。 |
| `session.active_skill` | 对象或 `{}` | 首帧、转场、最终状态 | 页面顶部的唯一当前 Skill 来源；历史消息不得覆盖。 |
| `risk` | 固定对象 | 输入/输出风控检测时 | 驱动通用安全提示；不得显示内部标签或供应商详情。 |
| `error` | 固定对象 | 模型调用异常时 | 展示 `code + message`；本地调试才可展开 `upstream_detail`。 |

Skill 元数据统一包含 `brief` 和 `info`：`brief` 用于推荐按钮下方的简短说明，`info` 用于进入 Skill 后的状态卡或 intro 说明。`description` 是旧兼容字段，新版前端不再将其作为主要展示文案。`general_chat` 也返回这两个字段，但前端将其作为隐式默认状态隐藏。

### 3.2 标识符语义补充

前端联调中最常见的误区是把 `run_id`、`message_id` 和推荐来源 ID 混用。它们的边界如下：

| 字段 | 语义 | 典型用途 | 不能拿来做什么 |
| --- | --- | --- | --- |
| `run_id` | 本次 SSE 执行 ID | 流式去重、停止当前生成、判断 `seq` 比较域 | 不能代替消息主键，也不能拿去当 `source_message_id`。 |
| `message_id` | 当前这条助手消息的稳定主键 | 表单提交状态同步、点赞/点踩、推荐来源绑定、历史恢复 | 不能代替本轮执行 ID。 |
| `skill_rooms[].source_message_id` | 产生这批推荐卡片的 assistant 消息 ID | 点击推荐卡片时传给 `input.source_message_id` | 不是任意上一轮 run，也不是前端本地数组索引。 |
| `skill_rooms[].source_interaction_id` | 产生推荐卡片的交互 ID | 点击推荐卡片时传给 `input.source_interaction_id` | 现阶段不要自行改写，按服务端返回原样透传。 |

实现上可按下面的口诀处理：

- `run_id` 属于“这次请求”
- `message_id` 属于“这条消息”
- `source_message_id` 属于“触发点击的那条旧消息”

### 3.3 `status` 与 `assistant.status`

| 值 | 是否终态 | UI 行为 |
| --- | --- | --- |
| `streaming` | 否 | 显示生成中；允许当前帧继续替换正文、推理、表单和卡片。 |
| `completed` | 是 | 结束 loading，保留正文、表单和有效卡片。 |
| `stopped` | 是 | 展示“已停止”，保留已收到的正文；不展示本轮后续新表单或卡片。 |
| `superseded` | 是 | 新输入取代旧 run；旧占位消息标记已被取代，清空未完成正文、表单和卡片。 |
| `blocked` | 是 | 风控拦截；只展示 `risk.message`，正文、表单、卡片均以当前帧为空值为准。 |
| `failed` | 是 | 展示 `error.code + error.message`；存在 `retryable=true` 时可提供“重试”。 |

`assistant.status` 与外层状态一致或为当前正文生成状态；前端以外层 `status` 判断终态。

## 4. 可渲染模块

### 4.1 推理进度 `intent`

```json
{
  "status": "streaming",
  "steps": [
    {"id":"intent", "label":"正在识别本轮需求", "status":"completed", "detail":""},
    {"id":"planner", "label":"正在制定规划思路", "status":"active", "detail":""}
  ]
}
```

| 字段 | 规则 |
| --- | --- |
| `status` | `streaming` 或 `completed`；完成后所有活动步骤应显示为完成。 |
| `steps[].id` | 稳定步骤键，用于替换同一步骤。 |
| `steps[].label` | 直接展示的完整中文文案；前端不得再拼“正在”。 |
| `steps[].status` | 通常为 `active` 或 `completed`。 |
| `steps[].detail` | 内部诊断字段，普通前端不展示。 |

### 4.2 表单 `form`

`form={}` 表示本轮没有 Facts 表单。当前只支持单张表单：

```json
{
  "form_id":"missing_facts_form",
  "title":"补充关键信息",
  "description":"",
  "status":"active",
  "interaction_id":"fact_form:missing_facts_form",
  "fields":[{
    "fact_key":"grade",
    "label":"孩子年级",
    "input_type":"single_select",
    "required":true,
    "placeholder":"请选择孩子当前年级",
    "example":"例如：初二",
    "options":[{"label":"高一","value":"高一"}],
    "submit_mode":"auto",
    "scope":"profile",
    "value_type":"string"
  }]
}
```

| 字段 | 说明 |
| --- | --- |
| `form_id` | 表单实例 ID；和 `interaction_id` 一起用于提交状态同步。 |
| `title` / `description` | 原样显示；`description` 可为空字符串。 |
| `status` | `active` 可编辑；`submitted` 已提交；`expired` 置灰，不可再次提交。 |
| `interaction_id` | 固定形如 `fact_form:{form_id}`。 |
| `fields` | 当前需要用户补充的字段列表；列表顺序即展示顺序。 |
| `fields[].fact_key` | Facts 键，提交时使用。 |
| `label` / `placeholder` / `example` | 原样用于字段标题、提示和示例。 |
| `input_type` | `text`、`single_select`、`multi_select`。未知类型降级为文本输入。 |
| `required` | 是否必填；当前服务端默认 `true`。 |
| `options` | 选项数组；仅选择类控件使用，文本字段为 `[]`。 |
| `submit_mode` | `auto`：值变更后可自动写 Facts；`manual`：由用户点提交按钮。 |
| `scope` | Facts 作用域，如 `profile`、`session`、`shared`。 |
| `value_type` | Facts 值类型，如 `string`、`string_list`、`boolean`；提交时保持对应 JSON 类型。 |

#### 当前可下发 Facts 字段

> 当前服务端来源是 `config/fact_form_config.yml`；以下是本版本枚举，前端仍应以实际
> `form.fields` 和 `GET /api/v1/facts/form-config` 为准，不得硬编码为唯一字段集。

| `fact_key` | 标题 | 控件 / 可选值 | 提交 |
| --- | --- | --- | --- |
| `grade` | 孩子年级 | 单选：小学、初一、初二、初三、高一、高二、高三 | 自动 |
| `student_province` | 高考省份 | 文本 | 自动 |
| `physical_requirements` | 身高 | 文本 | 手动 |
| `subject_group` | 选科组合 | 单选：物理、历史 | 自动 |
| `score_total` | 当前分数 | 文本 | 手动 |
| `score_recent_avg` | 最近三次大考均分 | 文本 | 手动 |
| `budget_level` | 预算水平 | 单选：大于 5 万、小于 5 万 | 自动 |
| `career_orientation` | 职业兴趣 | 多选：师范类、军警类、飞行员、其他 | 手动 |
| `exam_qualification_status` | 学考是否合格 | 单选：合格、不合格 | 自动 |
| `hukou_years` | 户籍是否连续满三年 | 单选：连续 3 年户籍、否 | 自动 |
| `special_identity_tags` | 荣誉情况 | 多选：五大学科竞赛省级及以上奖项、五大学科竞赛国家集训队成员、其他 | 手动 |

表单 Facts 写入成功后，前端调用：

```http
PATCH /api/v1/sessions/{session_id}/messages/{message_id}/interactions/{interaction_id}
Content-Type: application/json

{"status":"submitted","submitted_fact_keys":["grade","student_province"]}
```

`interaction_id` 必须等于 SSE 给出的值；已失效表单返回 `409 INTERACTION_INACTIVE`。

#### 表单提交后的继续执行

`PATCH /sessions/{session_id}/messages/{message_id}/interactions/{interaction_id}` 只负责把表单状态同步为 `submitted`。对于当前 Native Questionnaire，真正推进 Skill 的动作通常是再发一条普通 `chat`：

```json
{"action":"chat","content":"字段标签：值；字段标签：值","source":"chat"}
```

也就是说：

1. `message_id` / `interaction_id` 负责“标记这张表单已提交”
2. 后续 `chat` 文本负责“把答案继续送进当前 Skill”

如果前端暂时只做第 2 步而不做 PATCH，后端通常仍能继续流程，但历史表单不会自动显示为 `submitted`。

### 4.3 高中路径选择 `path_options`

当 `multi_path_planning` / `convergence` 产出可继续展开的路径时，v2 状态返回：

```json
{
  "status": "active",
  "interaction_id": "path_actions",
  "source_message_id": "msg_xxx",
  "options": [
    {
      "path_id": "path_xxx",
      "title": "综合评价招生",
      "description": "路径简介",
      "prompt": "我想了解：综合评价招生 路径",
      "enabled": true
    }
  ]
}
```

前端点击后使用 `prompt` 发起普通请求：

```json
{"action":"chat","content":"我想了解：综合评价招生 路径","source":"chat"}
```

该操作不发送 `select_path`，不修改 `requested_target_skill_id`，由后端现有路由继续进入路径详情分析。`path_options` 的
`interaction_id` 保持为 `path_actions`，以兼容历史消息和交互状态；新用户消息产生后，上一轮选项自动失效。

| 字段 | 规则 |
| --- | --- |
| `status` | `active` 可点击；`selected` 或 `expired` 置灰。 |
| `interaction_id` | 固定为 `path_actions`。 |
| `source_message_id` | 产生路径卡片的助手消息 ID。 |
| `options[].path_id` | 路径资产 ID。 |
| `options[].title` | 路径展示名称。 |
| `options[].description` | 路径简要说明。 |
| `options[].prompt` | 点击时提交的完整普通聊天文本。 |
| `options[].enabled` | 仅当前最新且 active 的路径卡片为 `true`。 |

### 4.4 Skill 推荐 `skill_rooms`

```json
[
  {
    "skill_id":"score_improve",
    "title":"提分",
    "brief":"帮助孩子定位学习提升重点并制定提分计划。",
    "info":"进入提分规划后，我会结合当前学科表现和学习习惯，梳理优先提升的科目与可执行的学习安排。",
    "description":"可继续制定各科学习提升方案。",
    "status":"enterable",
    "enabled":true,
    "source_message_id":"msg_xxx",
    "source_interaction_id":"route_suggestions"
  }
]
```

| 字段 | 前端规则 |
| --- | --- |
| `skill_id` | 进入 Skill 的 `target_skill_id`。 |
| `title` | 卡片按钮标题。 |
| `brief` | 按钮下方的一行简要说明。 |
| `info` | 进入 Skill 后的详细说明，不在推荐列表默认展开。 |
| `description` | 旧兼容字段；新版前端不再优先展示。 |
| `status` | `enterable` 显示“可进入”；`entered` 显示“已进入”并禁用。 |
| `enabled` | 仅在**当前最新助手消息**且值为 `true` 时可点击；历史、已选择、已过期项目一律置灰。 |
| `source_message_id` | 点击推荐卡片时传给 `input.source_message_id`。 |
| `source_interaction_id` | 点击推荐卡片时传给 `input.source_interaction_id`。 |

推荐卡片只会由 `general_chat` 模型或后端高置信度路由在正文完成后决定。路由关键词、用户输入的 Skill 名称和短回答
都不能自动切换 Skill，也不能由前端自行生成卡片。当前工具栏和推荐候选均**不按**
`routing.school_stage_scope` 或学生年级过滤；前端必须以服务端实际返回的目录和卡片为准。

补充约束：

- 推荐卡片进入 Skill 与 toolbar 进入 Skill 共用同一个接口 `POST /sessions/chat/stream`
- 但推荐卡片必须使用 `source="route_suggestion"`，且必须带 `source_message_id` 与 `source_interaction_id`
- toolbar 进入 Skill 使用 `source="toolbar"`，不携带上述来源字段
- 服务端会校验 `source_message_id` 指向的消息必须是**当前最新 assistant 消息**且该推荐仍为 `active`；否则常见返回为 `409 route suggestion is no longer current`

### 4.4 Skill 状态与转场

```json
{
  "skill_transition":{"action":"enter","from_skill_id":"general_chat","to_skill_id":"subject_advisor","source":"toolbar"},
  "session":{"active_skill":{"skill_id":"subject_advisor","title":"选科参谋","description":"","scene_name":"选科参谋"}}
}
```

- `session.active_skill` 是顶部“当前规划主题”的唯一来源。
- `skill_id="general_chat"` 时不显示专项主题徽标。
- `skill_transition.action` 为 `enter` 或 `exit`；`exit` 的 `to_skill_id` 固定为 `general_chat`。
- 进入/退出的同一状态帧中，必须同时依据 `skill_transition` 和 `session.active_skill` 更新 UI。
- 当 `skill_transition.action="exit"` 时，状态帧还会给出退出提示文案，前端可渲染转场卡；不要再把同一语义重复渲染成第二条普通 assistant 气泡。

## 5. 风控与错误

### 5.1 `risk`

| 字段 | 值 / 行为 |
| --- | --- |
| `status` | `idle`、`checking`、`passed`、`degraded`、`blocked`。 |
| `stage` | 通常为 `input` 或 `output`。 |
| `blocked` | `true` 时优先展示 `risk.message`，不展示正文、表单、卡片。 |
| `message` | 可面向用户展示的通用安全提示；空字符串表示无需提示。 |

不得显示供应商名称、风险标签、case ID、审核原文或内部诊断。输出被拦截时，状态帧已经将
`assistant.content`、`form`、`path_options`、`skill_rooms` 清空；前端不得从旧帧回填这些内容。

### 5.2 `error`

```json
{
  "code":"MODEL_TIMEOUT",
  "message":"模型响应超时，请稍后重试。",
  "upstream_detail":"LLMConnectionError: timeout ...",
  "retryable":true,
  "terminal":true
}
```

- 生产环境：只显示 `code` 和 `message`。
- 本地调试：可折叠显示已脱敏且最长 2,000 字符的 `upstream_detail`。
- `terminal=true` 时本轮以 `status=failed` 结束；`terminal=false` 表示辅助模型（如推荐卡片判定）失败，正文可继续并以 `completed` 结束。

| 错误码 | 用户提示含义 | `retryable` |
| --- | --- | --- |
| `MODEL_UNAVAILABLE` | 模型服务未配置或暂不可用 | 是 |
| `MODEL_TIMEOUT` | 模型响应超时 | 是 |
| `MODEL_AUTHENTICATION_FAILED` | 模型服务认证失败 | 否 |
| `MODEL_RATE_LIMITED` | 模型服务限流 | 是 |
| `MODEL_INVALID_RESPONSE` | 模型返回内容无法解析 | 是 |
| `MODEL_STREAM_INTERRUPTED` | 模型流连接中断 | 是 |
| `MODEL_UPSTREAM_ERROR` | 模型上游服务错误 | 是 |
| `MODEL_RUNTIME_ERROR` | 未分类模型运行错误 | 是 |

### 5.3 建流前 HTTP 错误

这些错误没有 SSE 帧，应按 HTTP 状态处理。

| HTTP | `detail` / 业务码 | 含义 | 前端动作 |
| --- | --- | --- |
| 422 | `INVALID_INPUT_JSON` | `input` 不是 JSON 对象字符串 | 修正请求，不重试。 |
| 422 | `unsupported action`、校验详情 | action、source、字段类型或枚举错误 | 修正请求，不重试。 |
| 422 | `route_suggestion requires source_message_id and source_interaction_id` | 卡片点击缺少来源绑定 | 丢弃点击，刷新当前消息。 |
| 409 | `SESSION_ID_CONFLICT` / `PROFILE_ID_CONFLICT` | 身份或档案归属冲突 | 终止并重新获取身份/会话。 |
| 409 | `RUN_ID_CONFLICT` | 普通动作重用了 run ID | 不重试相同 run；生成新 run 后按需重发。 |
| 409 | `RUN_NOT_ACTIVE` | stop 指向非活动 run | 停止 loading，刷新会话状态。 |
| 409 | `QUIT_SKILL_TARGET_MISMATCH` | 退出目标不是当前 Skill | 以最新 `session.active_skill` 刷新 UI。 |
| 409 | `TARGET_SKILL_ALREADY_ACTIVE` / 交互失效 | 重复进入或卡片已失效 | 保持当前状态，不重试。 |
| 409 | `SESSION_UPDATE_CONFLICT` | 并发保存冲突 | 短暂退避后刷新会话。 |
| 429 | 流并发容量错误 | SSE 容量已满 | 按 `Retry-After` 再试。 |

## 6. 典型时序

以下片段省略未变化字段；实际每个 `state` 仍包含第 3 节的完整对象。

### 6.1 普通聊天

1. `state seq=1`：`status=streaming`，`risk={checking,input}`，空正文。
2. `state seq=2`：`intent.status=streaming`，显示“正在识别本轮需求”。
3. `state seq=3..n`：`assistant.content` 逐帧变长，内容是完整正文。
4. `state seq=n+1`：`intent.status=completed`、`risk={passed,output}`。
5. `state seq=n+2`：`status=completed`、`assistant.status=completed`。
6. `done`：重复第 5 步，不重复渲染。

### 6.2 正文 + 表单 + 推荐卡片

1. 先按普通聊天显示推理和正文。
2. 表单出现时，新的 `state` 填充 `form`，前端在正文下方渲染字段。
3. general_chat 正文完成后，若模型给出合法推荐，新的 `state` 填充 `skill_rooms`；前端在表单后渲染卡片。
4. 最终 `completed` 帧保留正文、当前 `form` 和可用卡片。若无表单/卡片，分别保持 `{}`/`[]`。

### 6.3 进入和退出 Skill

- 进入：`state` 先给出 `skill_transition.action=enter`，同帧 `session.active_skill` 变为目标 Skill；后续正文属于该 Skill。
- 退出：`state` 给出 `skill_transition.action=exit`，同帧 `session.active_skill.skill_id=general_chat`；随后 `completed`，顶部专项主题立即消失。
- 服务端会把这次退出持久化为两条历史消息：
  1. synthetic user 消息：`退出AI咨询室`
  2. assistant `skill_transition` 消息
  因此刷新页面后，历史恢复应能继续看到退出指令和退出转场卡。

### 6.4 停止、替代、风控和模型失败

| 场景 | 最终状态帧 | UI 结果 |
| --- | --- | --- |
| 用户停止 | `status=stopped` | 保留已经显示的正文，显示已停止。 |
| 新输入替代旧 run | `status=superseded` | 清空旧未完成正文/表单/卡片，标记已被新输入取代。 |
| 风控拦截 | `status=blocked`、`risk.blocked=true` | 仅显示通用 `risk.message`。 |
| 主模型失败 | `status=failed`、`error.terminal=true` | 显示错误码和友好提示；可按 `retryable` 提供重试。 |
| 推荐判定等辅助模型失败 | `error.terminal=false`，最终通常 `completed` | 保留正文，不显示本轮新卡片。 |

## 7. 前端实现清单

1. 每次请求为当前助手气泡创建一个本地占位消息，并记录该请求的 `run_id`。
2. 仅消费匹配 `run_id` 且 `seq` 更新的 `state`；整帧替换 `presentation`。
3. `done` 只做“传输完成”确认；因为 `seq` 未增加，不重复覆盖状态。
4. 使用 `message_id` 提交表单、反馈和推荐来源；使用 `run_id` 做流控制、停止和去重。
5. 后端历史接口会将旧 `blocks/route_suggestions` 转换为相同 `presentation` 模型；历史与实时只保留一套渲染逻辑。
6. 恢复历史会话时，优先使用 `GET /sessions/{session_id}` 的 `skill_display` 决定当前顶部 `active_skill`，并使用 `GET /sessions/{session_id}/context` 的 `messages[].presentation` 恢复消息列表；不要重放旧 `run_id`。

相关文档：

- [API_DOCUMENTATION.md](API_DOCUMENTATION.md)：请求、HTTP 接口与 BFF 转发边界。
- [FRONTEND_INTERACTION_CHECKLIST.md](FRONTEND_INTERACTION_CHECKLIST.md)：前端接入验收清单。
- [SSE_ERROR_CODES.md](SSE_ERROR_CODES.md)：模型错误码速查。
