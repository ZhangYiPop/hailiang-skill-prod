# SSE v3 Harness 完整帧契约

> 协议：`hailiang.sse.v3`
> 产品入口：`POST /api/v3/sessions/chat/stream`
> 面向对象：BFF 与 App 前端

V3 的一帧是一个**完整助手气泡快照**。BFF 将最新帧整体替换暂存，前端以同一个 `assistant_message_id` 更新同一个气泡；不读取 Harness 调试接口、不拼接内部事件、不根据本地操作推断任务是否推进。

## 1. 对外关联 ID 与职责

| 字段 | 来源 | 含义 |
| --- | --- | --- |
| `session_id` | BFF | 会话 ID。请求和每个 SSE 帧原样一致。 |
| `run_id` | BFF | 一次用户任务的幂等与恢复键。BFF 创建；重连必须原样复用。字段名固定为 snake_case `run_id`，不是 `runId`。 |
| `assistant_message_id` | 算法服务 | 此次 Harness 任务的稳定助手气泡 ID。任务拆解、专家过程、表单、专家结论和最终正文都更新该气泡。 |
| `expert_team_id` | BFF | 除 `stop` 外每个 `input` 的必填团队选择器。兼容单个字符串；推荐传字符串数组，为多专家团预留。首次 `chat` 选择团队，后续表单回答和重连沿用同一选择。 |
| `expert_team_ids` | 算法服务 | 每帧回显的规范化团队选择数组。当前与 `expert_team_id` 一一对应；未来多团队编排启用后才可能含多个值。 |
| `team_orchestration_version` | 算法服务 | 本 run 的团队编排兼容版本。当前固定为 `single_team_v1`；旧记录缺失时也按该单团队语义解释。 |

`assistant_message_id` 不能把两个不同 `run_id` 合并；BFF 改用新 `run_id` 就是新任务和新气泡。算法不在产品帧中暴露 `client_run_id`、`harness_run_id`、数据库运行 ID 或真实节点 ID；这些只属于受控调试接口。

算法不提供点赞、点踩或分享能力。BFF 可以用 `session_id + run_id + assistant_message_id` 关联自己的消息和互动记录。

## 2. 请求动作

外层固定使用 `session_id`、`run_id`、序列化 JSON 的 `input` 与 `context_data`。App 经 BFF 调用，不直接理解专家、Skill、节点或版本。

```json
{"action":"chat","content":"帮孩子规划升学路径","expert_team_id":["student_growth_expert_team"]}
```

```json
{
  "action":"answer_input_request",
  "request_id":"input_req_xxx",
  "expert_team_id":["student_growth_expert_team"],
  "answers":{"province":"浙江"},
  "text":"补充说明",
  "skipped":false
}
```

停止仍走现有 BFF 停止接口（例如 `/api/v4/chat/stop`），由 BFF 使用原 `session_id`、`run_id` 转发：

```json
{"action":"stop","source":"composer"}
```

App 不直接调用 stream 入口的停止动作。`chat` 和 `answer_input_request` 缺少或改写 `expert_team_id` 均应被拒绝；要换专家团，创建新的 `run_id` 并发起新的 `chat`。

`expert_team_id` 的推荐类型为 `string[]`，例如 `["student_growth_expert_team"]`；历史 `string` 仍兼容。当前 `orchestrator_head` 只具备**单团队**编排能力，因此数组必须恰好含一个、去重后的合法团队 ID。传入多个团队将返回 `422 MULTI_EXPERT_TEAMS_NOT_ENABLED`，不会静默选择第一个；该限制将在多团队协调能力实现后移除。`stop` 不传该字段。

当前新 run 写入 `team_orchestration_version="single_team_v1"`。它只标记兼容语义，不代表当前已经执行多团队协作；旧 run 与旧对话快照缺失该字段时，客户端和服务端均按 `single_team_v1` 处理。未来启用真实多团队运行时才会引入新的版本值及团队级执行树，现有 `run_id`、`assistant_message_id` 和历史对话记录均不需要迁移。

## 3. 完整帧骨架

