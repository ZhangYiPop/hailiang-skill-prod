# SSE v2 `input` 场景速查

以下命令依赖 `jq`。先复制一次公共函数；之后每个场景只需要设置
`SESSION_ID`、`RUN_ID`、`CONTEXT_DATA`、`INNER_INPUT`，最后调用 `post_stream`。

```bash
BASE_URL='http://10.30.6.45:8010'

post_stream() {
  curl -N -X POST "$BASE_URL/api/v2/sessions/chat/stream" \
    -H 'Content-Type: application/json' \
    -H 'Accept: text/event-stream' \
    --data "$(jq -nc \
      --arg session_id "$SESSION_ID" \
      --arg run_id "$RUN_ID" \
      --arg input "$INNER_INPUT" \
      --argjson context_data "$CONTEXT_DATA" \
      '{session_id: $session_id, run_id: $run_id, context_data: $context_data, input: $input}')"
}
```

## 1. 普通聊天：不选择专家团或专家

```bash
SESSION_ID="manual-normal-$(date +%s)-$RANDOM"
RUN_ID="run-$(date +%s)-$RANDOM"
CONTEXT_DATA='{"user_id":"manual-test-user","profile_id":"profile_child_a","student_name":"小海"}'
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "孩子最近有些焦虑，我想先聊聊。", source: "chat",
  expert_context: {expert_team_id: null, expert_id: null, operation: "continue"}, enable_thinking: false, return_reasoning: false
}')
post_stream
```

`input`：除 `stop` 外，`expert_context` 始终固定传三个键：`expert_team_id`、`expert_id`、
`operation`。此处是普通聊天，前两个键都传 `null`；`profile_id` 仍不能放进 `input`。

## 2. 连续链路：选择专家团 → 产生转交卡 → 点击转交卡 → 成员追问

```bash
# 第 1 步：选择专家团并提问。服务端先由主协调专家承接。
SESSION_ID="manual-student-team-$(date +%s)-$RANDOM"
CONTEXT_DATA='{"user_id":"manual-test-user","profile_id":"profile_child_a","student_name":"小海"}'
RUN_ID="run-$(date +%s)-$RANDOM"
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "我要留学。", source: "chat",
  expert_context: {
    expert_team_id: "student_growth_expert_team", expert_id: null, operation: "select_team"
  },
  enable_thinking: false, return_reasoning: false
}')
post_stream

# 第 2 步：仅当第 1 步 SSE state 返回 team_handoff.status="active" 后执行。
# 三个值分别取自该卡片所在 state：source_message_id、candidates[].expert_id、
# expert.active.expert_id（点击前实际承接者）。
SOURCE_MESSAGE_ID='msg_from_previous_state'
TARGET_EXPERT_ID='expert_id_from_previous_card_candidate'
CURRENT_EXPERT_ID='expert_id_from_previous_state_active'
RUN_ID="run-$(date +%s)-$RANDOM"
INNER_INPUT=$(jq -nc \
  --arg source_message_id "$SOURCE_MESSAGE_ID" \
  --arg target_expert_id "$TARGET_EXPERT_ID" \
  --arg current_expert_id "$CURRENT_EXPERT_ID" '{
    action: "confirm_team_handoff", context_scope: "profile", source: "team_handoff",
    source_message_id: $source_message_id, target_expert_id: $target_expert_id,
    expert_context: {
      expert_team_id: "student_growth_expert_team", expert_id: $current_expert_id, operation: "continue"
    },
    enable_thinking: false, return_reasoning: false
  }')
post_stream

# 第 3 步：转交成功后普通追问。无需读取或回传当前专家；服务端继承已确认的成员。
RUN_ID="run-$(date +%s)-$RANDOM"
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "那我现在应该先准备哪些材料？", source: "chat",
  expert_context: {expert_team_id: null, expert_id: null, operation: "continue"},
  enable_thinking: false, return_reasoning: false
}')
post_stream
```

第 1 步 `input`：`select_team` 传目标 `expert_team_id`，`expert_id` 必须为 `null`。

第 2 步 `input`：`target_expert_id` 是用户点击的新专家；
`expert_context.expert_id` 是点击前实际承接的专家，不能为 `null`。

第 3 步 `input`：普通追问固定双 `null`；服务端继承上一步已确认的成员专家。

## 3. 专家团内，用户在工具栏主动指定成员并提问

