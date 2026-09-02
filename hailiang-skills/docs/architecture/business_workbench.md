# AI 业务调试台

业务调试台为业务人员提供 Skill、专家、专家团三层对象的版本化编辑、验证、发布和生产迁移能力。入口为 `/workbench`，后端接口前缀为 `/workbench/v1`，生产部署接口前缀为 `/deployment/v1`。

## 版本模型

- 对象是全局逻辑身份，类型为 `skill`、`expert` 或 `expert_team`。
- 每次保存创建不可变修订 `r1/r2/...`。保存请求必须携带 `base_revision_id`，基线落后时返回 `409` 和字段差异。
- 发布把一个修订固化为不可变版本 `v1/v2/...`。单个已发布版本及其依赖只允许归档，只有在执行下述完整对象永久删除且不存在外部引用时才会随对象清理。
- 专家锁定一个或多个 Skill `release_id`；专家团锁定至少两个专家 `release_id`，并指定其中一个为主协调专家。
- 调试会话和生产会话都保存 `ResolvedConfigurationSnapshot`。已创建会话不随部署指针变化，新会话读取当前激活部署。

对象库同时提供永久删除入口。永久删除需要输入对象显示名称进行二次确认，并由服务端再次核对；成功后会清理该对象的修订、发布、文件、调试和评测记录，不能恢复，也不能使用同一个对象 ID 重建。如果仍有其他修订锁定其发布版本，或暂存/激活部署包含该对象，删除会被阻止并返回具体引用方。永久删除不改变“单个已发布版本不可就地修改”的规则。

## 共享运行内核

工作台和聊天服务通过同一个 Python 包加载 Skill Registry、专家 Runtime 和专家团 Runtime。业务版本保存 Prompt、规则、参数、引用资料、Skill Python 脚本和组合关系。平台级工具仍通过 `capability_id` 解析；Skill 脚本随修订和配置包版本化，但保存时必须通过语法与静态安全审查，运行时只能进入现有沙箱。

`GET /workbench/v1/kernel` 返回内核版本、能力目录及其摘要。配置包保存同一份内核指纹，生产导入会拒绝未知能力、哈希篡改、依赖缺失和不兼容内核。

当前文件系统中的 Skill、专家和专家团会在首次启动时导入为基线 `v1`。迁移期间可通过 `HAILIANG_WORKBENCH_BOOTSTRAP=false` 关闭自动导入；未激活工作台部署时，运行时继续使用原文件系统注册表。

## 发布和部署流程

1. 保存候选修订并完成校验。
2. 至少完成一次调试会话，或完成一次人工确认的用例运行。
3. 勾选人工确认后发布不可变版本。
4. 从发布中心导出 ZIP。专家和专家团会递归携带精确依赖闭包。
5. 在生产部署中心导入 ZIP。导入只创建 `staged` 部署，可先读取预览快照。
6. 激活时以事务切换根对象的生产指针。回滚切换到该部署记录的前一个快照，只影响之后创建的新会话。

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
- `HAILIANG_WORKBENCH_BOOTSTRAP`：是否导入文件系统基线，默认 `true`。
- `HAILIANG_WORKBENCH_SQLITE_PATH`：文件存储模式下的本地数据库路径。
- `HAILIANG_WORKBENCH_MAX_ASSET_BYTES`：单个资料文件上限，默认 20 MB。
- `HAILIANG_WORKBENCH_MAX_PACKAGE_BYTES`：配置包及解压内容上限，默认 100 MB。

正式环境使用 PostgreSQL，并通过 Alembic 迁移 `0002_business_workbench` 建表。文件存储模式仅用于本地开发和测试。

## 核心接口

- `POST /workbench/v1/actors`：登记用户名和设备操作人。
- `/workbench/v1/objects`、`/revisions`、`/releases`：对象、修订、差异和发布。
- `/workbench/v1/debug-sessions`：绑定候选快照的调试记录。
- `/workbench/v1/evaluation-suites`、`/evaluation-runs`：用例集、批量运行和人工结论。
- `POST /workbench/v1/exports`：导出已发布版本及依赖闭包。
- `/deployment/v1/imports`、`/deployments/{id}/activate|rollback`：生产暂存、预览、激活和回滚。

OpenAPI 中包含所有请求字段和错误响应；`409` 表示乐观锁或版本哈希冲突，`422` 表示业务校验或配置包安全校验失败。

## 后续待办

- 候选 Expert / 专家团测试的“语义切换确认”策略：当 Expert 判断某个已锁定 Skill 更匹配时，先输出绑定当前不可变快照的确认交互；只有用户明确确认后才切换。确认或拒绝、目标 Skill、切换原因和原 Skill 表单的挂起/失效状态都必须写入候选测试证据。当前行为仍为：Expert 自动切换到已锁定 Skill。