```json
{
  "protocol": "hailiang.sse.v3",
  "session_id": "session_001",
  "run_id": "bff_run_001",
  "assistant_message_id": "msg_xxx",
  "expert_team_id": "student_growth_expert_team",
  "expert_team_ids": ["student_growth_expert_team"],
  "team_orchestration_version": "single_team_v1",
  "record_version": 18,
  "cursor": 42,
  "reason": "task_started",
  "stream_status": "running",
  "outcome": null,
  "profile_id": "profile_001",
  "profile_name": "小海",
  "context_scope": "profile",
  "context_label": "小海",
  "context_notice": {
    "type": "profile_context_activated",
    "text": "本轮回答将结合 **小海** 的档案数据。"
  },
  "profile_context_status": "matched",
  "persistence": {
    "should_store_snapshot": true,
    "should_save_history": false,
    "replace_staged": false,
    "should_discard_staged": false,
    "terminal": false,
    "record_kind": "progress"
  },
  "state": {
    "status": "running",
    "phase": "experts_running",
    "action": "专家正在并行分析",
    "dispatch": {},
    "nodes": [],
    "inputs": [],
    "result": "",
    "recommendations_status": "not_started",
    "jump_cards": [],
    "safety": {"replacement_applied": false, "replacement_source": null}
  }
}
```

`record_version` 单调递增；收到版本不高于已渲染版本的帧可忽略。`reason` 是展示刷新原因，不能替代 `state` 的业务状态。

## 4. 专家树：如何知道某个专家正在做什么

`state.nodes` 是扁平的树表，通过 `id` 与 `parent_id` 建树：

```text
main（主编排器）
├─ expert（升学规划专家）
│  ├─ skill（选科分析能力）
│  │  └─ tool（读取档案 / 检索资料 / 请求补充）
│  └─ tool（专家直接调用的工具）
└─ expert（学习指导专家）
   └─ tool（读取档案）
```

前端应先按 `parent_id` 建索引，再将根节点和子节点递归渲染。`id` 是产品公开投影 ID，只可用于本帧树关联；不可传回算法，也不可当作真实数据库 ID 保存。

| `kind` | 含义 | 展示位置 |
| --- | --- | --- |
| `main` | 主编排器 / 最终汇总节点 | 根节点 |
| `expert` | 一个已分派的业务专家 | 主节点下 |
| `skill` | 专家或主编排器授权调用的专项能力 | 所属专家或主节点下 |
| `tool` | 当前节点的具体工具动作 | 所属 task / skill 下 |

每个节点的稳定公开字段如下：

| 字段 | 用途 |
| --- | --- |
| `title`、`actor` | 节点名称和执行者名称。 |
| `brief` | 面向用户的当前任务书摘要。专家的业务职责说明在 `agent_brief`，来源是业务人员维护的 agent brief，不是模型临时生成。 |
| `status` | `queued`、`running`、`waiting_for_user`、`waiting_for_dependency`、`completed`、`failed`、`cancelled`。 |
| `phase`、`action` | 当前阶段与可直接展示的业务动作；展示“正在执行：{action}”，不要显示抽象的“等待任务结果”。 |
| `waiting_on` | 等待的公开节点 ID；据此展示“等待某专家/步骤”。 |
| `children_count`、`completed_children_count` | 本节点直接子步骤数量及完成数。 |
| `attempts`、`error` | 工具的重试次数及公开失败原因。 |
| `result` | 已完成专家的结构化结论；未完成时为 `null`。 |
| `outcome` | `completed`、`partial` 或 `failed`；与技术状态分开表达业务结果。 |

专家完成后，前端即可用 `node.result` 展示专家结论卡；不要等待最终总结。`result` 的固定形状为：

```json
{
  "conclusion": "结论正文",
  "findings": ["可选关键发现"],
  "limitations": ["适用限制"],
  "unresolved": ["仍未解决事项"]
}
```

首版专家结论至少保证 `conclusion`；数组不存在或为空时不渲染对应小节。专家结论是整段提交，不逐字流式。最终主回复也以完整 `state.result` / `done` 帧更新；当前 V3 不提供面向产品的 `message.delta`。

## 5. 编排首分派、汇总与推荐何时展示

`state.dispatch` 是“编排首已经规划的任务”，`nodes` 是“实际已创建并执行的节点”。两者必须同时使用，不能把已经计划误展示为已经运行。

