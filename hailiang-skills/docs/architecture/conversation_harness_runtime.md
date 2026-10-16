# Conversation Harness v1

## 实现入口

`src/hailiang_skills/harness/` 提供独立执行内核。主 agent 使用 `complete_with_tools` 提交专家任务，专家自主选择授权 Skill 或直接分析。Skill 结果返回调用它的专家，专家继续分析后才返回主 agent。主 agent 的独立 Skill 来自 `config/runtime.yml` 的显式列表；目前列表为空，因为现有业务 Skill 已归属专家。别名先规范化再检查授权，目录不列出禁用 Skill。工作台保存、发布以及启动快照都会检查独立与内部 Skill 的冲突。

公共输入、事实和检索工具由执行主体及配置快照决定。Skill 的原生工具沿用既有实现，阶段状态独立保存。Skill 文件压缩保存到任务配置快照，并按摘要恢复到 `HAILIANG_STATE_DIR/harness-assets`，不会在执行期间换用后来发布的规则或脚本。

模型、温度、端点及生成参数同样冻结在任务快照中；密钥只按环境变量名在执行时读取，不保存到快照。

## 部署

先在目标数据库执行 `alembic upgrade head`。迁移 `0008_conversation_harness` 增加 run、task、tool、input、checkpoint、job、event、execution attempt 和 fact 九张表。文件模式使用 `HAILIANG_STATE_DIR/harness.sqlite3`；SQLite 事务使用 WAL 与写锁，PostgreSQL 使用任务行锁、事实事务锁、唯一活动会话约束。

`HAILIANG_HARNESS_NEW_SESSIONS=true` 是默认值。模式记录在新会话的会话级 `runtime_mode` 中；没有标记的旧记录始终作为 legacy。关闭开关只影响以后创建的会话，不会把已有 Harness 会话切回旧链路。

开发模式默认内嵌调度 worker，SSE 连接与执行独立。生产设置 `HAILIANG_HARNESS_EMBEDDED_WORKER=false`，安装 `deploy/systemd/hailiang-skills-harness@.service` 与更新后的 API unit，并执行 systemd daemon reload。API unit 的 Wants/PartOf 关系管理同环境 worker。也可直接运行：

```bash
python -m hailiang_skills.harness.worker
```

worker、API 使用相同 PostgreSQL、配置及模型/检索环境。Redis 通过带环境前缀的 Pub/Sub 通知唤醒；消息丢失或 Redis 暂时不可用时，worker 回到数据库轮询。数据库是作业与事件的权威存储。默认租约 90 秒，每 20 秒续约；过期任务可以被新执行领取，旧执行版本不能发布结果。暂停或重启 worker 不会取消会话任务。

默认每个整体任务同时执行至多 3 个节点；每节点 24 次模型、48 次工具调用，每整体任务 96 次模型、192 次工具调用，最多 64 个任务节点。配置位于 runtime.yml。上下文预算默认 32000 估算 token，不代表供应商计费 token。预算不足会压缩已完成的历史轮次；尚未解决的原生工具调用和最新轮次保留完整配对。大工具结果向模型投影有限节选，原文保留在工具记录中，节选带原工具 ID 与裁剪标记。

检索最多尝试三次。副作用工具使用稳定的 harness idempotency key；不支持核对/幂等的工具在崩溃后进入 outcome unknown，不会自动重放或换调用 ID 重做同一操作。

## API 与交互

接口均以 `/api/v2` 为前缀：

| 方法与路径 | 用途 |
|---|---|
| GET `/sessions/{id}/harness` | 模式、按孩子过滤的任务快照、重放游标 |
| POST `/sessions/{id}/harness/runs` | `goal`、`context_data`，可选专家/团队及工作台快照 |
| GET `/sessions/{id}/harness/runs/{run_id}` | 单任务执行树与卡片 |
| GET `/sessions/{id}/harness/events?after={seq}` | SSE 重放；支持 Last-Event-ID |
| POST `/sessions/{id}/input-requests/{request_id}/answers` | 提交答案，不调用普通聊天接口 |
| POST `/sessions/{id}/harness/runs/{run_id}/cancel` | 幂等停止整棵执行树 |