```bash
# 第 1 步：先选择专家团，建立当前主协调专家状态。
SESSION_ID="manual-toolbar-team-$(date +%s)-$RANDOM"
CONTEXT_DATA='{"user_id":"manual-test-user","profile_id":"profile_child_a","student_name":"小海"}'
RUN_ID="run-$(date +%s)-$RANDOM"
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "我想咨询亲子冲突。", source: "chat",
  expert_context: {expert_team_id: "student_growth_expert_team", expert_id: null, operation: "select_team"},
  enable_thinking: false, return_reasoning: false
}')
post_stream

# 第 2 步：工具栏主动指定成员。CURRENT_EXPERT_ID 取第 1 步 state.expert.active.expert_id。
CURRENT_EXPERT_ID='expert_id_from_previous_state_active'
RUN_ID="run-$(date +%s)-$RANDOM"
INNER_INPUT=$(jq -nc --arg current_expert_id "$CURRENT_EXPERT_ID" '{
  action: "switch_team_member", context_scope: "profile",
  target_expert_id: "admission_specialist",
  content: "我想直接请升学指导专家分析升学方向。", source: "toolbar",
  expert_context: {
    expert_team_id: "student_growth_expert_team", expert_id: $current_expert_id, operation: "continue"
  },
  enable_thinking: false, return_reasoning: false
}')
post_stream
```

`input`：`target_expert_id` 是工具栏新选专家；`expert_context` 是切换前的当前团队和专家。
不要把 `@专家名称` 拼入 `content`，也不要用普通 `chat` 代替该动作。

## 4. 新对话时直接指定专家团内成员

```bash
SESSION_ID="manual-team-member-$(date +%s)-$RANDOM"
RUN_ID="run-$(date +%s)-$RANDOM"
CONTEXT_DATA='{"user_id":"manual-test-user","profile_id":"profile_child_a","student_name":"小海"}'
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "请直接分析孩子的升学方向。", source: "toolbar",
  expert_context: {
    expert_team_id: "student_growth_expert_team", expert_id: "admission_specialist", operation: "select_expert"
  },
  enable_thinking: false, return_reasoning: false
}')
post_stream
```

`input`：`select_expert` 同时传专家团 ID 和该团内的目标成员 ID。目标成员也可以恰好是该团队的
主协调专家；服务端仍将其标记为协调专家，因此该专家后续可以正常生成专家转交卡。

## 5. 新对话时直接指定独立专家（不进入专家团）

```bash
SESSION_ID="manual-expert-$(date +%s)-$RANDOM"
RUN_ID="run-$(date +%s)-$RANDOM"
CONTEXT_DATA='{"user_id":"manual-test-user","profile_id":"profile_child_a","student_name":"小海"}'
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "请帮我分析孩子的升学选择。", source: "toolbar",
  expert_context: {expert_team_id: null, expert_id: "admission_specialist", operation: "select_expert"},
  enable_thinking: false, return_reasoning: false
}')
post_stream
```

`input`：直接专家模式固定 `expert_team_id:null`，`expert_id` 为用户主动选择的专家。

## 6. 同一 Session 切换到另一位孩子后继续聊

```bash
# 第 1 步：在孩子 A 选择团队。
SESSION_ID="manual-child-switch-$(date +%s)-$RANDOM"
CONTEXT_DATA='{"user_id":"manual-test-user","profile_id":"profile_child_a","student_name":"小海"}'
RUN_ID="run-$(date +%s)-$RANDOM"
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "我想咨询孩子的升学方向。", source: "chat",
  expert_context: {expert_team_id: "student_growth_expert_team", expert_id: null, operation: "select_team"},
  enable_thinking: false, return_reasoning: false
}')
post_stream

# 第 2 步：同一 session 切换至孩子 B；不预检，也不需要读取 A 的专家状态。
CONTEXT_DATA='{"user_id":"manual-test-user","profile_id":"profile_child_b","student_name":"小明"}'
RUN_ID="run-$(date +%s)-$RANDOM"
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "请结合小明自己的情况分析升学方向。", source: "chat",
  expert_context: {expert_team_id: null, expert_id: null, operation: "continue"},
  enable_thinking: false, return_reasoning: false
}')
post_stream
```

`input`：切换孩子只由 BFF 修改顶层 `context_data.profile_id`；普通追问仍固定传双 `null`。
`context_activation:"auto"` 时服务端会恢复/创建 B 分支并继承 session 的当前 Agent；不要在
`input` 传 `profile_id`。

## 7. 同一 Session 切换到另一位孩子，并在同一条消息指定团内专家

```bash
# 第 1 步：孩子 A 选择团队。
SESSION_ID="manual-child-member-$(date +%s)-$RANDOM"
CONTEXT_DATA='{"user_id":"manual-test-user","profile_id":"profile_child_a","student_name":"小海"}'
RUN_ID="run-$(date +%s)-$RANDOM"
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "我想咨询孩子的成长问题。", source: "chat",
  expert_context: {expert_team_id: "student_growth_expert_team", expert_id: null, operation: "select_team"},
  enable_thinking: false, return_reasoning: false
}')
post_stream

# 第 2 步：同一 session 切到孩子 B，同时由工具栏指定团内专家。
CONTEXT_DATA='{"user_id":"manual-test-user","profile_id":"profile_child_b","student_name":"小明"}'
RUN_ID="run-$(date +%s)-$RANDOM"
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "请直接由升学指导专家分析小明的升学方向。", source: "toolbar",
  expert_context: {
    expert_team_id: "student_growth_expert_team", expert_id: "admission_specialist", operation: "select_expert"
  },
  enable_thinking: false, return_reasoning: false
}')
post_stream
```

