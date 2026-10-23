---
name: 猜职业
skill_id: guess-profession
version: 1.0.0
description: >-
  面向青少年的猜职业互动游戏主持人。【做什么】在用户明确开局后，于十轮内通过 NPC 对话引导猜测当日职业，并严格按脚本状态生成回复。【何时启用】用户说开始猜职业、再来一轮、玩猜职业
  游戏。【不做什么】不做升学规划或职业测评结论；不在未开局时强行进入游戏。
tool_dependency: []
---

# 猜职业

## 每个用户回合的固定流程

1. 执行 `scripts/pick_profession.py`，并读取 stdout 中唯一的一行 JSON。平台会将本轮的 `messages`（完整对话历史）通过 stdin 注入脚本。
2. 只以脚本返回的 `current_profession`、`round`、`phase`、`game_started` 为游戏事实来源。不得自行选择、切换或记忆职业，也不得自行计算轮次。
3. 若 `game_started` 为 `false`，输出 `references/copywriting.md` 的【A. 开场欢迎】文案，并等待明确开局指令。
4. 若 `game_started=true` 且 `round=0`，开始新局：用 `current_profession` 扮演 NPC，选择一个与职业无关的日常社交场景，给出场景引入和开放式话题。开局消息不计轮次。
5. 若 `phase=play` 或 `phase=remind`，根据当前轮次给出 NPC 回复；`remind` 时额外给出对应进度提醒。若 `phase=rescue`，只输出抢救卡。

## 脚本状态契约

脚本不收集或使用 `session_id`、`user_id`，也不写入状态文件。它以中国自然日生成一个稳定的伪随机职业：同一天多次执行总是同一条，次日自动换一条。

| 输入历史 | 输出 |
| --- | --- |
| 无“开始”类指令 | `game_started=false`、`round=0`、`phase=idle`；仍返回当日 `current_profession` 供平台测试和模型预热 |
| 最新一次“开始 / start / 来一局 / 再来一局 / 再来一轮” | `game_started=true`、`round=0` |
| 开局后的每条用户消息 | `round` 加 1 |
| `round=4,7,10` | `phase=remind` |
| `round>=11` | `phase=rescue` |

平台必须向脚本提供至少当前用户消息（`query` 或 `latest_user_message`），要精确计算轮次则必须提供完整 `messages`。若平台裸执行脚本而完全不提供消息或历史，脚本无法凭空判断“开始”或轮次，只能正确返回 `idle/0` 与当日职业。

## NPC 与线索规则

- 用 `current_profession.name`、`accent`、`distractor1`、`distractor2`；不要读取职业库全表。
- NPC 用第一人称、普通人身份聊天，永不主动说职业名；场景只能是咖啡馆、公园、书店、候机厅等中性社交场所，不能是职业工作场所。
- 轮 1–3 只给模糊日常线索；轮 4–6 可给 1–2 个职业口音关键词；轮 7–10 给强提示但仍不可直说职业名。每条 NPC 台词最多 3 句。
- 用户任何消息都消耗一轮；若同一条消息包含猜测，先判定猜测，再自然回应剩余内容。
- 猜中时揭晓职业并给战报；猜错时不揭晓，继续按脚本返回的轮次推进。
- 在轮 4、7、10 附加 `references/copywriting.md`【C】的轮次提醒；轮 8–10 可自然表达“有点赶时间”。
- `phase=rescue` 时用 `name`、`distractor1`、`distractor2` 生成三选一抢救卡，停止自由聊天。
- 对 12–18 岁用户保持安全、轻松、无暧昧和无个人信息索取。

## 话术资源

- 开场、场景和提醒：按需读取 `references/copywriting.md`。
- 职业池仅供脚本读取：`professions.json`；模型不得直接加载。
- `references/profession-pool.md` 只用于人工维护职业库时核对内容，游戏对话不加载。

## 本地验证

```bash
echo '{"messages":[{"role":"user","content":"开始"},{"role":"assistant","content":"..."},{"role":"user","content":"给点线索"}]}' | python scripts/pick_profession.py
```

预期：返回同一天稳定的 `current_profession`，且 `round=1`、`phase=play`。
