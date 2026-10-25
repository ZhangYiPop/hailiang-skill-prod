# `/api/v3/sessions/chat/stream` 状态字段说明

本文是 `hailiang.sse.v3` 对话流的前端消费契约。适用于对话测试台、业务调试台和其他浏览器/BFF 客户端。

## 1. 传输帧

客户端通过 SSE 接收帧：

```text
event: state
data: { ... }

event: ping
data: {}

event: done
data: { ... }
```

`state` 是运行中的完整产品状态快照；收到后应替换当前运行快照，不要只合并顶部字段。`ping` 只用于连接保活，不改变业务状态。`done` 表示本次流的终态或挂起/超时结果，仍应先消费其中的最终 `state`。

每个业务帧的公共字段包括：

| 字段 | 含义 |
| --- | --- |
| `protocol` | 固定为 `hailiang.sse.v3` |
| `session_id` | 会话 ID |
| `run_id` | 本次 HTTP 请求对应的运行 ID |
| `assistant_message_id` | 助手消息/气泡的稳定 ID |
| `cursor` | SSE 状态游标，用于断线续读 |
| `reason` | 本次状态变化原因 |
| `stream_status` | SSE 流状态，不等同于某个专家的任务状态 |
| `outcome` | 业务结果，未结束时通常为 `null` |
| `state` | 本文第 2 节定义的业务状态 |

## 2. 层级关系

状态树如下：

```text
state                         本轮对话/主任务
└── state.nodes[kind=main]    主任务节点
    ├── state.nodes[kind=expert]  专家节点
    │   └── state.nodes[kind=skill] Skill 子任务
    │       └── state.nodes[kind=tool] 工具节点
    └── state.nodes[kind=tool]    主任务直接调用的工具
```

前端必须按节点层级读取状态：

- 顶部总进度使用 `state.status`、`state.phase`、`state.action`。
- 专家卡片使用对应 `kind=expert` 节点的 `status`、`phase`、`action`。
- Skill 进度使用对应 `kind=skill` 节点的 `status`、`phase`、`action`。
- 工具明细使用对应 `kind=tool` 节点的 `status`、`title`、`error`。

禁止把主任务的 `state.action` 或 `mainNode.action` 复制到专家卡片。主任务可以显示“正在等待学习指导师、家庭教育专家顾问完成专项分析”，但学习指导师卡片必须显示自己的 `expertNode.action`，例如“正在等待「学习习惯分析」完成”。

## 3. `state` 顶层字段

| 字段 | 用途 | 权威性 |
| --- | --- | --- |
| `status` | 整个运行的生命周期状态 | 总体状态 |
| `phase` | 主编排阶段 | 顶部进度 |
| `action` | 主任务当前动作说明 | 仅顶部总进度 |
| `synthesis_status` | 专家结果汇总阶段：`pending` / `running` / `completed` | 顶部汇总提示 |
| `recommendations_status` | 推荐步骤状态 | 推荐区域 |
| `dispatch` | 专家分派计划和任务目标 | 计划信息，不是最细实时状态 |
| `round_experts` | 专家 ID 分组统计 | 头像、数量、总体状态 |
| `nodes` | 主任务、专家、Skill、工具的实时节点树 | **子任务状态唯一优先来源** |
| `inputs` | 表单/追问请求 | 输入卡片 |
| `input_queue` | 表单队列摘要 | 输入队列 |
| `result` | 最终或阶段性回答文本 | 消息正文 |
| `error` | 公开安全错误 | 错误提示 |

## 4. `state.nodes[]` 字段

节点示例：

```json
{
  "id": "node_4",
  "parent_id": "node_1",
  "kind": "expert",
  "expert_id": "academic_coach",
  "title": "学习指导师",
  "status": "waiting_for_dependency",
  "phase": "waiting_for_dependency",
  "action": "正在等待「学习习惯分析」完成",
  "result": null,
  "error": null,
  "started_at": "2026-10-25T05:14:47+00:00",
  "ended_at": null,
  "waiting_on": [],
  "children_count": 1,
  "completed_children_count": 0
}
```

字段规则：

| 字段 | 说明 |
| --- | --- |
| `id` | 当前公开快照中的节点 ID；用于父子关系和 `waiting_on` |
| `parent_id` | 父节点 ID；专家通常指向主节点，Skill 指向专家节点 |
| `kind` | `main`、`expert`、`skill` 或 `tool` |
| `expert_id` | 专家稳定 ID；专家卡片应优先使用它做 key |
| `title` | 面向用户的节点名称 |
| `status` | 生命周期状态 |
| `phase` | 面向产品的阶段状态 |
| `action` | 当前节点自己的可展示动作文案 |
| `result` | 节点已提交的结构化结果；未完成时为 `null` |
| `error` | 节点公开错误 |
| `waiting_on` | 当前节点依赖的节点 ID |
| `children_count` | 子任务总数 |
| `completed_children_count` | 已结束子任务数 |
| `started_at` / `ended_at` | 节点开始/结束时间 |

