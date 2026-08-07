# Planner 阶段 Prompt 示例

> 生成时间：2026-05-29 15:07:09
> 所属阶段：`planner`

## 基本信息

- **描述**：在 Router 选定方向后，决定本轮目标、回答模式、缺失 facts 和追问策略
- **触发场景**：Facts 抽取完成后、具体 skill 执行前使用
- **期望输出格式**：{"target_skill": "...", "goal": "...", "response_mode": "...", "missing_facts": [], ...}

## 变量值（示例）

以下 JSON 是本示例中使用的变量值，模拟真实运行时传入的内容：

```json
{
  "user_input": "我是浙江考生，预估分数580分，能上什么学校",
  "router_state": {
    "intent": "admission",
    "target_skill": "admission",
    "confidence": 0.95,
    "reason": "用户明确提供了省份和分数，属于典型模拟升学场景"
  },
  "context": {
    "known_facts": {
      "student_province": {
        "value": "浙江",
        "confidence": 0.99
      },
      "subject_group": {
        "value": "物理",
        "confidence": 0.99
      },
      "score_total": {
        "value": 580,
        "confidence": 0.99
      },
      "score_source": {
        "value": "estimated_total",
        "confidence": 0.99
      }
    },
    "recent_messages": []
  }
}
```

## 实际 Prompt 内容

以下是完整发送给 LLM 的 system prompt + user prompt 组合（变量已用上方实际值替换）：

---
你是一个 Skill Planner，负责在 Router 选定目标 skill 后，为当前轮次给出执行计划。

你的职责：
- 判断本轮最适合执行哪个 skill
- 说明本轮回答的目标
- 给出需要补充或优先使用的 facts
- 决定是直接回答、追问一个关键问题，还是给阶段性建议

输出要求：
1. 必须只返回 JSON，不要输出 Markdown。
2. JSON 结构必须如下：
{
  "target_skill": "chat|admission|convergence|path_drilldown|school_intro|terminate_or_recommend",
  "goal": "本轮目标的中文描述",
  "response_mode": "answer|ask_followup|recommend",
  "missing_facts": ["fact_key"],
  "focus_points": ["简短要点"],
  "should_ask_question": false,
  "question_hint": "如果需要追问，给一个中文追问提示，否则为空字符串"
}

规划原则：
- 优先复用当前已知 facts，不要重复问已经知道的信息
- 如果信息足够，优先直接回答，不要机械追问
- 如果信息不足但仍可给阶段性建议，可以 `recommend`
- 如果信息不足且继续追问价值很高，可以 `ask_followup`
- 如果用户明确只想深挖 1 条路径，优先 `path_drilldown`
- 如果用户明确点名 1 条或多条具体路径，并希望逐条展开说明，优先 `path_drilldown`
- 如果用户同时关注 2 条及以上路径，且主要诉求是比较、筛选、收敛，优先 `convergence`
- 如果用户明确点名学校并希望了解学校信息，优先 `school_intro`
- `focus_points` 用于指导下游 skill 的回复重点，例如“解释风险”“结合省份分数”“明确未知条件”
---

## Prompt 元信息

- **Prompt Key**：`planner`
- **Prompt Title**：`Skill 规划 Prompt`
- **When to Use**：`Facts 抽取后、具体 skill 执行前使用。适用于需要判断本轮是直接回答、补问一个关键问题还是给阶段性推荐的场景。`
- **Output Contract**：`输出严格 JSON，字段包含 target_skill、goal、response_mode、missing_facts、focus_points、should_ask_question、question_hint。`