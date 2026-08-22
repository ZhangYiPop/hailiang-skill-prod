# Hailiang Expert Bundle v1

每个目录是一个专家包，必须包含：

- `agent.yaml`：业务可配置的 ID、名称、授权 Skill、预算、能力和 `topology`。
- `AGENT.md`：角色、经验规则、Skill 使用原则、转交原则及回答边界。
- `skills.lock.json`：平台注册 `runtime_skills` 的 `skill_id`、`version`。

专家包不是 Skill 分发包：禁止放入 `skills/`，导入时只从项目根目录的
`runtime_skills` 解析。任何 Skill 缺失或版本不匹配都会拒绝导入。Skill 内容
变更必须升级版本；平台禁止原地覆盖同一 `skill_id + version`。

v1 只接受 `topology: single_expert`。可以保留未来团队设计文档，但 `members`、
`delegation`、`delegation_policy` 和 `topology: team` 不能进入 v1 发布包。

业务人员只需维护 `AGENT.md` 和 `agent.yaml` 的授权 Skill/预算；模型、Shell、
沙箱和事实写权限均不属于专家包配置范围。
