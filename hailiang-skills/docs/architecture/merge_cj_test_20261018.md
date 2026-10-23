# cj-test 合并依据（2026-10-18）

## 基准与范围

- 目标分支：codex/subagent-development-zhangyi。
- 合并前目标：560e0625b6cf47decb329a801c7c28c5829264e7。
- 来源：cj-test，b4466a6a3233b8ca9eaeb355ea93baa32d18c3c5。
- 共同祖先：b7d8c5dbee2ec07c74707b33c43a87e1074d66e3。
- 用户授权合并，并要求保留依据。采用保留双亲的 merge commit，完整保留来源历史；不是 squash 或局部 cherry-pick。
- 纳入来源的 13 个提交，但纳入代码不等于启用 orchestrator_v2。未改变其默认关闭开关，未修改部署环境、现有会话模式，未执行数据库迁移或推送远端。
- 开始合并时主目录没有已跟踪文件的未提交修改。原有未跟踪调试文件、缓存和输出未纳入提交。聊天的另一个 detached worktree 未修改。

## 来源提交

| 提交 | 内容 |
| --- | --- |
| dbeaf11 | ConsultationPlan、jump_cards、max_parallel 协议 |
| cd2b324 | 规划头、汇总尾、推荐抽取系统 Skill |
| eb8c67d | 专家团并行配置和协作规则 |
| 0d7d344 | 身份、学段 facts 和前置表单 |
| 017aea7 | head、DAG 就绪调度、tail、推荐抽取模块 |
| 7cf2634 | 工作台编排规则发布和并行参数编辑 |
| baf1ad0 | orchestrator_v2 聊天 SSE 入口 |
| 4f2c6fa | Debug Zone 和 Harness 轨迹展示 |
| e3feca5 | 专家回复经 tail 收口 |
| e9d4b0a | 升学 Skill 瘦身、选科资产路径兼容 |
| 2b7e040 | 用户原话年级复用、问卷答案匹配 |
| a247019 | Skill 规范 v1.1 平台支持 |
| b4466a6 | runtime Skill 资产对齐 v1.1 |

## 冲突处理决策

唯一文本冲突文件：src/hailiang_skills/harness/runtime.py，public() 内两处。

1. 保留目标分支 SKILL_ASSET_SEARCH_TOOL 和 LEGACY_RAG_SEARCH_TOOL 的“检索 Skill 资料”名称。当前实现检索本地 Skill 资产，不应恢复为外部 RAG 服务的定义。
2. 接纳 cj 的 subject_requirements / subject_requirements_lookup 展示名称。
3. 保留目标分支工具 error 输出（包含 result.error 回退），保证失败可排查。
4. 接纳 cj 的 created_at 和节点时间排序。
5. 保留输入请求 owner_task_id / actor，保证追问所属专家可展示。

streaming_runner.py 自动合并；保留目标分支 560e062 的 supersede、部分回复保存、确认事实承接、Redis 收尾逻辑，并纳入 cj 的条件式新入口。文本自动合并不能代替端到端生命周期验证。

## 接受的收益与尚未解决的问题

已纳入：Skill 文本精简、年级复用、字段和工具依赖声明、工作台调试增强、编排规则持久化与发布、规划/汇总协议。

尚未解决：Harness 全量适用 facts 注入、read_facts 重复内容、工具历史逐轮累积、累计输入预算耗尽、超限后才摘要。fields.uses 的声明不代表 Harness 已按字段投影；旧 Skill 上下文仍保留 effective_fact_ledger。

禁止把本次合并当作“新编排已可上线”的依据：

- core/streaming_runner.py 调用 run_orchestrator_v2_turn 时未注入 HarnessChatDeps，默认 head/expert 是占位路径。审查时对齐身份、学段后复现输入“孩子高一，如何选科？”返回“（fallback_expert）孩子高一，如何选科？”并 completed。
- runtime_bridge/harness_chat_adapter.py 对 ready 任务同步循环调用 expert_runner，max_parallel 尚不代表真实并发。
- status != failed 被视为成功；真实 waiting_for_user 等状态尚未正确接入。失败任务加入 completed_ids，依赖不能区分成功与失败。
- 新适配器尚未连接原 Harness 的数据库作业、检查点、租约和定向恢复。身份表单不等于全局问题树或跨专家追问账本。
- 新增禁止公开内部错误的提示词只调整措辞，不会修复故障。调试应保留完整错误，回复仍需准确表达证据缺口。

## 本次合并后的验证

在目标目录以 PYTHONPATH=src 使用项目 .venv，明确测试合并后的源码。

1. 下列定向回归：112 passed，1 个 Starlette 弃用警告，退出码 0。

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_harness.py tests/test_harness_api.py tests/test_harness_policy.py tests/test_harness_publication.py tests/test_consultation_plan.py tests/test_orchestration_head.py tests/test_plan_executor.py tests/test_harness_chat_adapter.py tests/test_orchestrator_e2e.py tests/test_platform_gate.py tests/test_platform_skill_spec_p1_p11.py tests/test_single_chat_stream.py -q --tb=short
```

2. 当前分支新增的替代任务回归：2 passed，116 deselected，退出码 0。

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_runtime_bridge.py -k supersed -q --tb=short
```

该测试退出阶段有后台 sandbox 预热线程的 logging closed-stream / interpreter shutdown 错误；不能因此宣称真实 Docker sandbox 验证通过。

3. frontend 下 npm run check（tsc -b --noEmit）通过，退出码 0。
4. git diff --cached --check 通过；无未解决冲突。

原始测试输出保存于同目录 merge_cj_test_20261018_evidence/。未运行全部测试、真实模型对话回放、浏览器交互、实际 PostgreSQL 迁移与重启恢复测试。

此前对 cj 单分支定向测试为 37 passed；扩大至问卷为 68 passed / 7 failed。其中抽查共同基线确认 3 项原有失败，2 项新旧规范断言不兼容，其余未完成归因。这些历史结果不是本次合并后全量验证结果。

## 后续启用条件与合并规则

- HAILIANG_ORCHESTRATOR_V2 默认关闭；只冻结到新会话。外部环境显式设置为 true 或既有会话为 true 仍会进入新路径，本次未修改这些部署状态。
- 上线新增配置存储前执行并验证 Alembic 0009_orchestration_rules 迁移。
- 将 head 的 ConsultationPlan 接到现有持久化 Harness，tail 作为等待必要依赖的总结节点；避免另建同步执行内核。
- 去掉生产占位回退；缺执行器应明确失败。
- 验证真实专家并行、多个专家排队追问、多轮和跳过、失败依赖、取消迟到结果、断线和 worker 重启。
- 增加上下文分项计量、事实投影、结构化专家结果，按同模型同配置进行 token 与重复追问对比。
- 后续合并应同时保留工具错误、专家归属和时间排序，不用整块 ours/theirs 覆盖 public()。
- 审查来源差异可用 git diff b7d8c5d b4466a6；审查本次相对目标的结果应比较本 merge 的第一父提交。原始 13 个提交保留供逐项追溯。