生命周期状态：

```text
queued                    等待领取执行
running                   正在执行
waiting_for_dependency    等待自己的子任务或依赖
waiting_for_user          等待用户输入
completed                 已完成
failed                    执行失败
cancelled                 已取消
```

`waiting_for_dependency` 只说明该节点暂时不能继续，不代表一定在等待“其他专家”。具体等待对象必须读取同一个节点的 `action`、`waiting_on` 和子节点。

## 5. 专家卡片的推荐读取方式

```ts
const nodes = state.nodes ?? [];
const main = nodes.find(node => node.kind === "main");

const experts = nodes.filter(node =>
  node.kind === "expert" && node.parent_id === main?.id
);

const cards = experts.map(expert => {
  const activeChild = nodes.find(node =>
    node.parent_id === expert.id &&
    !["completed", "failed", "cancelled"].includes(node.status)
  );

  return {
    key: expert.expert_id,
    name: expert.title,
    status: expert.status,
    phase: expert.phase,
    // 子任务有更具体的动作时可以展示子任务动作；绝不能使用 state.action。
    action: activeChild?.action ?? expert.action,
    result: expert.result,
    error: expert.error,
  };
});
```

如果同一个 `expert_id` 因重试出现多个节点，按 `created_at` 取最新节点；不要按 `nodes` 数组下标作为专家身份。

## 6. `dispatch` 与 `round_experts` 的边界

`state.dispatch.experts[]` 用于表示“邀请/分派了谁、任务目标是什么”：

```json
{
  "expert_id": "academic_coach",
  "name": "学习指导师",
  "plan_status": "assigned",
  "execution_status": "assigned",
  "node_id": "node_4"
}
```

它适合初始化专家列表，但实时动作以 `state.nodes[kind=expert]` 为准。

`state.round_experts` 只提供聚合统计，例如：

```json
{
  "assigned_expert_ids": ["academic_coach", "family_edu_specialist"],
  "running_expert_ids": ["academic_coach"],
  "waiting_expert_ids": [],
  "completed_expert_ids": ["family_edu_specialist"],
  "failed_expert_ids": [],
  "status": "running"
}
```

它可以用于专家头像、完成数量和专家团总体状态，不能用于显示某个专家当前正在做什么。

## 7. 顶部与专家卡片的正确示例

```text
顶部主任务：正在等待学习指导师完成专项分析

学习指导师：正在等待「学习习惯分析」完成
家庭教育专家顾问：已提交专家结论
```

错误实现：

```ts
// 错误：把主任务动作下沉到所有专家卡片
expertCard.action = state.action;
```

正确实现：

```ts
// 正确：每张卡片读取自己的 expert 节点
expertCard.action = expertNode.action;
```

## 8. 结束帧和断线恢复

- `event: ping`：忽略业务更新，仅刷新连接保活计时器。
- `event: state`：替换当前状态快照，并按 `cursor` 更新本地游标。
- `event: done`：先保存其中的最终状态，再依据 `outcome` 结束本次流。
- 断线重连时使用服务端约定的 `cursor`/重放机制，不要从 `state.action` 推断专家是否完成。

最重要的规则是：**顶部看 `state`，专家看对应 `nodes[kind=expert]`，Skill 看对应 `nodes[kind=skill]`；任何子节点都不能回退使用主任务的 `action`。**

## 9. 为什么会出现“专家卡片显示错文案”

一次多专家运行至少有两个同时存在的状态：

1. 主任务状态：负责分派专家、等待专家结果、汇总和最终回复。
2. 专家状态：负责该专家自己的分析、Skill、工具和追问。

例如：

```text
主任务：等待学习指导师和家庭教育专家顾问完成
├── 学习指导师：等待自己的“学习习惯分析” Skill
└── 家庭教育专家顾问：已经完成
```

此时三个节点可能分别是：

