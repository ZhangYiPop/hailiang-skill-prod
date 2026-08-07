# 当前对话与 Skill 路由流程

> **当前 Skill ID 约定（2026-07）**：`general_chat` 是新会话的隐式默认入口；原
> `main_planner` 的公开 Skill ID 已统一为 `career_plan_entity`（展示名“升学规划顾问”）。
> `main_planner` 仅作为历史快照、旧请求和旧字段的兼容别名，不再作为新的入口或工具栏
> ID 返回。升学规划顾问虽然仍负责画像判断和子 Skill 引导，但现在也是可被
> `general_chat` 推荐、由用户点击后进入的专项 Skill。

本文描述当前代码真正执行的对话链路，作为排查“用户输入后为什么进入某个 Skill”以及新增 Skill 接入时的统一参照。

## 1. 先说结论

当前生产入口只有一条主链路：

```text
HTTP 请求
  -> chat / chat_stream
  -> MainPlannerOrchestrator.handle_message()
  -> 恢复 runtime SessionState
  -> general_chat / career_plan_entity 路由
  -> runtime 原生 Skill 或 Hailiang bridge Skill
  -> 同步 facts、保存状态
  -> 生成最终回复和下一步建议
```

旧的链路仍然存在于代码中，但不是 `create_app()` 的正常主流程：

```text
RouterSkill -> FactsExtractorSkill -> PlannerSkill -> 业务 Skill
```

这条旧链路由 `src/hailiang_skills/core/orchestrator.py` 实现。当前 `api/main.py` 虽然仍注册了这些旧 Skill，但实际注入的是 `MainPlannerOrchestrator`。旧链路主要保留给兼容、测试和无 runtime 路由时的 fallback。

## 2. 从用户输入到响应的完整流程

### 2.1 HTTP 层

对外聊天入口只有 `POST /api/v1/sessions/chat/stream`。请求以 `input.action` 区分
普通聊天、进入/退出 Skill 和停止；响应以 `hailiang.sse.v2` 的 `event: state` 完整快照组成，
最后追加 `event: done` 表示本次 SSE 数据已全部发送完成。
内部仍由 `orchestrator.handle_message()` 执行模型编排。

请求进入后依次做这些事情：

1. 读取会话，恢复 `SessionContext`。
2. 加载用户、档案和会话 facts，形成 `effective_facts`。
3. 设置本轮模型参数，例如 `enable_thinking`、`return_reasoning`。
4. 如果用户从上一轮的 route suggestion 中选择了某个方向，保存 `requested_target_skill_id` 和 `handoff_context`。
5. 进入 `MainPlannerOrchestrator.handle_message()`。
6. 对用户输入做输入安全检测；命中时直接中断，不进入 Skill。
7. Skill 执行完成后，对模型输出做输出安全检测。
8. 持久化 facts、会话状态、消息和事件日志。
9. 计算推荐 Skill，并写入 v2 状态中的 `skill_rooms`。

### 2.2 MainPlannerOrchestrator 本轮处理

每一轮的核心顺序是：

```text
用户消息
  -> 写入 context.messages
  -> 初始化旧场景状态
  -> 加载 runtime SessionState
  -> hailiang facts -> runtime global_facts
  -> 路由优先级判断（默认 general_chat 只产出建议，不自动切换）
  -> 多元路径学段门控
  -> 准备长上下文和会话记忆
  -> 分发到目标 Skill
  -> runtime global_facts -> hailiang facts
  -> 写入状态并返回 SkillResult
```

其中“路由优先级判断”不是单次 LLM 调用，而是多个短路规则、显式匹配和分类器的组合。

## 3. Skill 是怎么被判断出来的

新会话的 `active_skill` 初始为 `general_chat`。普通消息识别到一个或多个候选时，
服务端保持 `general_chat`，把候选放进本轮 assistant 回复下的 `route_suggestions` 卡片；
只有用户点击卡片或顶部工具栏按钮，才调用独立 Skill 转场接口。`career_plan_entity`
和其他专项 Skill 的切换规则相同。

当前路由大致遵循下面的优先级，越靠前越优先：

| 优先级 | 判断来源 | 作用 |
| --- | --- | --- |
| 1 | `requested_target_skill_id` | 用户点击上一轮的下一步建议，直接切到指定 Skill；置信度视为 1.0 |
| 2 | resume 状态 | 用户要求回到之前被中断的 Skill |
| 3 | 学段门控 | 已命中多元路径，但不知道小学/初中/高中时，先留在当前会话 Skill 询问年级 |
| 4 | 显式场景名 | 用户直接说“提分”“兴趣探索”“选科参谋”“多元路径规划”等，优先按注册表匹配 |
| 5 | pending route 选择 | 上一轮推荐了方向，本轮用户通过按钮确认该方向 |
| 6 | 保护性短路 | 结构化补充 facts、画像收集、较长画像描述，保持当前场景；泛化表达留在 `general_chat` |
| 7 | `IntentRouter` | 对可用 Skill 的场景名、触发词、routing examples 做文本匹配；配置了 embedding 时同时使用向量匹配 |
| 8 | LLM 路由兜底 | 没有文本/向量候选时，用 LLM 产出路由判断 |
| 9 | 默认兜底 | 当前在 `general_chat` 或 `career_plan_entity` 时继续当前场景；已在其他专项 Skill 时受 task lock 约束 |

