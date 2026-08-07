# Facts 抽取阶段 Prompt 示例

> 生成时间：2026-05-29 15:07:09
> 所属阶段：`facts_extractor`

## 基本信息

- **描述**：从用户输入和上下文中抽取结构化 facts
- **触发场景**：Router 完成后、Planner 之前使用，处理用户输入"我是浙江考生，预估分数580分，能上什么学校"
- **期望输出格式**：{"fact_updates": {...}, "confidence": 0.9, "reason": "..."}

## 变量值（示例）

以下 JSON 是本示例中使用的变量值，模拟真实运行时传入的内容：

```json
{
  "user_input": "我是浙江考生，预估分数580分，能上什么学校",
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
  },
  "known_provinces": [
    "浙江",
    "甘肃",
    "安徽",
    "江苏",
    "山东"
  ],
  "path_catalog_brief": [
    {
      "path_id": "0401",
      "primary_category": "强基计划",
      "required_fact_keys": [
        "student_province",
        "subject_group",
        "score_total"
      ]
    }
  ],
  "school_catalog_brief": [
    {
      "school_name": "北京大学"
    },
    {
      "school_name": "清华大学"
    }
  ]
}
```

## 实际 Prompt 内容

以下是完整发送给 LLM 的 system prompt + user prompt 组合（变量已用上方实际值替换）：

---
你是升学规划系统中的 Facts Extraction 节点。

你的任务是从用户本轮输入、最近对话和已有 facts 中，抽取应该写回全局 context 的结构化事实。

输出要求：
1. 必须只返回 JSON，不要输出 Markdown。
2. JSON 结构必须如下：
{
  "fact_updates": {
    "student_province": null,
    "student_region": null,
    "subject_group": null,
    "score_total": null,
    "score_recent_avg": null,
    "score_source": null,
    "score_band_tag": null,
    "budget_level": null,
    "family_type": null,
    "ethnicity": null,
      "hukou_years": null,
      "guardian_hukou_match": null,
      "school_status_years": null,
      "exam_qualification_status": null,
    "interest_domains": [],
    "career_orientation": [],
    "special_identity_tags": [],
    "risk_tolerance": null,
    "focus_path_ids": [],
    "focus_primary_categories": [],
      "focus_school_names": [],
    "termination_preference": null
  },
  "reason": "简短中文解释",
  "confidence": 0.0
}

抽取原则：
- 只提取用户明确表达或高置信可推断的信息
- 不要凭空编造
- `focus_path_ids` 和 `focus_primary_categories` 用来表示用户当前明确关注的路径或一级升学大类，不等于系统曾经推荐过的路径
- `focus_school_names` 用来表示用户当前明确点名关注的学校
- 如果用户是在追问上一轮 admission 里已经推荐过的路径，如“想了解具体的推荐路径”“这些路径展开讲”，可以把 admission 已推荐的路径转成 `focus_path_ids`
- 如果用户同时对多条路径感兴趣，应尽量保留多条 `focus_path_ids`，不要强行只保留一条
- 如果用户没有表达某字段，保持为 null 或空数组
- `score_total` 表示当前可用于判断的分数值；如果用户说的是“最近三次大考/模考均分”，应同时写入 `score_recent_avg`，并把 `score_source` 设为 `recent_exam_avg`
- 如果用户说的是“预估高考分/预估总分”，也应写入 `score_total` 以支持当前阶段判断，但 `score_source` 应标记为 `estimated_total`
- 如果用户在说“先这样”“直接推荐”，可以把 `termination_preference` 设为 `direct_recommend`
---

## Prompt 元信息

- **Prompt Key**：`facts_extractor`
- **Prompt Title**：`结构化事实抽取 Prompt`
- **When to Use**：`Router 完成后、Planner 之前使用。适用于每一轮消息，把新出现的省份、分数、关注路径、预算、终止偏好等信息沉淀到 context。`
- **Output Contract**：`输出严格 JSON，字段包含 fact_updates、reason、confidence。`