```json
[
  {
    "id": "node_1",
    "parent_id": null,
    "kind": "main",
    "status": "waiting_for_dependency",
    "phase": "waiting_for_dependency",
    "action": "正在等待学习指导师完成专项分析"
  },
  {
    "id": "node_4",
    "parent_id": "node_1",
    "kind": "expert",
    "expert_id": "academic_coach",
    "status": "waiting_for_dependency",
    "phase": "waiting_for_dependency",
    "action": "正在等待「学习习惯分析」完成"
  },
  {
    "id": "node_5",
    "parent_id": "node_1",
    "kind": "expert",
    "expert_id": "family_edu_specialist",
    "status": "completed",
    "phase": "completed",
    "action": "已提交专家结论"
  }
]
```

如果前端在渲染每一张专家卡片时写成：

```ts
card.action = state.action;
```

就会把主任务的等待文案错误显示在学习指导师卡片中。正确写法是：

```ts
card.action = expertNode.action;
```

如果要展示专家正在执行的子任务，则使用该专家节点的直接子节点：

```ts
card.action = activeChild?.action ?? expertNode.action;
```

## 10. 字段读取优先级

前端或 AI 生成的 UI 逻辑应遵守下面的优先级：

### 顶部总进度

```text
state.action
  ↓ 没有文案时
state.phase
  ↓ 仍没有文案时
根据 state.status 显示通用兜底
```

### 专家卡片

```text
对应 expertNode.action
  ↓ 没有文案时
对应 expertNode.phase
  ↓ 仍没有文案时
根据 expertNode.status 显示通用兜底
```

### Skill 卡片

```text
对应 skillNode.action
  ↓ 没有文案时
对应 skillNode.phase
  ↓ 仍没有文案时
根据 skillNode.status 显示通用兜底
```

不能采用下面的回退链：

```text
expertNode.action → mainNode.action → state.action
```

主任务文案不能作为子专家的状态兜底，否则会再次发生层级串用。

## 11. 一个完整的多专家状态快照

前端可以按下面的结构理解一次快照：

```json
{
  "protocol": "hailiang.sse.v3",
  "session_id": "session-001",
  "run_id": "request-run-001",
  "assistant_message_id": "assistant-message-001",
  "cursor": 18,
  "reason": "updated",
  "stream_status": "running",
  "state": {
    "status": "running",
    "phase": "waiting_for_dependency",
    "action": "正在等待学习指导师完成专项分析",
    "synthesis_status": "pending",
    "round_experts": {
      "assigned_expert_ids": ["academic_coach", "family_edu_specialist"],
      "running_expert_ids": ["academic_coach"],
      "waiting_expert_ids": [],
      "completed_expert_ids": ["family_edu_specialist"],
      "failed_expert_ids": [],
      "status": "running"
    },
    "nodes": [
      {
        "id": "node_1",
        "parent_id": null,
        "kind": "main",
        "title": "本轮回答",
        "status": "waiting_for_dependency",
        "phase": "waiting_for_dependency",
        "action": "正在等待学习指导师完成专项分析",
        "result": null
      },
      {
        "id": "node_4",
        "parent_id": "node_1",
        "kind": "expert",
        "expert_id": "academic_coach",
        "title": "学习指导师",
        "status": "running",
        "phase": "using_skill",
        "action": "正在执行学习习惯分析",
        "result": null
      },
      {
        "id": "node_5",
        "parent_id": "node_1",
        "kind": "expert",
        "expert_id": "family_edu_specialist",
        "title": "家庭教育专家顾问",
        "status": "completed",
        "phase": "completed",
        "action": "已提交专家结论",
        "result": {
          "status": "completed",
          "conclusion": "已完成家庭沟通和边界方案"
        }
      },
      {
        "id": "node_6",
        "parent_id": "node_4",
        "kind": "skill",
        "expert_id": "academic_coach",
        "title": "学习习惯分析",
        "status": "running",
        "phase": "using_skill",
        "action": "正在执行专项 Skill",
        "result": null
      }
    ]
  }
}
```

这份快照在 UI 中应渲染为：

```text
顶部：正在等待学习指导师完成专项分析

学习指导师：正在执行学习习惯分析
家庭教育专家顾问：已提交专家结论
```

## 12. 节点筛选与稳定身份

推荐使用 `expert_id` 作为专家业务身份，使用 `id` 和 `parent_id` 建立当前快照内的父子关系：

