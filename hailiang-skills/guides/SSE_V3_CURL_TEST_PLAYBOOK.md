# SSE v3 `chat/stream` cURL 联调手册

> 算法入口：`POST {API_BASE}/api/v3/sessions/chat/stream`
> 调用方：BFF。App 只调用 BFF，BFF 负责注入可信的 `context_data` 后按本文调用算法服务。

本文所有命令只使用 V3 最小对话入口，不调用 `/harness/*` 调试、快照、回答或取消接口。

## 1. 准备

```bash
export API_BASE="http://127.0.0.1:8016"
export SESSION_ID="sse-v3-curl-$(date +%s)"
export RUN_ID="bff-run-$(date +%s)"
export TEAM_ID="student_growth_expert_team"
export USER_ID="sse-v3-curl-user"
export PROFILE_ID="sse-v3-curl-profile"
```

V3 默认通过 Harness 多 Agent 运行时执行；它与 Workbench 候选修订手动测试
共用同一套编排、专家自动分派与 Skill 执行能力，同时保留本手册中的 V3 请求体
和 SSE 完整帧协议。生产环境还应保持
`HAILIANG_SCRIPT_EXECUTOR=remote` 与
`HAILIANG_REQUIRE_REMOTE_SANDBOX=true`，确保 Skill 脚本不回退至 API 进程。

- `SESSION_ID`：BFF 的会话 ID。
- `RUN_ID`：BFF 为一次 HTTP 请求创建的幂等键。重试同一请求时复用；回答和恢复可生成新的值。`assistant_message_id` 才是同一任务和气泡的稳定关联键。
- `TEAM_ID`：本轮选择的专家团。`chat` 与 `answer_input_request` 必传；本文使用推荐的单元素数组形式。`stop` 不传。
- 当前编排头只支持一个团队。接口已预留数组类型；传两个或更多团队会返回 `422 MULTI_EXPERT_TEAMS_NOT_ENABLED`，不会只取第一个。
- 首帧和后续完整帧均应回显 `team_orchestration_version="single_team_v1"`；缺少该字段的历史记录也按此语义处理。
- `context_data`：BFF 从登录态、档案服务注入；App 不应自行构造。

下面用一个小工具从 SSE 文件提取完整 JSON 帧：

```bash
frames() { sed -n 's/^data: //p' "$1"; }
```

## 2. 新问题：规划、首帧、身份表单

```bash
cat >/tmp/sse-v3-chat.json <<JSON
{
  "session_id":"$SESSION_ID",
  "run_id":"$RUN_ID",
  "input":"{\"action\":\"chat\",\"content\":\"请为一名高一学生规划选科和学习方向。\",\"expert_team_id\":[\"$TEAM_ID\"]}",
  "context_data":{
    "user_id":"$USER_ID",
    "profile_id":"$PROFILE_ID",
    "profile_data":{"name":"联调学生","grade":"高一","province":"浙江","interests":["物理","机器人"]}
  }
}
JSON

curl -sS -N --max-time 620 \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  --data-binary @/tmp/sse-v3-chat.json \
  "$API_BASE/api/v3/sessions/chat/stream" | tee /tmp/sse-v3-chat.sse
```

检查首轮帧：

```bash
frames /tmp/sse-v3-chat.sse | jq -c '{run_id, assistant_message_id, expert_team_id, expert_team_ids, team_orchestration_version, stream_status, reason, state:{status:.state.status,phase:.state.phase,action:.state.action,node_count:(.state.nodes|length),input_count:(.state.inputs|length)}}'
```

预期：

- 每个帧的 `run_id` 都等于 `$RUN_ID`。
- 不出现 `harness_run_id`、`client_run_id`、`snapshot` 或真实 `task_*` / `tool_*` 节点 ID。
- 节点使用 `node_1` 等公开 ID；关系通过 `state.nodes[].parent_id` 表示。
- 若身份信息尚不完整，最后一帧是 `event: done_suspended`，且 `state.inputs[]` 中有一个 `status="pending"` 的活动卡片。

取得当前表单 ID：

```bash
export ASSISTANT_MESSAGE_ID="$(frames /tmp/sse-v3-chat.sse | jq -r '.assistant_message_id' | tail -1)"
export REQUEST_ID="$(frames /tmp/sse-v3-chat.sse | jq -r '.state.inputs[]? | select(.status=="pending") | .id' | tail -1)"
echo "$ASSISTANT_MESSAGE_ID / $REQUEST_ID"
```

## 3. 回答多字段表单：恢复、分派专家、查看执行树