`input`：这是切孩子与显式选择成员同时发生的写法：`context_activation:"auto"` 加
`select_expert`，同时传目标团队和目标专家 ID。

## 8. 未绑定孩子聊天

```bash
SESSION_ID="manual-unbound-$(date +%s)-$RANDOM"
RUN_ID="run-$(date +%s)-$RANDOM"
CONTEXT_DATA='{"user_id":"manual-test-user"}'
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "unbound", context_activation: "auto",
  content: "我想先了解如何帮孩子规划学习。", source: "chat",
  expert_context: {expert_team_id: null, expert_id: null, operation: "continue"}, enable_thinking: false, return_reasoning: false
}')
post_stream
```

`input`：未绑定孩子使用 `context_scope:"unbound"`；顶层 `context_data` 只能含 `user_id`。

## 9. 唯一转交候选，用户不点卡片而是回复“好的”

```bash
# 仅当当前 session 已有唯一且 active 的转交卡时执行。
# SESSION_ID、CONTEXT_DATA 沿用产生该卡的那次会话和孩子范围。
RUN_ID="run-$(date +%s)-$RANDOM"
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "好的，请继续。", source: "chat",
  expert_context: {expert_team_id: null, expert_id: null, operation: "continue"},
  enable_thinking: false, return_reasoning: false
}')
post_stream
```

`input`：这是普通 `chat + continue`，不传卡片 ID 或目标专家 ID，也不回传当前团队与专家。
唯一候选时服务端会按既有规则识别“好的”等确认文字；多候选时必须点击卡片。

## 10. 停止当前流式回答

```bash
# SESSION_ID 复用当前会话；RUN_ID 必须复用正在生成的那一轮，不能新建。
SESSION_ID='session_from_active_stream'
RUN_ID='run_from_active_stream'
CONTEXT_DATA='null'
INNER_INPUT=$(jq -nc '{action: "stop", source: "composer"}')
post_stream
```

`input`：`stop` 不带 `context_scope`、`expert_context`、`content` 或孩子信息。

## 11. `expert_context` 固定三字段规则

```json
// 所有普通追问：服务端继承当前 session Agent；若没有 Agent 即普通聊天
{"expert_team_id":null,"expert_id":null,"operation":"continue"}

// 兼容旧客户端的严格状态断言：仅在需要断言具体当前 Agent 时使用
{"expert_team_id":"student_growth_expert_team","expert_id":"当前实际承接专家ID","operation":"continue"}

// 用户明确选择专家团
{"expert_team_id":"student_growth_expert_team","expert_id":null,"operation":"select_team"}

// 用户明确选择专家团成员
{"expert_team_id":"student_growth_expert_team","expert_id":"admission_specialist","operation":"select_expert"}

// 用户直接选择独立专家
{"expert_team_id":null,"expert_id":"admission_specialist","operation":"select_expert"}

// 工具栏明确退出专家模式；本条聊天按 general_chat + Soul 执行
{"expert_team_id":null,"expert_id":null,"operation":"clear_expert"}

// 点击转交卡 / 工具栏团内切成员：断言切换前当前团队和专家
{"expert_team_id":"student_growth_expert_team","expert_id":"当前实际承接专家ID","operation":"continue"}
```

`expected_branch_version`、`expected_selection_version` 仍是可选断言字段，不属于固定三字段。
三个固定字段任意缺失都会返回 `422 EXPERT_CONTEXT_FIELDS_REQUIRED`。

## 12. 工具栏退出专家模式，并按普通聊天发送问题

```bash
# 点击“退出专家模式”后，下一条消息使用 clear_expert；这不是普通 continue。
RUN_ID="run-$(date +%s)-$RANDOM"
INNER_INPUT=$(jq -nc '{
  action: "chat", context_scope: "profile", context_activation: "auto",
  content: "我想先不限定专家，聊聊最近的感受。", source: "toolbar",
  expert_context: {expert_team_id: null, expert_id: null, operation: "clear_expert"},
  enable_thinking: false, return_reasoning: false
}')
post_stream
```

`clear_expert` 会清空当前 session 的专家团/专家承接者，并作废当前范围未完成的表单或 Skill 交互。
本条消息及之后的 `null/null/continue` 使用普通聊天；它与普通追问的双 `null` 语义不同。