```ts
const terminal = new Set(["completed", "failed", "cancelled"]);
const nodes = state.nodes ?? [];
const main = nodes.find(node => node.kind === "main");

const expertNodes = nodes
  .filter(node => node.kind === "expert" && node.parent_id === main?.id)
  .sort((a, b) => String(a.created_at ?? "").localeCompare(String(b.created_at ?? "")));

const latestByExpert = new Map<string, Node>();
for (const node of expertNodes) {
  if (node.expert_id) latestByExpert.set(node.expert_id, node);
}

for (const expert of latestByExpert.values()) {
  const children = nodes.filter(node => node.parent_id === expert.id);
  const activeChild = children.find(node => !terminal.has(node.status));
  renderExpertCard({
    key: expert.expert_id,
    title: expert.title,
    status: expert.status,
    phase: activeChild?.phase ?? expert.phase,
    action: activeChild?.action ?? expert.action,
    result: expert.result,
    error: expert.error,
  });
}
```

说明：

- `nodes` 数组顺序不是专家身份，不要使用 `nodes[0]`、`nodes[1]` 绑定头像。
- 同一个专家因重试可能出现多个节点，取该 `expert_id` 最新创建的节点。
- `node.id` 只保证当前公开快照中的父子引用有效；跨快照保存业务身份时使用 `expert_id`。
- `dispatch.experts[].node_id` 可以帮助初始化映射，但实时渲染仍以 `nodes` 为准。

## 13. 状态转换与 UI 行为

| 节点状态 | UI 建议 | 是否显示结果 |
| --- | --- | --- |
| `queued` | 显示“等待执行”或直接显示节点 `action` | 否 |
| `running` | 显示节点 `action`，允许动画 | 否 |
| `waiting_for_dependency` | 显示节点 `action`，必要时展开 `waiting_on`/子节点 | 否 |
| `waiting_for_user` | 显示“等待补充信息”，从 `inputs` 找对应表单 | 否 |
| `completed` | 显示“已完成”或节点 `action` | 是 |
| `failed` | 显示失败状态和安全错误 | 可显示部分结果 |
| `cancelled` | 显示已取消 | 否 |

专家节点进入 `waiting_for_dependency` 时，不要直接显示“等待其他专家”。应按以下顺序判断：

1. 该专家是否存在未结束的 `skill` 子节点？
2. 该专家是否存在未结束的 `tool` 子节点？
3. `waiting_on` 是否指向明确的依赖节点？
4. 如果以上都没有，再显示后端提供的 `expertNode.action`，不能自行改写为主任务文案。

## 14. 表单/追问状态不要从专家状态猜测

当专家或 Skill 需要用户补充信息时：

- 节点状态读取 `expertNode.status === "waiting_for_user"` 或 `skillNode.status === "waiting_for_user"`。
- 表单内容读取 `state.inputs[]`。
- 表单所属节点使用 `inputs[].owner_node_id`。
- 队列摘要读取 `state.input_queue`。

不要因为主任务 `state.waiting_count > 0` 就把所有专家都显示为等待用户输入。`waiting_count` 是运行级汇总，具体归属必须通过 `owner_node_id` 判断。

## 15. SSE 客户端消费伪代码

```ts
let currentState: V3State | null = null;
let lastCursor = 0;

function onSseFrame(event: string, payload: V3Frame | Record<string, never>) {
  if (event === "ping") {
    // 只保活，不改变 state
    return;
  }

  if (event !== "state" && event !== "done") return;

  const frame = payload as V3Frame;
  if (typeof frame.cursor === "number" && frame.cursor < lastCursor) return;
  if (typeof frame.cursor === "number") lastCursor = frame.cursor;

  if (frame.state) {
    // 使用完整快照替换，避免旧专家 action 残留
    currentState = frame.state;
    renderTopProgress(frame.state);
    renderExpertCards(frame.state.nodes ?? []);
  }

  if (event === "done") {
    finishStream(frame.outcome, frame.state);
  }
}
```

如果当前版本将最终正文增量包装为 `event: state` 且 `reason: "final_text_delta"`，仍然按普通 `state` 帧处理；正文读取 `state.result`，不能因为 `reason` 是增量就跳过节点状态更新。

`state.result` 是截至当前的**累计正文快照**，不是本帧新增文本：应替换同一个 `assistant_message_id` 对应的正文，不要逐帧拼接。专家的 `state.nodes[].result.conclusion` 属于专家详情；单专家场景下它可能与最终正文完全相同，不要再把它追加到最终正文。最终 `done` 的 `state.result` 仍然是最终权威快照。重连或状态修订时也应替换，不应假设文本一定只增不减。

### 15.1 单专家：专家结论与最终正文如何展示

当编排器本轮只分派一位专家时，专家完成后会先形成专家节点结论；专家团模式下，编排尾随后仍可能进行出站处理并形成最终正文。单专家策略通常直接采用专家原文，因此两份内容可能一样或高度相似，但不要依靠文本相等或专家数量来判断哪份是最终结果：编排尾仍可能处理内容，实际流程也可能因失败或中断而不同。

