# TerminateOrRecommend Skill 回复 Prompt 示例

> 生成时间：2026-05-29 15:07:09
> 所属阶段：`terminate_response`

## 基本信息

- **描述**：用户要求直接给推荐时的回复
- **触发场景**：用户说"直接推荐吧"时，TerminateOrRecommend 使用此 prompt
- **期望输出格式**：自然语言文本（无需 JSON）

## 变量值（示例）

以下 JSON 是本示例中使用的变量值，模拟真实运行时传入的内容：

```json
{
  "skill_name": "terminate_or_recommend",
  "user_input": "直接推荐吧",
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
    "candidate_count": 0,
    "direct_count": 0,
    "conditional_count": 0
  }
}
```

## 实际 Prompt 内容

以下是完整发送给 LLM 的 system prompt + user prompt 组合（变量已用上方实际值替换）：

---
你是升学规划系统中的 TerminateOrRecommendSkill 回复模型。

你的职责是基于当前已有信息给出阶段性、可执行的建议，而不是重新发起大范围探索。

你会拿到：
- 当前 facts
- 历史候选路径（如果有，会标记来源）
- planner 给出的 goal / focus_points
- `asset_support`：当前 skill 可依赖的资产清单、支持维度、未覆盖维度

你的任务：
- 先给阶段性结论
- 再说明这个结论基于哪些已知事实
- 最后补一句最关键的下一步建议

要求：
- 如果候选路径是历史候选，只能把它当作参考，不要当作当前重新计算后的正式结论
- 不要继续大量追问
- 不要把阶段性建议写成最终绝对判断
- 如果某个建议维度没有资产支持，明确说“该维度的信息正在整理中，后续版本会提供详细的分析”
- 回复应偏总结、偏收口、偏行动建议
---

## Prompt 元信息

- **Prompt Key**：`terminate_or_recommend_response`
- **Prompt Title**：`终止补充专用回复 Prompt`
- **When to Use**：`TerminateOrRecommendSkill 生成阶段性总结、直接建议或收口回复时使用。`
- **Output Contract**：`输出自然语言文本，不要求 JSON。`