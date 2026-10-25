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
