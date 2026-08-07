[OPEN] specialty-skill-routing

# 背景

- 问题：测试反馈“小初特长方向诊断”skill 较难触发，无论在首轮对话还是进入“升学规划顾问”skill 后，都不容易切到该 skill。
- 当前阶段：只做证据收集与原因分析，不修改业务逻辑。
- 关联会话：
  - `/Users/ayi/Project/hailiang-skill_v0201/hailiang-skills/logs/sessions/sess_de232b72c089/events.jsonl`
  - `/Users/ayi/Project/hailiang-skill_v0201/hailiang-skills/logs/sessions/sess_de232b72c089/snapshot.json`

# 可证伪假设

1. 路由配置中对 `interest_explore` 的触发条件过窄，导致首轮更容易命中 `main_planner` 或其他兜底 skill。
2. `main_planner` 的提示词或合约将“特长方向诊断”定义为内部处理能力，而不是显式切换到独立 skill。
3. 进入“升学规划顾问”后，当前会话阶段或已激活 skill 对后续路由产生粘性，抑制了再次切换到 `interest_explore`。
4. 用户表达与 `interest_explore` 的关键词/意图样本不匹配，导致意图分类置信度不足。
5. 事件日志中实际发生了路由尝试，但被循环防御、阶段约束或 skill 生命周期规则拦截。

# 检查计划

1. 读取会话事件与快照，确认实际路由轨迹。
2. 检查 `interest_explore`、`main_planner`、`junior_multi_path_planning` 的元数据与合约。
3. 检查路由配置、阶段状态、已激活 skill 粘性与生命周期规则。
4. 输出证据链和原因判断，暂不提交修复。
