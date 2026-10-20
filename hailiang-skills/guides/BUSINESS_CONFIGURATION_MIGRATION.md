# 业务配置数据库迁移与回退

## 日常怎么理解两套配置

业务配置（Skill / 专家 / 专家团）同时存在于：

1. **仓库目录**：`runtime_skills/`、`runtime_agents/`、`runtime_agent_teams/`  
2. **调试台数据库**：对象库里的修订与发布，以及激活后的部署 ZIP

本地或测试环境如果已经激活过专家团部署，**运行时只读部署快照**。仓库目录此时不是会话在用的那份；在文件里改专家规则、删专家包，都不会改变当前问答，除非你显式把 `HAILIANG_BUSINESS_CONFIG_SOURCE` 设为 `filesystem`，或重新做「迁移 / 导入 → 发布 → 激活」。

怎么快速确认本机在用哪套：

- 看 `/health/ready` 里的 `expert_runtime.default_expert_id`、团队规模，是否与工作台当前对象一致。  
- 看 `/deployment/v1/deployments` 是否存在当前环境的 active 部署。  
- 看工作台对象列表：那里才是业务日常编辑的权威内容。

仓库目录的定位是：新库冷启动兜底、迁移对照、测试与历史基线。验收完成后的第二阶段计划删除业务目录的文件加载分支；在此之前不要把「改仓库」当成「改线上」。

业务人员日常流程见 [AI 业务调试台业务使用手册](AI_WORKBENCH_BUSINESS_USER_MANUAL.md)。架构侧说明见 [AI 业务调试台架构与运行手册](../docs/architecture/business_workbench.md)。

## 配置边界

业务配置包括 Skill、专家和专家团。Skill 的 `SKILL.md`、`runtime_contract.json`、`references/`、`assets/`、`scripts/` 以及根目录数据文件（例如 `professions.json`、`tools.yaml`）均随不可变修订保存。表单引擎、Facts、SSE、专家调度、公共工具和脚本沙箱仍属于程序代码。

第一阶段保留文件目录用于迁移对照，运行来源默认自动选择；需要对照或回滚时可用 `HAILIANG_BUSINESS_CONFIG_SOURCE` 覆盖：

- `auto`（默认）：有 active 生产专家团时使用数据库部署快照；新服务器尚未完成首次“暂存导入 → 生产部署”时完整使用文件系统内置运行时。

- `filesystem`：只读取 `runtime_skills`、`runtime_agents`、`runtime_agent_teams`，用于迁移前对照。
- `database`：有 active 生产专家团时，读取数据库当前发布目录并挂载 active 部署 ZIP；不会把业务 Skill、专家或专家团从文件系统补进数据库运行时。平台内置的 `career_plan_entity`、`general_chat` 仅作为运行壳的必要底座，历史或精简配置包缺少它们时会从源码补齐；数据库中若存在同 ID 版本仍以数据库版本为准。新服务器尚未完成首次“暂存导入 → 生产部署”时，完整回退到 `filesystem`，以便先启动工作台完成初始化；一旦激活部署，后续新会话和重启均使用该部署快照。

回退是业务运行时来源的整套切换，不是按业务 Skill/专家逐项混合。只有 active 生产部署才会切换数据库业务运行时；运行壳的两个平台内置 Skill 是唯一允许的兼容性补齐项。一般部署不需要设置 `HAILIANG_BUSINESS_CONFIG_SOURCE`；保留该变量只是为了显式强制某个来源。

启动自动回灌默认关闭。禁止把 `HAILIANG_WORKBENCH_BOOTSTRAP` 当作持续同步机制。

## 迁移命令

先预检，不写数据库：

```bash
python scripts/migrate_business_catalog.py > migration-preview.json
```

报告列出对象、依赖、全部纳管文件、内容哈希和忽略规则。确认后执行：

```bash
python scripts/migrate_business_catalog.py --execute --actor-id migration-operator > migration-result.json
```

如果只需要同步 `runtime_skills`，不希望文件版专家或专家团影响数据库中正在编辑的对象，预检和执行时增加 `--skills-only`：

```bash
python scripts/migrate_business_catalog.py --skills-only > skill-migration-preview.json
python scripts/migrate_business_catalog.py --skills-only --execute --make-current \
  --actor-id migration-operator > skill-migration-result.json
```

目录迁移可直接重新使用此前永久删除过的对象 ID。删除审计保留用于追溯，但不会作为迁移阻塞条件。

上述执行只创建或复用不可变版本，不推进已有对象的当前发布指针。首次迁移并明确确认整套配置时使用：

```bash
python scripts/migrate_business_catalog.py --execute --make-current --actor-id migration-operator > migration-activated.json
```

需要同时生成可跨环境交付的完整专家团包时增加 `--export-dir`：

```bash
python scripts/migrate_business_catalog.py --execute --make-current \
  --export-dir ./migration-packages --actor-id migration-operator \
  > migration-activated.json
```

输出报告包含每个 ZIP 的路径与 SHA-256；ZIP 由当前发布专家团递归导出并执行依赖闭包冲突校验。

命令可重复执行：同 ID、同内容复用；同 ID、不同内容新增修订和发布。迁移前必须执行 Alembic 升级，并保存报告、数据库备份、专家团递归 ZIP 及其 SHA-256。

## 两类回退

对象“当前发布”与正式“当前生效部署”相互独立：

- `POST /workbench/v1/releases/{release_id}/make-current` 切换对象当前发布指针。请求携带 `expected_current_release_id` 做并发校验，不改写或删除历史版本。
- `POST /workbench/v1/releases/{release_id}/drafts` 从历史发布复制草稿；完整资产和依赖锁被复制，调试证据不继承。
- 正式部署的激活、下线、回滚和恢复只切换不可变专家团 ZIP，不改变工作台对象发布指针。

Skill 回退不会修改专家锁，专家回退也不会修改专家团锁。要整体恢复一组确定依赖，应切换专家团发布或恢复对应生产 ZIP。

## 上线顺序

1. 测试环境按 Skill → 专家 → 专家团逐层调试和发布。
2. 导出根类型为 `expert_team` 的完整递归 ZIP；浅层 `TEAM.md + team.yaml` 包不能上线。
3. 在线上暂存导入，检查团队、成员、Skill 与文件变化摘要。
4. 人工确认激活；线上运行严格使用本次 ZIP，不与线上对象库或文件目录混合。

专家和专家团的对象级 `brief` 是发布包 payload 与兼容 `agent.yaml` / `team.yaml` 的固定字段。旧 Schema v1 包未包含该字段仍可导入为历史内容；在工作台补齐 1–120 字的一行摘要并保存新修订后，才能再次发布。
5. 完成表单、Facts、资料、脚本、工具、专家转交和回退演练后，第二阶段才删除业务目录及文件加载代码。

已有会话在下一次非停止操作时比较 `deployment_id + package_hash`。发生变化后保留历史消息、已确认 Facts 和已提交答案，清理未完成表单、候选路径、转交卡及旧执行状态；仍在生成的回答不被中断。旧表单或旧转交提交返回 `409 CONFIGURATION_UPDATED`。