答案包含 `user_id`、`profile_id`、`submission_id`、`expected_version`，以及 `answers`、单字段 `text` 或 `skipped=true`。事务同时保存答案、事实版本、请求状态、工具结果和恢复作业。重复提交相同 ID/内容返回原结果；更改内容、旧版本及失效请求被拒绝。API 校验任务用户/孩子，已有孩子必须属于当前用户。运行期间普通会话写入被拒绝；前端禁用输入、专家/团队与档案切换，停止按钮和卡片保持可用。

一个 run 维护一个工作气泡，树中展示排队、依赖、执行、等待用户、重试与终态。待答卡片放在可折叠树之外，按所属专家显示；多轮记录可回看。草稿与提交标识在 sessionStorage 中保存。刷新先取快照再订阅；旧版本快照不会倒退状态，断线只改变连接提示。只有主 agent 的结论对外展示，专家结果、提示词、原始工具参数保持内部。

`/api/v1/external/chat` 每次创建新会话。当开关启用时改为 HTTP 202，返回 run/session/user/profile 标识及 v2 快照与事件地址；即使传入 `stream=true`，也返回任务受理 JSON，客户端需转订 v2 SSE 并通过 v2 回答接口补充信息。接入方需要升级这一契约。关闭新会话开关时保留原同步/流式响应。外部请求中的历史对话作为近期消息投影；不能使用旧接口继续一个 Harness 任务。

工作台的 Harness 候选测试面板使用独立测试身份与锁定快照，支持任务树、回答、停止及调用追踪。内部 Skill 候选通过只授权该 Skill 的测试专家执行，不能临时扩大主路由权限。用户可以填写测试结论并保存为调试依据，保存不会自动发布。完整 trace 仅限非生产环境的工作台测试会话。

## 上下文与事实

composer 集中投影公共规则、角色、任务目标、相关事实版本、问答账本、历史摘要和近期消息/工具证据；摘要也经过同一 composer 与预算准入。专家不会收到其他专家的完整历史。问题账本和已答事实向所属 Skill 继承；相同共享字段可复用待答请求。已知字段从新卡片中剔除，跳过的字段不会无故再次登记。

事实支持用户、孩子、会话及任务范围，保存版本、来源、历史替代值、候选/假设/派生状态。模型推断不能覆盖用户确认值。`user_statement` 必须引用原始用户文本中的逐字证据，值也必须出现于证据；这证明用户来源，不证明外部真实性或语义抽取永不出错。结构化回答以字段 schema 校验。用户更正使依赖结果需要复核，正在生成的旧版本响应被拒绝；过期事实以墓碑状态传入，旧摘要不能把已删除值恢复。旧 FactService 读写接入版本事实投影，保持档案面板与新执行链路一致。

## 验证记录（2026-10-09）

```bash
.venv/bin/python -m pytest tests/test_harness.py tests/test_harness_api.py tests/test_harness_publication.py tests/test_harness_policy.py tests/test_external_chat.py -q
HAILIANG_HARNESS_TEST_DATABASE_URL=<isolated-harness_test-url> .venv/bin/python -m pytest tests/test_harness.py tests/test_harness_api.py -q
HAILIANG_HARNESS_TEST_REDIS_URL=<integration-redis-url> .venv/bin/python -m pytest tests/test_harness_wakeup.py -q
cd frontend
npm run build
npm test -- src/components/message-blocks/TaskProgressBlock.test.tsx
```

本次测试覆盖授权/别名、独立与内部 Skill 归属、并行等待、两专家独立卡片与定向恢复、共享和多轮问题、跳过、并发答案、版本拒绝、事实更正/候选隔离、原生工具结果配对、检索重试、SSE 断开与游标重放、迟到响应、实际进程崩溃及租约恢复、API 隔离、冻结资产/模型和发布冲突。SQLite Harness 29 项、外部接口 6 项通过；实际独立 PostgreSQL 执行/API 27 项通过。迁移在独立 PostgreSQL 上完成升级与降级验证。隔离 Redis 的跨 worker 唤醒 1 项通过。前端卡片 4 项、工作台 2 项通过，类型检查和生产构建通过。

