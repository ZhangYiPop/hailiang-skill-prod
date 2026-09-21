# AI 业务调试台

业务调试台为业务人员提供 Skill、专家、专家团三层对象的版本化编辑、验证、发布和生产迁移能力。入口为 `/workbench`，后端接口前缀为 `/workbench/v1`，生产部署接口前缀为 `/deployment/v1`。

## 版本模型

- 对象是全局逻辑身份，类型为 `skill`、`expert` 或 `expert_team`。
- 每次保存创建不可变修订 `r1/r2/...`。保存请求必须携带 `base_revision_id`，基线落后时返回 `409` 和字段差异。
- 发布把一个修订固化为不可变版本 `v1/v2/...`。单个已发布版本及其依赖只允许归档，只有在执行下述完整对象永久删除且不存在外部引用时才会随对象清理。
- 专家锁定一个或多个 Skill `release_id`；专家团锁定至少两个专家 `release_id`，并指定其中一个为主协调专家。
- 只有一个锁定 Skill 的独立专家会确定性地直接进入该 Skill；Skill 进入后仍按自身运行契约选择 RAG、MCP、Web Search、脚本、资料和表单等能力。多成员专家团的主协调专家保留团队内专家选择与转交能力，不适用该快捷入口。
- 专家团成员修订保存 `mention_name`（团队内唯一的展示名称）和 `routing_brief`（职责摘要）。主协调专家对明确专项问题必须生成现有 `team_handoff` 确认卡：单一专项给一个候选，多个合理专项给 2～3 个候选；问候、泛泛迷茫或信息不足才直接澄清。工具参数可安全规范化精确 ID、唯一展示名称或唯一专家名称（可带 `@`）；未知或歧义名称记录 `team_handoff_rejected` 并拒绝猜测。
- 候选修订手动测试与正式 SSE 共用同一条受控移交链路，并始终从本次请求绑定的候选/部署快照解析成员，不依赖进程级全局专家注册表。主协调专家会获得有界的最近对话历史。移交工具异常、非法候选或 ReAct 迭代上限不会再显示框架的“本轮没有完成处理”兜底；服务端优先重新生成受控候选卡，否则由主协调专家进行一次无工具的上下文续答，动态询问真正缺失的信息，不使用固定分类话术。候选 Trace 和正式事件均保留分流、规范化和拒绝原因。
- 每个对象用 `current_release_id` 指向当前发布；切换历史版本不改写版本号或依赖锁。
- 调试会话保存指定修订快照，生产会话保存部署快照。部署变化后，已有会话在下一次操作切换，正在生成的回答继续完成。

对象库同时提供永久删除入口。永久删除需要输入对象显示名称进行二次确认，并删除对象、修订、发布版本与资产；同类型 ID 后续可直接由创建或 ZIP 导入重用。删除审计仅用于追溯，不形成 ID 墓碑。引用检查只读取上游对象的当前发布；历史修订和生产部署 ZIP 不阻止删除。生产配置来自不可变 ZIP，不会因对象库删除而变化。

递归导入专家团时，包内已有但已归档的 Skill、专家或专家团会自动恢复为未归档状态；导入结果返回 `unarchived_objects`，前端会明确提示恢复数量，确保整个依赖闭包在默认对象库中可见、可编辑。

## 共享运行内核

工作台和聊天服务通过同一个 Python 包加载 Skill Registry、专家 Runtime 和专家团 Runtime。业务版本保存 Prompt、规则、参数、引用资料、Skill Python 脚本和组合关系。专家和专家团另有必填的对象级 `brief`（1–120 字、单行、面向用户展示），它随修订、发布、ZIP 和部署快照流转；团队成员的 `routing_brief` 只用于团内分流及转交卡，不能替代对象级 `brief`。平台级工具仍通过 `capability_id` 解析；Skill 脚本随修订和配置包版本化，但保存时必须通过语法与静态安全审查，运行时只能进入现有沙箱。

`GET /workbench/v1/kernel` 返回内核版本、能力目录及其摘要。配置包保存导出端内核指纹，供审计与排查；导入不要求两端指纹完全相同。Skill 脚本能力按包内已声明且经审查的脚本校验，不依赖接收端的全局文件目录。生产导入仍会拒绝未随包提供的能力、哈希篡改、依赖缺失、Schema 不兼容及未通过安全审查的脚本。