多字段表单只填写 `answers`；不要同时传 `text`。`text` 只适合单个文本字段，否则服务端会返回 `TEXT_REQUIRES_SINGLE_FIELD`。

```bash
cat >/tmp/sse-v3-answer.json <<JSON
{
  "session_id":"$SESSION_ID",
  "run_id":"${RUN_ID}-answer-1",
  "input":"{\"action\":\"answer_input_request\",\"assistant_message_id\":\"$ASSISTANT_MESSAGE_ID\",\"request_id\":\"$REQUEST_ID\",\"expert_team_id\":[\"$TEAM_ID\"],\"answers\":{\"user_role\":\"家长\",\"school_stage\":\"高中\"},\"skipped\":false}",
  "context_data":{"user_id":"$USER_ID","profile_id":"$PROFILE_ID"}
}
JSON

curl -sS -N --max-time 620 \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  --data-binary @/tmp/sse-v3-answer.json \
  "$API_BASE/api/v3/sessions/chat/stream" | tee /tmp/sse-v3-answer.sse
```

查看编排首已经拆分的专家和当前树：

```bash
frames /tmp/sse-v3-answer.sse | jq -c '
  select(.state.dispatch != null) |
  {phase:.state.phase, action:.state.action,
   dispatch:.state.dispatch,
   nodes:[.state.nodes[] | {id,parent_id,kind,actor,title,phase,action,status,waiting_on,children_count,completed_children_count}]}'
```

预期：

- `state.dispatch.status="completed"` 时可展示已分派专家、`brief`、`task_brief`、依赖与并行信息。
- `state.nodes` 形成 `main → expert → skill/tool` 树。
- 专家运行中查看该专家节点的 `phase/action`；等待依赖时看 `waiting_on`；完成后直接渲染其 `result`。
- 若本次有限流先结束，最后一帧为 `done_timeout`；保存该完整帧为 `running`，不要新建任务。

## 4. 任务表单：部分回答与全部跳过

如果专家又创建了表单，从最新文件取得 ID：

```bash
export REQUEST_ID="$(frames /tmp/sse-v3-answer.sse | jq -r '.state.inputs[]? | select(.status=="pending") | .id' | tail -1)"
```

部分回答。空白字段由服务端当作本次跳过字段，系统会复用相同事实身份给其他专家：

```bash
cat >/tmp/sse-v3-partial.json <<JSON
{
  "session_id":"$SESSION_ID",
  "run_id":"${RUN_ID}-partial-1",
  "input":"{\"action\":\"answer_input_request\",\"assistant_message_id\":\"$ASSISTANT_MESSAGE_ID\",\"request_id\":\"$REQUEST_ID\",\"expert_team_id\":[\"$TEAM_ID\"],\"answers\":{\"province\":\"浙江\"},\"skipped\":false}",
  "context_data":{"user_id":"$USER_ID","profile_id":"$PROFILE_ID"}
}
JSON

curl -sS -N --max-time 620 -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  --data-binary @/tmp/sse-v3-partial.json "$API_BASE/api/v3/sessions/chat/stream" | tee /tmp/sse-v3-partial.sse
```

全部跳过当前卡片：

```bash
cat >/tmp/sse-v3-skip.json <<JSON
{
  "session_id":"$SESSION_ID",
  "run_id":"${RUN_ID}-skip-1",
  "input":"{\"action\":\"answer_input_request\",\"assistant_message_id\":\"$ASSISTANT_MESSAGE_ID\",\"request_id\":\"$REQUEST_ID\",\"expert_team_id\":[\"$TEAM_ID\"],\"answers\":{},\"skipped\":true}",
  "context_data":{"user_id":"$USER_ID","profile_id":"$PROFILE_ID"}
}
JSON

curl -sS -N --max-time 620 -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  --data-binary @/tmp/sse-v3-skip.json "$API_BASE/api/v3/sessions/chat/stream" | tee /tmp/sse-v3-skip.sse
```

服务端负责激活同组下一题、下一问题组和恢复绑定节点；调用方只用最新完整帧替换气泡。

查看问题组和活动卡片：

```bash
frames /tmp/sse-v3-partial.sse | jq -c '
  {status:.state.status, queue:.state.input_queue,
   groups:[.state.input_groups[] | {id,status,request_ids,active_request_id,total_count,completed_count,actors}],
   cards:[.state.inputs[] | {id,title,status,actor,round,group_id,group_position,field_count:(.fields|length)}]}'
```

用最新完整帧取得活动卡片，而不是取列表中第一个历史请求：

