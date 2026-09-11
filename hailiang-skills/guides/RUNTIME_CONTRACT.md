# runtime_contract.json

`runtime_contract.json` 是平台的 Facts 权限边界，不是业务流程配置文件。没有读写 Facts 需求的 Skill 可以不提供该文件。

推荐的新 Skill 只声明 `facts`：

```json
{
  "facts": {
    "global": ["student.grade"],
    "skill": ["target_city", "travel_budget"],
    "stage": {},
    "exports": {
      "promote_to_global": [],
      "share_with_parent_skill": []
    }
  }
}
```

- `global`：允许读取的全局事实字段。
- `skill`：允许在当前 Skill 会话内读写的字段。
- `stage`：仅在特定阶段使用的字段；没有需求时留空。
- `promote_to_global`：允许从 Skill 事实提升为全局事实的字段。
- `share_with_parent_skill`：允许向父 Skill 分享的字段。

业务场景、触发语义、脚本用途和执行时机写在 `SKILL.md`。问卷入口在 `SKILL.md` 声明，题目配置放在 `assets/questionnaire.json`。

历史 Skill 中的 `skill_id`、`stages`、`routes`、`accepts_scenes`、`questionnaire` 等字段仍会被运行时兼容读取，但不再作为新 Skill 的推荐写法。迁移时应先把对应内容移入 `SKILL.md`，验证发布后再删除旧字段。