启动自动回灌默认关闭。第一阶段用 `scripts/migrate_business_catalog.py` 预检并幂等迁入数据库；运行时默认使用 `HAILIANG_BUSINESS_CONFIG_SOURCE=auto`：有 active 生产专家团时使用数据库部署快照，没有 active 部署时完整回退到文件系统，方便新服务器先启动工作台完成首次导入和生产部署。也可显式指定 `filesystem` 或 `database` 进行对照和回滚；激活部署后只使用不可变部署快照，不与文件系统对象逐项混合。验收完成后的第二阶段删除业务目录和文件加载分支。

## 发布和部署流程

1. 保存候选修订并完成校验。
2. 至少完成一次调试会话，或完成一次人工确认的用例运行。
3. 勾选人工确认后发布不可变版本。
4. 从发布中心导出 ZIP。专家和专家团会递归携带精确依赖闭包。

候选修订手动测试使用独立 SSE 通道。模型单次调用默认最多等待 60 秒、最多请求 2000 tokens（可通过 `HAILIANG_WORKBENCH_CANDIDATE_LLM_TIMEOUT_S` 与 `HAILIANG_WORKBENCH_CANDIDATE_LLM_MAX_TOKENS` 调整）；等待期间服务端发送 `ping` 保活。超时会以终态 `MODEL_TIMEOUT` 返回，前端必须结束加载并允许用户重试。
5. 在生产部署中心导入 ZIP。导入只创建 `staged` 部署，可先读取预览快照。
6. 激活时以事务切换根对象的生产指针。回滚、恢复或下线只切换部署快照；已有会话在下一次操作同步，旧表单和转交卡失效。

配置包包含 `manifest.json`、声明式对象配置、依赖锁、`references/` 资料和 `scripts/` Python 脚本。专家包还包含 `agent.yaml`、`AGENT.md`、`skills.lock.json`；专家团包包含 `team.yaml`、`TEAM.md`、`experts.lock.json`。生产导入会再次执行脚本安全审查；相同版本及哈希的重复导入是幂等操作，同版本但不同哈希会被拒绝。

## 运行方式

开发环境可使用现有 API 进程，或单独启动工作台入口：

```bash
python -m uvicorn hailiang_skills.api.workbench_main:app --host 127.0.0.1 --port 8001
```

生产环境提供 `deploy/systemd/hailiang-skills-workbench@.service`，它与聊天服务使用同一发布目录和虚拟环境，但作为独立进程运行。前端运行时配置支持：

```json
{
  "apiBaseUrl": "/api",
  "workbenchApiBaseUrl": "/api"
}
```

主要环境变量：

- `WORKBENCH_PORT`：独立工作台进程端口。
- `HAILIANG_BUSINESS_CONFIG_SOURCE`：可选来源覆盖，支持 `auto`（默认）、`filesystem`、`database`；`auto`/`database` 无 active 部署时完整回退文件系统，激活后使用部署快照，不做跨来源字段补齐。
- `HAILIANG_WORKBENCH_BOOTSTRAP`：遗留基线导入开关，默认 `false`；迁移应使用显式命令。
- `HAILIANG_WORKBENCH_SQLITE_PATH`：文件存储模式下的本地数据库路径。
- `HAILIANG_WORKBENCH_MAX_ASSET_BYTES`：单个资料文件上限，默认 20 MB。
- `HAILIANG_WORKBENCH_MAX_PACKAGE_BYTES`：配置包及解压内容上限，默认 100 MB。

正式环境使用 PostgreSQL，并通过 Alembic 迁移到 `0005_current_release`。文件存储模式仅用于本地开发和测试。

## 核心接口

- `POST /workbench/v1/actors`：登记用户名和设备操作人。
- `/workbench/v1/objects`、`/revisions`、`/releases`：对象、修订、差异、当前发布和历史恢复。
- `/workbench/v1/debug-sessions`：绑定候选快照的调试记录。
- `/workbench/v1/evaluation-suites`、`/evaluation-runs`：用例集、批量运行和人工结论。
- `POST /workbench/v1/exports`：导出已发布版本及依赖闭包。
- `/deployment/v1/imports`、`/deployments/{id}/activate|rollback`：生产暂存、预览、激活和回滚。

OpenAPI 中包含所有请求字段和错误响应；`409` 表示乐观锁或版本哈希冲突，`422` 表示业务校验或配置包安全校验失败。

## 后续待办

- 候选 Expert / 专家团测试的“语义切换确认”策略：当 Expert 判断某个已锁定 Skill 更匹配时，先输出绑定当前不可变快照的确认交互；只有用户明确确认后才切换。确认或拒绝、目标 Skill、切换原因和原 Skill 表单的挂起/失效状态都必须写入候选测试证据。当前行为仍为：Expert 自动切换到已锁定 Skill。
