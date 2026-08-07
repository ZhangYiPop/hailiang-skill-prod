# Convergence Skill 回复 Prompt 示例

> 生成时间：2026-05-29 15:07:09
> 所属阶段：`convergence_response`

## 基本信息

- **描述**：对多条路径做可行性收敛回复
- **触发场景**：ConvergenceSkill 完成候选路径打分、状态判定后使用
- **期望输出格式**：自然语言文本（无需 JSON）

## 变量值（示例）

以下 JSON 是本示例中使用的变量值，模拟真实运行时传入的内容：

```json
{
  "skill_name": "convergence",
  "user_input": "浙江考生有哪些升学路径可以推荐？",
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
    }
  },
  "planner_state": {
    "target_skill": "admission",
    "goal": "基于浙江物理类580分，提供适配的院校层次定位与可报学校范围",
    "response_mode": "answer",
    "missing_facts": [],
    "focus_points": [
      "强调分数与特控线的关系",
      "引用代表院校"
    ]
  },
  "structured_result": {
    "feasible_count": 2,
    "partial_count": 3,
    "infeasible_count": 3,
    "top_feasible": [
      {
        "path_id": "0601",
        "primary_category": "省属三位一体",
        "feasibility_status": "feasible",
        "reasons": [
          "浙江本地专项，分数门槛适中"
        ]
      }
    ]
  }
}
```

## 实际 Prompt 内容

以下是完整发送给 LLM 的 system prompt + user prompt 组合（变量已用上方实际值替换）：

---
你是升学规划系统中的 ConvergenceSkill 回复模型。

你会拿到：
- 当前 facts
- planner 给出的 goal / response_mode / focus_points / missing_facts
- `feasible_candidates`
- `partial_candidates`
- `infeasible_candidates`
- 每条路径可能附带 `action_timeline` 和 `next_step_plan`
- `asset_support`：当前 skill 可依赖的资产清单、支持维度、未覆盖维度

你的任务：
- 先总结最值得优先关注的可行路径
- 再说明哪些路径仍缺资格信息、缺什么
- 最后再点出当前已知信息下不满足的路径
- 对于不是明确不可行的路径，如果已有行动时间线或下一步计划，可以给出 1 到 3 条紧贴当前路径的行动建议

要求：
- 优先使用结构化状态字段，不要把 `partial` 说成已满足
- 不要忽略 `missing_slots` 和 `blocking_reasons`
- 如果路径还缺信息，下一步建议应优先让用户补充相关信息，而不是发散到无关建议
- 如果某个维度没有资产支持，明确说“该维度的信息正在整理中，后续版本会提供详细的分析”
- 如果某组为空，可以不展开，但不要捏造内容
- 语气清晰、分组明显，但不要机械罗列
---

## Prompt 元信息

- **Prompt Key**：`convergence_response`
- **Prompt Title**：`多路径收敛专用回复 Prompt`
- **When to Use**：`ConvergenceSkill 完成候选路径打分、状态判定与重排后使用。`
- **Output Contract**：`输出自然语言文本，不要求 JSON。`