旧链路选定回归 158 项通过、15 项失败；在干净 HEAD 上运行相同集合有完全相同的 15 个失败。前端全量 18 项通过、1 项失败，干净 HEAD 同样存在主题徽章 accessible name 的失败。没有将这些基线问题改写成 Harness 测试通过。

实际模型回放入口：

```bash
.venv/bin/python scripts/replay_harness.py --env-file env.local.sh --output output/harness-replay.json
```

脚本只解析指定模型的密钥 export，不执行环境脚本。它使用相同模型、温度和业务资产对比 legacy 与 harness，每个案例隔离虚构用户事实，并保存最终文本、工具和问题记录。样例包含已知信息不重复问、两专家并行、两轮结构化输入恢复。回放曾暴露 Skill 重问祖先任务字段、禁用 Skill 出现在目录、证据过大和字数超限；已增加对应修正与测试。长度检查最多修正一次，仍不符合时显式失败，不能伪造缺信息问题。

这些小样例不能证明所有业务对话质量。发布前需要在部署环境复核 BFF 身份/孩子映射、真实搜索供应商、SSE 代理、worker 服务安装和更大规模用户问题回放。尚未实际发布生产服务，不把测试环境运行记录当作生产故障演练通过。

## A01–A15 证据与验收边界

以下测试位于 `tests/test_harness.py`，API 位于 `tests/test_harness_api.py`，前端卡片位于 `frontend/src/components/message-blocks/TaskProgressBlock.test.tsx`。数据库场景同时在 SQLite 和实际 PostgreSQL 上验证。

| 条目 | 当前证据 | 仍需部署环境或质量验收 |
|---|---|---|
| A01 | 权限、依赖循环、独立 Skill/专家归属测试；真实双专家分派回放 | 业务任务集的拆解质量 |
| A02 | `expert_parallel_wait_and_multi_round_resume` | 生产多 worker 压力 |
| A03 | `two_expert_cards_resume_only_the_answered_node`；前端独立表单交互 | BFF/浏览器完整链路 |
| A04 | 多轮恢复测试；真实 `expert_multi_round` 回放两轮均属于专家 | 长对话样本 |
| A05 | 两独立卡片定向恢复、Skill 祖先答案复用 | — |
| A06 | 并发提交、幂等、旧版本、取消请求拒绝 | 网关网络重试 |
| A07 | 实际子进程崩溃、租约恢复、SSE 生成器断开重放、草稿及旧快照测试；Redis 唤醒 | 生产代理 SSE、真实服务重启/安装 |
| A08 | `fact_correction_reviews_result_before_main_summary`、用户原文来源校验 | 档案 UI 与 BFF 更正全链路 |
| A09 | API 用户/孩子隔离、独立任务/Skill 状态、删除旧上下文后隔离 | 真实账号映射 |
| A10 | 祖先问题账本、跳过复用、部分已知字段剔除、事实墓碑、有限证据投影 | 跨大量压缩轮次的真实模型回放 |
| A11 | 模型失败无卡片、检索有限重试、未明副作用不重放 | 搜索供应商故障演练 |
| A12 | 整树取消、迟到响应拒绝、跳过恢复；运行中改目标须先停止再启动 | 必需信息不足时的业务结论质量 |
| A13 | 原生工具配对、Skill 本地资料搜索、检索重试不重跑其他工具、授权预算 | 真实 Web Search 供应商 |
| A14 | 等待节点不能完成根任务；失败/受限结果提供主 agent 汇总 | 主 agent 对限制说明的业务评审 |
| A15 | 同模型/温度虚构样本对照及回放记录，修复重复字段/禁用目录/字数问题 | 大规模基线与量化质量阈值，尚未全量签收 |

代码和上述本地验证已交付；全部部署与质量条目完成前，不标记整份改造计划验收完成。