```bash
export REQUEST_ID="$(frames /tmp/sse-v3-partial.sse | jq -r 'select(.state.input_queue.active_request_id != null) | .state.input_queue.active_request_id' | tail -1)"
```

验证推进时按以下规则检查：

- `input_queue.active_request_id` 对应 `inputs[].id`，且该输入状态为 `pending`，这才是目前可以提交的卡片。
- 同一活动组里若还有排队卡片，提交后它会成为新的 `active_request_id`；只展示这张新激活的卡片。
- 当前组的 `total_count` / `completed_count` 是卡片数。每张卡片的字段数由对应的 `fields | length` 决定；`pending_count` / `queued_count` 也不是字段数。
- 当前组完成后，最早的 `queued` 组提升为 `active`。对应专家收到答案后继续分析，可能结束，也可能再创建一轮问题组；全 run 没有预先固定的总题数。
- 即使当前活动卡片还在等待，只要其他专家仍可执行，仍会收到 `state`。应在每个完整帧检查队列，不要只在 `done_suspended` 后更新界面。

## 5. `done_timeout` 后恢复同一任务

不调用任何查询接口。使用保存的 `assistant_message_id` 和新的请求 `run_id` 发 `resume`：

```bash
cat >/tmp/sse-v3-resume.json <<JSON
{
  "session_id":"$SESSION_ID",
  "run_id":"${RUN_ID}-resume-1",
  "input":"{\"action\":\"resume\",\"assistant_message_id\":\"$ASSISTANT_MESSAGE_ID\",\"expert_team_id\":[\"$TEAM_ID\"]}",
  "context_data":{"user_id":"$USER_ID","profile_id":"$PROFILE_ID"}
}
JSON
curl -sS -N --max-time 620 -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  --data-binary @/tmp/sse-v3-resume.json "$API_BASE/api/v3/sessions/chat/stream" | tee /tmp/sse-v3-reconnect.sse
```

预期：第一帧回显新的请求 `run_id`，但仍为同一个 `assistant_message_id`；已经出现的 `dispatch`、节点、表单和专家结论仍在 `state`，不会重复分派专家。

本地要快速复现超时，可在启动算法服务前设置：

```bash
export HAILIANG_HARNESS_SSE_WAIT_SECONDS=12
./run.sh
```

生产默认是 600 秒；`done_timeout` 不是失败，也不会自动继续拉流。

## 6. 完成与推荐卡

任务完成时最后一帧为 `event: done`。检查最终结果、专家结论和推荐：

```bash
frames /tmp/sse-v3-reconnect.sse | jq -c '
  select(.outcome != null) |
  {outcome, persistence,
   result:.state.result,
   expert_results:[.state.nodes[] | select(.kind=="expert" and .result!=null) | {actor,result}],
   recommendations_status:.state.recommendations_status,
   jump_cards:.state.jump_cards}'
```

- `outcome=completed`：保存最终对话记录。
- `outcome=partial`：保存受限完成的正文与限制。
- `outcome=failed`：保存失败结果。
- `recommendations_status=completed` 且 `jump_cards` 非空时渲染推荐卡。

## 7. 停止当前任务

停止请求不传 `expert_team_id`：

```bash
cat >/tmp/sse-v3-stop.json <<JSON
{
  "session_id":"$SESSION_ID",
  "run_id":"$RUN_ID",
  "input":"{\"action\":\"stop\",\"source\":\"composer\"}",
  "context_data":{"user_id":"$USER_ID","profile_id":"$PROFILE_ID"}
}
JSON

curl -sS -N --max-time 30 -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  --data-binary @/tmp/sse-v3-stop.json "$API_BASE/api/v3/sessions/chat/stream" | tee /tmp/sse-v3-stop.sse
```

预期最后一帧：

```text
event: done
outcome: cancelled
persistence.should_discard_staged: true
```

BFF 丢弃暂存，不保存该任务历史。

## 8. 新问题中断旧任务

新问题使用新的 `run_id`；服务端取消旧运行后创建新任务。旧任务的中间推理、工具结果和未完成正文不进入新任务上下文。

```bash
export NEXT_RUN_ID="bff-run-next-$(date +%s)"
sed "s/$RUN_ID/$NEXT_RUN_ID/g" /tmp/sse-v3-chat.json > /tmp/sse-v3-next-chat.json
curl -sS -N --max-time 620 -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  --data-binary @/tmp/sse-v3-next-chat.json "$API_BASE/api/v3/sessions/chat/stream"
```
