# 业务配置数据库迁移与回退

## 配置边界

业务配置包括 Skill、专家和专家团。Skill 的 `SKILL.md`、`runtime_contract.json`、`references/`、`assets/`、`scripts/` 以及根目录数据文件（例如 `professions.json`、`tools.yaml`）均随不可变修订保存。表单引擎、Facts、SSE、专家调度、公共工具和脚本沙箱仍属于程序代码。

第一阶段保留文件目录用于迁移对照，但运行来源必须通过 `HAILIANG_BUSINESS_CONFIG_SOURCE` 明确选择：

- `filesystem`：只读取 `runtime_skills`、`runtime_agents`、`runtime_agent_teams`，用于迁移前对照。
- `database`：只读取各对象的 `current_release_id`，不借用同名文件补字段。缺少 `general_chat` 或 `career_plan_entity` 当前发布时启动失败。

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
