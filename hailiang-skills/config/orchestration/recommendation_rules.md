# 推荐规则（种子镜像）

正式正文以 `runtime_system_skills/recommend_default/SKILL.md` 为准。本文件保留为工作台种子/兼容镜像。

## 1. 职责

在编排尾之后阅读本轮会话，输出 jump_cards 语义 JSON。不查用户状态，不选页面。一期每轮 0 或 1 组字段。

## 2. 可用输入

用户本轮原话、最终回答、本轮专家、catalog 枚举。画像里已有的地区/年级不要写入 filter。

## 3. 判断规则

主依据是本轮专家/平台回答是否落到某业务功能（对照 catalog 各条 when），不是等用户点名测评/咨询师。多个相关时只留最相关一组；寒暄/拒答/纯解释或只能靠专家类型猜测时输出空数组。intent / object_type / sub_type 必须来自 catalog。

## 4. 输出要求

```json
{"jump_cards": [{"intent": "assessment", "object_type": "single_assessment", "sub_type": "career_interest", "reason": "..."}]}
```

或 `{"jump_cards": []}`。filter 仅允许 region / grade / keyword / category。

## 5. 禁止

编造枚举外标签、按专家身份补默认卡、输出 route / title / button_text、一次输出多组字段。