```json
{
  "status": "completed",
  "planned_task_count": 2,
  "planned_expert_count": 2,
  "parallel_task_count": 2,
  "experts": [
    {
      "node_id": "node_2",
      "expert_id": "academic_coach",
      "name": "学习指导师",
      "brief": "分析学习习惯并提出改进方案",
      "agent_brief": "负责学习规划与习惯培养",
      "status": "assigned",
      "depends_on": []
    }
  ]
}
```

| 完整帧中的条件 | 可以展示的内容 |
| --- | --- |
| `state.phase=planning` 且 `dispatch.status` 不是 `completed` | “正在理解问题并拆分任务”。 |
| `dispatch.status=completed` | “已拆分为 N 个专家任务”、每个专家名、任务 `brief`、依赖和可并行数量。此时专家可以仍是 `assigned/queued`。 |
| 专家节点 `status=running` 或 `phase=analyzing/using_skill/using_tool` | 专家执行树，以及该节点 `action`。 |
| 节点 `waiting_for_dependency` | `waiting_on` 对应的专家或步骤。 |
| 节点 `waiting_for_user` 或当前活动表单存在 | “等待你补充信息”和表单；其他仍在运行的专家继续显示。 |
| 专家节点有 `result` | 该专家的结论卡，并显示“等待编排器汇总”直到 `state.phase=synthesizing` 或最终完成。 |
| `state.phase=synthesizing` | “编排器正在汇总专家结论”。 |
| `state.recommendations_status=running` | “正在生成相关推荐”。 |
| `state.recommendations_status=completed` 且 `jump_cards` 非空 | 渲染推荐卡。 |
| `state.status=completed` 或 `event: done` | 最终正文、最终专家结论、推荐和完整任务记录。 |

## 6. `jump_cards` 推荐卡

`state.jump_cards` 是编排尾在生成最终回复后输出的推荐卡数组，对旧推荐路由决策表做公开投影。它不是任务树节点，也不依赖任何内部路由接口；前端直接按数组顺序渲染即可。

```json
{
  "recommendations_status": "completed",
  "jump_cards": [
    {
      "id": "rec_5ffc06b319fc6d1f",
      "title": "学科潜能测评",
      "description": "了解物理、化学和技术学科的匹配度",
      "reason": "可先完成测评，再结合结果调整选科策略。",
      "intent": "assessment",
      "object_type": "single_assessment",
      "sub_type": "subject_aptitude",
      "filter": {
        "region": "浙江",
        "grade": "高一",
        "keyword": "",
        "category": ""
      }
    }
  ]
}
```

| 字段 | 含义 | 前端处理 |
| --- | --- | --- |
| `id` | 服务端生成的稳定推荐卡 ID | 列表 key 与 BFF 记录关联键。 |
| `title`、`description` | 卡片标题和补充说明 | 直接展示；`description` 可为空。 |
| `reason` | 本轮建议该卡的原因 | 展示为推荐理由；不能当作最终正文的一部分重复拼接。 |
| `intent` | 推荐意图，例如 `assessment` | 传给现有推荐跳转逻辑。 |
| `object_type` | 跳转对象类型，例如 `single_assessment` | 与既有推荐路由的对象类型一一对应。 |
| `sub_type` | 对象子类型，例如 `subject_aptitude` | 与既有推荐路由的子类型一一对应。 |
| `filter` | `{region, grade, keyword, category}` 查询条件 | 原样传给既有推荐页；空字符串表示不限制该维度。 |

`recommendations_status` 的渲染规则：

| 值 | 说明 | UI |
| --- | --- | --- |
| `not_started` | 编排尾尚未进入推荐阶段 | 不展示推荐区。 |
| `running` | 正在生成推荐 | 展示“正在生成相关推荐”。 |
| `completed` | 推荐成功完成 | 渲染非空 `jump_cards`。 |
| `empty` | 推荐过程完成但没有适用卡片 | 不展示推荐区。 |
| `failed` | 推荐生成失败 | 不影响最终回答或专家结论；不展示推荐区。 |
| `not_available` / `unavailable` | 当前专家团或环境未接通推荐能力 | 不展示推荐区。 |

只有 `recommendations_status="completed"` 且 `jump_cards.length > 0` 时展示卡片。完整帧后续更新会替换整个数组，前端不要根据上一次帧追加或保留已消失的推荐卡。

