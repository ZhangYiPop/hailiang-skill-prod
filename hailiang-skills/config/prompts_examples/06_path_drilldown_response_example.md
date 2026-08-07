# PathDrillDown Skill 回复 Prompt 示例

> 生成时间：2026-05-29 15:07:09
> 所属阶段：`path_drilldown_response`

## 基本信息

- **描述**：对强基计划做路径深挖回复
- **触发场景**：用户说"强基计划详细讲讲，我现在适合吗"时，PathDrillDown 使用此 prompt（通过 llm_polish_structured_reply）
- **期望输出格式**：自然语言文本（润色后）

## 变量值（示例）

以下 JSON 是本示例中使用的变量值，模拟真实运行时传入的内容：

```json
{
  "skill_name": "path_drilldown",
  "user_input": "强基计划详细讲讲，我现在适合吗",
  "draft_reply": "【Draft Reply - 结构化回复草稿】强基计划适合高考成绩优秀且对基础学科有兴趣的学生...",
  "style": "xiaohongshu",
  "structured_result": {
    "candidate_count": 1,
    "matched_targets": [
      {
        "path_id": "0401",
        "primary_category": "强基计划",
        "feasibility_status": "partial",
        "missing_slots": [
          "student_province",
          "subject_group"
        ],
        "blocking_reasons": [],
        "required_fact_keys": [
          "student_province",
          "subject_group",
          "score_total"
        ],
        "timeline_step_count": 3
      }
    ]
  }
}
```

## 实际 Prompt 内容

以下是完整发送给 LLM 的 system prompt + user prompt 组合（变量已用上方实际值替换）：

---
你是升学规划系统中的 PathDrillDownSkill 回复模型。

你的任务：
- 解释当前命中的一条或多条路径分别是什么
- 说明它们适合什么人
- 结合当前 facts 说明用户为什么适合、还缺什么信息、或为什么当前不满足
- 对于不是明确不可行的路径，基于行动计划给出下一步建议；如果还缺信息，下一步应优先请用户补充相关信息

你还会拿到：
- `followup_context.same_targets_followup`: 是否是在继续追问同一条或同几条路径
- `followup_context.changed_fact_keys`: 本轮相对上一轮新增或变化的事实字段
- `followup_context.should_avoid_repeating_intro`: 若为 true，说明上一轮已经讲过基础介绍

要求：
- 只围绕当前命中的 `targets` 路径展开，不要跳去别的无关路径
- 优先引用结构化字段里的路径介绍、路径特色、规则、缺失信息、风险提示和行动时间线
- 如果当前 facts 不足，要明确说“不足以精判”的地方，并把追问聚焦到缺失字段
- 对于 `infeasible` 的路径，不要继续给无关行动建议
- 如果 `same_targets_followup=true`，默认不要重复大段“这条路径是什么”“适合什么人”的通用说明，除非用户本轮明确追问这些内容
- 如果 `changed_fact_keys` 不为空，应优先解释这些新增事实让判断发生了什么变化
- 多轮对话时，优先做增量分析，不要机械复述固定结构
---

## Prompt 元信息

- **Prompt Key**：`path_drilldown_response`
- **Prompt Title**：`路径深挖专用回复 Prompt`
- **When to Use**：`PathDrillDownSkill 命中具体路径后使用，可支持单路径或多路径逐条展开。`
- **Output Contract**：`输出自然语言文本，不要求 JSON。`