`IntentRouter` 对已有子 Skill 还会应用场景锁：

- 当前在 `general_chat`：只生成建议卡，不因文本命中自动切换。
- 当前在 `career_plan_entity`：可以继续做规划问诊，也可以通过回复卡片引导到其他专项 Skill。
- 当前在子 Skill：没有明确的“切换/进入/先看”等表达时，不轻易跨场景跳转。
- 用户只是补充 facts 时，保持当前场景，不把事实内容误判成新任务。

### 3.1 当前可用 Skill 映射

| runtime skill id | 执行方式 | 实际业务执行者 | 场景 |
| --- | --- | --- | --- |
| `career_plan_entity` | runtime 原生 | runtime 模型与状态机 | 生涯规划专项顾问、画像收集、方向推荐；由 `general_chat` 推荐后进入 |
| `mock_admission` | Hailiang bridge | `admission` | 模拟升学 |
| `multi_path_planning` | Hailiang bridge | `convergence` | 高中/通用多元路径规划 |
| `junior_multi_path_planning` | runtime 子 Skill | runtime 原生 | 初中多元路径规划 |
| `interest_explore` | runtime 原生 | runtime Skill | 兴趣探索、特长方向诊断 |
| `score_improve` | runtime 原生 | runtime Skill | 提分规划 |
| `future_explore` | runtime 原生 | runtime Skill | 前景探路 |
| `subject_advisor` | runtime 原生 | runtime Skill | 选科参谋/选课参谋 |

Hailiang bridge 的映射在 `runtime_bridge/main_planner.py` 的 `HAILIANG_TARGETS` 中维护：

```text
mock_admission       -> admission
multi_path_planning  -> convergence
```

## 4. 路由完成后怎么执行

路由得到 `active_skill_id` 后，按类型分发：

### 4.1 Hailiang bridge Skill

适用于 `mock_admission` 和 `multi_path_planning`：

```text
main_planner
  -> mock_admission
  -> admission.run(...)
```

或：

```text
main_planner
  -> multi_path_planning
  -> convergence.run(...)
```

这类 Skill 继续使用 Hailiang 原有的规则、结构化资产、候选路径和 `SkillResult` 协议。

### 4.2 runtime 原生 Skill

其他 runtime Skill 由 runtime client 执行：

1. 按 `SKILL.md` 和 `runtime_contract.json` 构造 prompt。
2. 按 progressive disclosure 策略决定是否加载 references、assets、scripts。
3. 根据当前 stage 和 facts 执行工具或脚本。
4. 读取 runtime 返回的 facts/status/route 信号。
5. 生成最终回复。

如果 runtime client 不可用：

- runtime 原生 Skill：返回“已命中 Skill，但当前没有可用模型客户端/运行时”的明确错误说明。
- 没有可用 runtime 目标：退回旧 `chat` Skill。

## 5. Facts 和状态在路由中的作用

当前有两套状态模型，通过 bridge 同步：

```text
Hailiang SessionContext
  shared_facts + profile_facts + session_facts
  -> effective_facts

runtime SessionState
  global_facts + skill_facts + stage_facts
```

每轮执行前：

```text
context.known_facts -> state.global_facts
```

runtime Skill 执行后：

```text
state.global_facts -> context.update_fact(...)
```

需要区分三个容易混淆的字段：

- `runtime_state.active_skill_id`：runtime 当前目标 Skill。
- `skill_states.main_planner.target_skill`：返回给前端的 runtime 路由目标。
- `context.interaction_state.active_skill` / API `active_skill`：本轮实际执行的 Skill。bridge 场景下可能是旧名，例如 `admission`、`convergence`。

因此出现下面的结果是正常的：

```text
main_planner_state.target_skill = multi_path_planning
active_skill                    = convergence
```

## 6. 流式输出流程

流式请求由 `StreamingRunner` 包装，前端看到的主要事件顺序是：

```text
run_started
  -> skill_context
  -> skill_status(intent)
  -> skill_status(planner)
  -> final_text_delta 逐段输出
  -> message_block / skill_action
  -> final_message
  -> run_completed
```

当前 runtime 原生 Skill 的底层执行主要是完整回复模式，bridge 会在拿到完整回复后切块发送 `final_text_delta`；Hailiang 旧业务 Skill 可以在模型生成时直接发送回复增量。

## 7. 当前需要整理的主要问题

### 7.1 路由配置存在多个权威来源

目前至少有这些路由来源：

