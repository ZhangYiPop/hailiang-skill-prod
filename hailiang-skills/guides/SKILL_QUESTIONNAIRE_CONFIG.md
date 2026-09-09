# Skill 问卷配置

Skill 将问卷内容放在 `assets/questionnaire.json`，只在 `SKILL.md` 的 frontmatter 中声明配置文件路径。问卷由 Skill 自己定义，运行时负责生成 `fact_form` 并校验答案。

## 声明方式

```yaml
questionnaire:
  enabled: true
  config_path: assets/questionnaire.json
```

`assets/questionnaire.json` 内容：

```json
{
  "schema_version": 1,
  "title": "留学费用预算信息",
  "max_fields_per_form": 6,
  "questions": [
    {
      "id": "target_country",
      "label": "计划留学国家或地区",
      "input_type": "single_select",
      "required": true,
      "options": [{ "value": "英国", "label": "英国" }, { "value": "美国", "label": "美国" }]
    },
    {
      "id": "study_goals",
      "label": "留学目标",
      "input_type": "multi_select",
      "max_selections": 3,
      "options": [{ "value": "学术提升", "label": "学术提升" }, { "value": "就业发展", "label": "就业发展" }]
    },
    {
      "id": "annual_budget",
      "label": "每年可接受的预算",
      "input_type": "number",
      "value_type": "number",
      "min": 0,
      "decimal_places": 1,
      "unit": "万元"
    },
    {
      "id": "notes",
      "label": "其他补充说明",
      "input_type": "text",
      "required": false
    }
  ]
}
```

## 字段约定

`id` 只需在当前 Skill 的问卷内稳定且唯一，不要求跨 Skill 全局唯一；答案、历史和 Facts 映射均使用 `id`。`single_select` 和 `multi_select` 必须有非空 `options`，选项使用 `value` 保存、`label` 展示。填空支持 `text`、`integer`、`number`；数字可使用 `min`、`max` 和 `decimal_places`。

需要条件显示时使用结构化条件，例如：

```yaml
display_condition:
  question_id: target_country
  operator: equals
  value: 英国
```

也支持 `all`、`any`、`contains`、`in` 和 `not_equals`。不要把业务规则写成可执行代码。

## 会话和 Facts

答案默认保存于当前 Skill 的 `skill_session`。如果确实需要写入 Facts，使用受控的 `questionnaire.persistence.mappings`，不要让业务配置直接填写数据库字段。

在 AI 业务调试台中编辑 JSON 后，先校验和预览，再保存为新 revision；只有发布的 revision 会被线上使用。导出的配置保存为 `assets/questionnaire.json`，而 `SKILL.md` 只保留 `config_path` 声明。