| 界面位置 | 读取字段 | 展示规则 |
| --- | --- | --- |
| 专家卡片状态 | 对应专家节点的 `status`、`phase`、`action` | 展示该专家自己的进度；`completed` 不代表最终正文已完成 |
| 专家详情（可选） | 对应专家节点的 `result.conclusion` | 作为专家详情或折叠区，不追加到最终正文 |
| 对话最终正文 | `state.result` | 以 `assistant_message_id` 定位同一气泡，按帧**整体替换**；不存在或为空时按当前阶段展示进度，不用专家结论填充正文 |
| 本次流完成 | `event: done` 中的 `state.result` | 先按最终快照更新气泡，再根据 `outcome`、`stream_status` 处理完成、失败或其他终态 |

例如，以下是同一助手消息的三个**简化状态片段**，省略了其余字段；实际帧还包含协议、游标等公共字段。专家节点的 `result` 是对象，正文 `state.result` 是字符串。

```jsonc
// ① 专家已经完成，编排尾尚未产出正文
{
  "assistant_message_id": "msg_1",
  "state": {
    "nodes": [{"kind": "expert", "expert_id": "academic_coach", "status": "completed",
               "result": {"conclusion": "建议先建立固定的学习时间。"}}],
    "result": ""
  }
}

// ② 编排尾流式输出中：这是当前累计正文，不是要追加的新片段
{
  "assistant_message_id": "msg_1",
  "reason": "final_text_delta",
  "state": {
    "nodes": [{"kind": "expert", "expert_id": "academic_coach", "status": "completed",
               "result": {"conclusion": "建议先建立固定的学习时间。"}}],
    "result": "建议先建立固定"
  }
}

// ③ event: done：使用最终正文替换同一气泡
{
  "assistant_message_id": "msg_1",
  "state": {
    "nodes": [{"kind": "expert", "expert_id": "academic_coach", "status": "completed",
               "result": {"conclusion": "建议先建立固定的学习时间。"}}],
    "result": "建议先建立固定的学习时间。"
  }
}
```

示例中的最终正文只显示一次。专家详情如果展开，也可以显示该专家结论，但应与正文保持独立区域，避免看起来像同一个回答连续输出两遍。`nodes` 是每帧的节点集合，真实数据还可能包含主任务、工具等节点；不要假定专家总在固定数组下标。

推荐的处理逻辑是：先依据 `kind`、`parent_id` 定位专家，用专家节点更新卡片；再用帧中的 `state.result` 替换 `assistant_message_id` 对应的正文。收到 `done` 时仍先应用其状态；若终态为失败、取消或没有正文，应按协议显示相应状态，不把专家结论假装成最终成功结果。不要因为内容相同而忽略最终帧，也不要通过删除重复字符串来修补错误的追加逻辑。

## 16. 前端接入验收清单

- [ ] 顶部总进度使用 `state.action`，专家卡片不使用它。
- [ ] 专家卡片通过 `kind === "expert"` 和 `parent_id === main.id` 筛选。
- [ ] 专家卡片的文案来自对应 `expertNode.action`。
- [ ] Skill 状态来自 `kind === "skill"` 的子节点。
- [ ] 工具状态来自 `kind === "tool"` 的子节点。
- [ ] 专家身份使用 `expert_id`，不使用数组下标。
- [ ] 同一专家重试时取最新节点，不把旧节点继续显示为当前状态。
- [ ] `round_experts` 只用于汇总统计，不用于具体动作文案。
- [ ] `dispatch` 只用于分派计划，不替代实时节点状态。
- [ ] `waiting_for_user` 时通过 `owner_node_id` 找对应表单。
- [ ] 收到 `state` 后替换完整快照，避免旧 action 残留。
- [ ] `ping` 不触发业务状态更新。
- [ ] `done` 先消费最终状态，再结束流。
- [ ] 单专家的专家结论只在专家详情中展示，不追加到 `state.result` 对应的最终正文。
- [ ] 不把 `state.result` 的累计快照逐帧拼接；`done` 使用最终快照替换同一消息。

验收用例：当家庭教育专家已完成、学习指导师仍在执行 Skill 时，页面必须同时显示：

```text
家庭教育专家顾问：已提交专家结论
学习指导师：正在执行学习习惯分析 / 正在等待「学习习惯分析」完成
```

不得显示为：

```text
学习指导师：正在等待其他专家完成
```

除非该文案确实来自学习指导师自己的 `expertNode.action`，并且它的 `waiting_on` 或子节点明确指向其他专家节点。