## 7. 表单字段与问题组

当前可填写卡片由活动输入请求决定，使用其 `id` 作为 `request_id`。字段兼容现有 `AIChatDynamicFormField` 的核心形状：

```json
{
  "key":"province",
  "fact_key":"student_province",
  "label":"所在省份",
  "input_type":"single_select",
  "options":[{"label":"浙江","value":"浙江"}],
  "required":true,
  "scope":"profile",
  "min":null,
  "max":null
}
```

- `fact_key` 缺省时使用 `key`；`options` 固定为 `{label,value}` 数组。
- 服务端维护问题组和跨专家复用：同一事实身份的答案只填写一次，所有匹配专家得到相同工具结果。
- 提交可为部分填写；空字段会被服务端作为本次跳过字段处理，不覆盖已有事实。全空提交等同跳过。
- 前端只展示服务端声明为 `pending` 的活动卡片；不要自行切换同组下一题或下一组。

## 7. 事件、保存、超时与恢复

| 事件 | 条件 | BFF 行为 |
| --- | --- | --- |
| `state` | 任务、分派、节点、表单、结论、画像或正文更新 | 覆盖暂存完整快照，不写正式历史。 |
| `done_suspended` | 有活动表单，且没有其他可运行节点 | 用完整快照保存为 `waiting_for_user`。 |
| `done_timeout` | 到达 BFF 的有限连接等待边界 | 用完整快照保存为 `running`。后台可能仍在执行；本期不自动续连。 |
| `done` | 完成、受限完成、失败或停止 | 按 `persistence` 保存最终记录；停止时丢弃暂存，不写历史。 |

BFF 需要保存：最新完整快照、原始 `chat` 请求和 `run_id`。App 被系统杀进程后回到会话，BFF 用原始请求和相同 `run_id` 重连流入口，服务端先返回此任务的最新完整快照并避免重复分派。前端不需要了解 Harness 查询接口。

`done_timeout` 没有自动续接次数：**本期为 0 次**。它只是让 BFF 在自己的时限前可落库的检查点；UI 保持同一气泡为“仍在处理中”，等待 App 下次进入会话触发 BFF 重连。

出现表单不必然发 `done_suspended`：只要仍有其他专家或工具可运行，流继续以 `state` 推送。只有没有可运行节点、活动表单等待用户时才结束本次有限流并发送 `done_suspended`。

## 8. 个性化与风控

V3 沿用 V2 的 `profile_id`、`profile_name`、`context_scope`、`context_label`、`context_notice` 和 `profile_context_status`。仅在：

```text
context_scope = "profile" && profile_context_status = "matched"
```

时展示“本次回答结合某孩子的个性化数据”。`context_notice.text` 是独立提示，不拼入正文。

用户可见最终输出被审核拦截时，算法先取阿里云 `Data.Advice[].Answer` 中第一条非空代答；没有代答则使用项目安全兜底文案。仍发送可保存的 `done`：

```json
{
  "outcome":"partial",
  "state":{
    "result":"安全代答或项目兜底文案",
    "safety":{"replacement_applied":true,"replacement_source":"aliyun_advice"}
  }
}
```

不要以 `message.status=blocked` 推断风控，不公开命中词、原始模型文本、审核原始响应或内部推理。

## 9. 2026-10-21 测试服务器验证结果

以 `POST http://10.30.6.45:8011/api/v3/sessions/chat/stream` 使用独立会话进行真实验证：首轮出现身份表单；提交后，服务端确实产生了 `main → expert → tool` 层级、编排首 `dispatch`、专家 `brief/action` 和输入组。

但该测试环境返回的是部署前兼容格式：顶层使用 `snapshot`，并外露 `harness_run_id`、`client_run_id` 和真实节点 ID，且 `run_id` 被替换为内部运行 ID；其 `stop` 输入还要求 `expert_team_id`，并拒绝本文约定的 `source:"composer"`。这与本文第 1–3 节的最小 V3 产品契约不一致，**不能让前端按该现网帧接入**。部署包含最小入口适配器的版本后，应重新执行同一回归；验收条件是：只出现本文规定的三个对外关联 ID，完整数据位于 `state`，保持 `run_id` 原样回显，并接受本文第 2 节的 stop 输入。