- `runtime_skills/main_planner/runtime_contract.json`：正式场景到 target skill 的路由表。
- `runtime_bridge/main_planner.py` 的 `SCENE_HINTS`：关键词提示和隐式场景判断。
- `skill_runtime/intent_router.py`：从 runtime metadata、triggers、accepts_scenes、routing examples 构造候选。
- `config/skills_routing_config.yml`：旧 Router 的关键词 fallback。
- `config/skills_registry.yml`：旧 Skill 的可跳转关系。

这会导致“同一句话到底由谁判断”不够直观，也容易出现新增 Skill 只改了一处、实际却无法触发的问题。

### 7.2 route contract 中的 required facts 被运行时放宽

`MainPlannerOrchestrator._load_runtime_registry()` 会把 main_planner route 的 `required_global_facts` 和 `required_skill_facts` 重建为空元组。也就是说，contract 中虽然声明了 `grade` 等前置 facts，当前路由本身并不会统一拦截；只有部分场景通过额外逻辑，例如多元路径的学段门控，主动追问。

### 7.3 旧链路和新链路同时存在

旧 `RouterSkill` 仍有自己的关键词、LLM fallback、场景切换和 loop defense；新 `IntentRouter` 又有一套短路、文本/embedding、LLM fallback 和 task lock。排查时如果没有先确认入口，很容易读错代码或误以为两套路由会串行执行。

### 7.4 Skill id、实际执行名、展示名不完全一致

`mock_admission -> admission`、`multi_path_planning -> convergence` 是最明显的例子。虽然当前通过 `skill_display.py` 做了兼容，但日志、前端和测试应明确使用“路由目标”和“实际执行者”两个字段，不要只依赖 `active_skill` 一个字段。

### 7.5 route suggestion 是“下一轮路由”，不是本轮路由

`route_suggestions` 在回复生成后分析本轮回答中的可选方向。用户点击后，下一轮通过 `requested_target_skill_id` 再进入路由。因此它不能替代本轮 `IntentRouter`，也不能被当作本轮已经执行过的 Skill。

## 8. 建议收敛成的目标流程

后续建议把路由抽象成一个明确的 `RouteDecision`，固定为：

```text
1. 输入安全检查
2. 读取会话与 facts
3. 事实更新识别
4. 意图与场景判断
5. 前置 facts / 学段门控
6. 生成 RouteDecision
7. 选择执行适配器
8. 执行 Skill
9. 同步 facts 和状态
10. 输出安全检查与持久化
11. 生成下一轮 route_suggestions
```

其中路由配置建议只保留一套主来源：

- `runtime_contract.json`：Skill id、场景、前置 facts、stage、执行类型。
- `SKILL.md` 或 runtime metadata：触发词、示例、Skill 能力描述。
- 通用 `IntentRouter`：统一完成显式匹配、文本/embedding 匹配和 LLM 兜底。
- `SCENE_HINTS` 与旧 `config/skills_routing_config.yml`：逐步迁移后删除，避免重复维护。

## 9. 当前新增的入口与 Skill 进入流程

当前实现已增加 `general_chat` 通用大模型兜底 Skill，展示名为“自由问答”。它是新会话的唯一隐式入口；
`career_plan_entity`（历史目录仍叫 `main_planner`）是可由按钮进入的专项顾问：

```text
用户输入
  -> general_chat 回答当前问题并生成 route_suggestions 按钮
  -> 用户点击 Skill 按钮并调用独立转场接口
  -> 目标 Skill
  -> skill_intro（输出 runtime description）
  -> Skill 正式开场白和后续回复
```

进入子 Skill 时，`skill_intro` 会作为独立助手消息持久化，并通过 SSE 单独发送；因此前端刷新或重新加载会话后仍能看到 Skill 描述。输入框上方的 Skill 按钮通过 `GET /api/v1/skills` 动态读取 runtime registry，`general_chat` 不出现在可进入列表中，`career_plan_entity` 与其他专项 Skill 一样出现在可进入列表中。

每次路由至少记录以下字段，方便排查：

```json
{
  "from_skill": "main_planner",
  "target_skill_id": "interest_explore",
  "execution_skill": "interest_explore",
  "route_mode": "direct",
  "reason": "用户明确提到兴趣探索",
  "confidence": 0.98,
  "matched_examples": ["兴趣探索"],
  "gate": {"passed": true, "missing_facts": []}
}
```

这样可以把“识别到了什么”“为什么切换”“是否被 facts 阻断”“最后执行了谁”一次说明清楚。

## 9. 相关代码入口

- API 入口：`src/hailiang_skills/api/routes/chat.py`、`chat_stream.py`
- 当前主编排：`src/hailiang_skills/runtime_bridge/main_planner.py`
- 当前意图路由：`src/hailiang_skills/skill_runtime/intent_router.py`
- runtime skill 注册与加载：`src/hailiang_skills/skill_runtime/skill_registry.py`
- runtime 合约：`runtime_skills/*/runtime_contract.json`
- facts bridge：`src/hailiang_skills/runtime_bridge/facts.py`
- 展示字段兼容：`src/hailiang_skills/core/skill_display.py`
- 旧链路：`src/hailiang_skills/core/orchestrator.py`、`src/hailiang_skills/skills/router.py`
