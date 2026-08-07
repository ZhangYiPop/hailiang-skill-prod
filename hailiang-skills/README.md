# hailiang-skills

面向升学咨询场景的 Skill Runtime 融合版对话引擎。

当前项目已经从原先 hailiang 自研 `RouterSkill -> FactsExtractorSkill -> PlannerSkill -> 业务 Skill` 的主链路，升级为 `Intent Router -> skill-runtime` 的前置路由架构。新会话先进入 `general_chat`；用户意图命中专项场景时，当前轮仍由自由问答回答，并在 assistant 回复下展示 `route_suggestions` 按钮，用户点击后才进入目标 Skill。原 `main_planner` 的公开 Skill ID 已统一为 `career_plan_entity`（升学规划顾问），它现在是可被推荐和直达的专项 Skill，同时保留画像问诊和子场景引导能力。`main_planner` 仅作为历史数据与旧请求的兼容别名。

更细的架构和新增场景接入步骤见：

- [Skill Runtime 融合架构与新增场景接入手册](docs/architecture/skill_runtime_integration.md)
- [Skill 标准与渐进式 Prompt 约定](docs/architecture/skill_standard.md)
- [对话 SSE 完整数据流](docs/architecture/conversation_sse_dataflow.md)
- [API 文档](guides/API_DOCUMENTATION.md)
- [前端交互与渲染清单](guides/FRONTEND_INTERACTION_CHECKLIST.md)
- [SSE 前端联调示例](guides/SSE_FRONTEND_MOCK_EXAMPLES.md)
- [算法后端整体架构](docs/architecture/ALGORITHM_BACKEND_ARCHITECTURE.md)

其中对话链路、SSE 协议、事件顺序、表单/按钮/转场/取消/风控等完整说明，统一以 `docs/architecture/conversation_sse_dataflow.md` 为准。
前端如果需要直接做 parser、状态机和 UI mock，优先参考 `guides/SSE_FRONTEND_MOCK_EXAMPLES.md`。

最近一轮前后端联调中，围绕 `run_id / message_id / source_message_id` 的语义区别、toolbar 与推荐卡片进入 Skill 的请求差异、退出 Skill 的请求与返回、以及表单提交后“交互状态同步 + chat 续发”的双阶段链路，已补充到：

- [API 文档](guides/API_DOCUMENTATION.md)
- [SSE 前端联调示例](guides/SSE_FRONTEND_MOCK_EXAMPLES.md)
- [SSE v2 前端对齐协议](guides/SSE_RESPONSE_CONTRACT.md)

前端或 BFF 做接口联调时，建议优先看这三份文档，而不是只看单个 mock 片段。

如果前端需要基于后端真实出流做联调，可以在 `config/runtime.yml` 中打开：

```yaml
sse_recording:
  enabled: true
  root_dir: logs/sessions
  format: jsonl
```

开启后，后端会把真实发给前端的 SSE 线协议明文落到本地：

- `logs/sessions/{session_id}/sse/{run_id}.jsonl`：单轮真实流
- `logs/sessions/{session_id}/sse/session_stream.jsonl`：同一会话下串起全部 `run_id` 的总流

每条记录同时保留 `raw_sse` 和解析后的 `payload`，便于直接回放、grep 和转成前端 mock 数据。

如果后端已经打开 SSE 录制，后续排查某次 `chat/stream` 的真实推流，推荐直接用：

```bash
python3 scripts/show_sse_trace.py \
  --session-id 193477649811558401 \
  --run-id 96944207-59f0-4188-bf6a-7d830e7fbe6d \
  --source-message-id msg_94c5b56f823d4d2b \
  --target-skill-id multi_path_planning
```

这个脚本会同时搜索三类日志：

- `logs/sse_recording/sessions/{session_id}/sse/{run_id}.jsonl`：单轮真实 SSE 推流
- `logs/sse_recording/sessions/{session_id}/sse/session_stream.jsonl`：同 session 聚合流
- `logs/sessions/{session_id}/events.jsonl`：事件日志，包括 `skill_transition_requested`

常用参数：

- `--raw`：打印命中的原始 JSON 行
- `--show-head 20`：先看目标文件前 20 行
- `--all-lines`：不按关键词过滤，直接浏览整份目标文件
- `--keyword xxx`：追加自定义关键词，可多次传入

相比手写 `grep`，这个脚本更适合云端机器上没有 `rg` 的场景，也更方便按 `session_id / run_id / source_message_id / target_skill_id` 组合排查。

## 当前能力

### Runtime 原生场景

这些场景由 `runtime_skills/*/SKILL.md` 和 `runtime_contract.json` 驱动，走 skill-runtime 的状态、路由、tool、asset lookup 和 `status_track` 协议。`SKILL.md` 现在承载主流 Agent Skills 风格的 metadata，例如 `skill_type`、`entrypoint_role`、`prompt_loading`、`retrieval`、`assets`、`debug`；`runtime_contract.json` 继续承载 stage/facts/routes 等运行时契约。

| Runtime Skill | 场景 | 目录 |
| --- | --- | --- |
| `career_plan_entity` | 升学规划顾问 / 可进入专项 Skill | `runtime_skills/main_planner/`（历史目录名） |
| `future_explore` | 前景探路 | `runtime_skills/future_explore/` |
| `score_improve` | 提分规划 | `runtime_skills/score_improve/` |
| `interest_explore` | 兴趣探索 | `runtime_skills/interest_explore/` |
| `subject_advisor` | 选科参谋 | `runtime_skills/subject_advisor/` |
| `junior_multi_path_planning` | 初中多元路径规划 | `runtime_skills/junior_multi_path_planning/` |

### Hailiang 旧业务桥接场景

这两个场景以 runtime Skill 形式被 `career_plan_entity` 发现和路由，但实际业务仍调用 hailiang 原来的规则型 Skill。

| Runtime Skill | 实际执行 Skill | 业务 |
| --- | --- | --- |
| `mock_admission` | `admission` | 模拟升学 |
| `multi_path_planning` | `convergence` | 高中多元路径规划 |

这样可以做到：

- 明确需求先由 `general_chat` 回答并生成目标 Skill 按钮，点击后再进入对应 Skill
- `career_plan_entity` 保持画像问诊、顾问判断和子场景推荐能力，但不再是默认入口
- 原有模拟升学、多元路径规划能力不丢
- 原来的结构化资产、候选路径、引用、表单和调试态继续可用
- 后续新增场景可以选择 runtime 原生或 hailiang 规则型两种接法

前置 `Intent Router` 会读取各 runtime skill 的 `SKILL.md` frontmatter 中的 `routing` 配置，包括 `scene_name`、`routing_examples`、`slot_facts` 和可选学段范围。`routing_examples` 只用于判断“该进哪个 skill”，不负责拦截缺失 facts；进入目标 skill 后，再由目标 skill 自己根据 `runtime_contract.json` 的 stage / required facts 追问槽位。

路由层支持可选 embedding 召回：当 `config/llm/qwen_dashscope.json` 中 `embedding.enabled=true` 且 `DASHSCOPE_API_KEY` 可用时，会用 DashScope compatible `/embeddings` 接口和 `text-embedding-v4` 对用户输入与各 skill 的 `routing_examples` 做语义相似度匹配。embedding 调用失败、无 key 或低置信时，会自动降级到配置文本匹配和保守兜底，不影响本地测试和基础路由。

开启 embedding 后，`main_planner.intent_route.debug_payload` 和 `main_planner_route` 事件会额外记录路由调试明细，包括是否成功构建 query embedding、每个 skill 的 `best_text_score` / `best_embedding_score` / `final_score`、以及命中最高的样本文本，便于直接在 `snapshot.json` 和 `events.jsonl` 中核对语义召回效果。若某轮在 embedding 比较前就被前置规则短路，`debug_payload.routing_short_circuit` 会继续记录 `stage / rule / reason / details`，例如长画像进入 `main_planner` 时会带上字符数、命中画像关键词和分句数等判定细节。

如果聊天模型和 embedding 模型不走同一个网关，可以在 `embedding.base_url` 单独配置 embedding 的兼容接口前缀；运行时会继续用顶层 `base_url` 访问 `/chat/completions`，并用 `embedding.base_url` 访问 `/embeddings`。未显式配置 `embedding.base_url` 时，会自动回退到顶层 `base_url`。

当前第一版 embedding 还支持路由样本本地缓存：初始化时会把各 skill 的 `routing_examples / triggers / accepts_scenes / main_planner.routes` 汇总成样本，对未命中的样本按 `embedding.max_batch_size` 分批请求远程 embedding，并把结果写入 `Path.cwd()/.skill_runtime_cache/intent_router_embeddings/`。同一条样本会按 `skill_id + source + text + model + base_url` 生成稳定 hash，后续启动时优先复用本地缓存，只对新增或变更样本重新向量化，并在写回时清理已删除的历史样本。第一版只缓存 route examples，不缓存用户 query。

```json
{
  "embedding": {
    "enabled": true,
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "text-embedding-v4",
    "api_key_env": "DASHSCOPE_API_KEY",
    "timeout_s": 8,
    "max_batch_size": 10,
    "cache_enabled": true
  }
}
```

场景锁分两类：

- `consultative`：`career_plan_entity` 顾问锁。用户点击进入生涯规划后，补充年级、成绩、特长等画像信息不会被关键词自动切走，直到用户点击顾问推荐的子场景
- `task`：child skill 任务锁。进入具体场景后，补 facts、普通追问和旁支闲聊默认留在当前 skill；只有明确“换到/先看/继续刚才/回到”等切换语义才会跳转

如果用户一次性输入很长一段、多维度的孩子情况，例如同时包含年级、成绩、兴趣特长、学习状态、选科或专业困惑，Intent Router 会先在 `general_chat` 轮次记录候选；用户点击“升学规划顾问”后，再由 `career_plan_entity` 生成画像简历、判断主矛盾并推荐子 Skill。这个规则由 `planner.intent_router.long_profile_message_min_chars` 和 `long_profile_signal_threshold` 控制；结构化表单式补 facts 仍按续答处理，不会因为内容较长而误跳。

`career_plan_entity` 当前仍保留多元路径规划的自然表达识别，例如“除了普通高考还有什么路”“还有哪些升学路径”等表达；在 `general_chat` 中这类表达先产生多元路径按钮，进入升学规划顾问后再继续做画像和方向引导。

新增 child skill 时，优先在 `SKILL.md` frontmatter 里补充：

```yaml
routing:
  scene_name: 选科参谋
  intent_clarity: explicit
  routing_examples:
    - 高一怎么选科
    - 物化生和物化地怎么选
  # slot_facts 只是给目标 skill 自查和追问槽位用，router 不会用它拦截进入。
  slot_facts: [grade, province, candidate_subjects]
  school_stage_scope: senior
```

`subject_advisor` 的专业选科要求不直接把长 Excel 塞进 prompt。`docs/选科规划/2021年全国选科通用指引-20260611.xlsx` 已通过 `runtime_skills/subject_advisor/scripts/build_subject_requirement_asset.py` 转成结构化资产 `runtime_skills/subject_advisor/assets/subject_selection/subject_requirements.json`，字段语义见 `references/subject_requirement_asset.md`。当用户询问医学/工科/法学/计算机等专业方向、医生/程序员/律师/警察等职业目标，或“物化组合能报什么”“只选物理能不能读计算机”这类组合覆盖问题时，runtime 会向该 skill 暴露 `subject_requirements` 工具，由 `scripts/subject_requirements_lookup.py` 按专业、职业或选科组合查询命中记录，再交给模型做顾问解释。该资产只代表 2021 通用最低要求，具体省份、年份、院校和专业组仍需核实当年本省考试院和高校招生章程。

`career_plan_entity` 现在还支持基于 `runtime_skills/main_planner/references/06_用户画像&规划策略&可探索场景.md` 的画像矩阵推荐。它在用户点击生涯规划按钮后读取 `SKILL.md` 中的 `planner.scene_selection` 配置，结合已知 `grade / score_level / talent` 与当前用户表达，给出推荐 scene，再通过子 skill 的 `accepts_scenes / triggers` 和 runtime route registry 生成后续按钮。这样后续新增升学子 skill 时，优先补 metadata 和 contract，而不是继续在代码里堆硬编码关键词。

每轮 assistant 主回复完成后，后端会发起一次二次子 LLM 解析，判断主回复中是否清晰指向可继续进入的子 Skill。如果命中，会把结果结构化为 `route_suggestions`，前端会在 assistant 气泡下方展示“可进一步选择的规划主题”按钮卡片。这类建议的 `suggestion_source=llm_reply_analysis`，不要求 `is_final_summary=true`，也不会触发 `skill_finalized`；只有真正 Phase 5 总结或满足 final summary 判断时，才会记录 `skill_finalized`。

子 LLM 不直接切换 active skill，也不写 `scene_lock` / `consultative_lock` / `resume_to_skill_id`。`route_suggestions` 与真实路由仍然分离：用户点击某个按钮后，前端发送 `requested_target_skill_id` 和 `handoff_context`，后端仍会交给 `main_planner`、场景锁和 facts 状态做最终裁决。对多元路径场景，当前规则是：`multi_path_planning` 只用于高中升大学 / 高考多元路径；如果已知孩子是初中，`多元路径规划` / `初中多元路径规划` 会归一到 `junior_multi_path_planning`。如果子 LLM 不可用，仅保留 `进入【xxx】` / `进入「xxx」` 这类强格式文本的精确兜底，`suggestion_source=strong_format_fallback`；普通关键词不再触发卡片。

为了更接近成熟 Agent/RAG 产品的行为，runtime 现在把“用户可见回复”和“调试审计来源”拆成了两层：skill 可通过 `response_policy` 覆盖引用可见性，但大多数场景不需要在每个 `SKILL.md` 里重复书写，因为框架默认就会对模型上下文匿名化检索来源，不把具体 `reference` 文件名和编号直接暴露给最终用户；但 `prompt_assembly.retrieved_sources`、会话事件和调试面板仍保留真实来源路径，方便前后端排查命中情况。只有少数确实要放开引用展示的 skill，才需要显式声明 `response_policy` 覆盖默认值。

其中多元路径已按学段拆分：

- 初中用户命中多元路径语义时，优先进入 `junior_multi_path_planning`
- 高中用户命中多元路径语义时，继续进入 `multi_path_planning`
- 如果当前还无法判断是初中还是高中，`main_planner` 会先追问孩子年级 / 学段，并保留本轮多元路径意图；用户补一句 `初二` / `高一` 后会自动续接到对应 skill
- `multi_path_planning` 保持为高中 / 新高考导向 bridge skill，不再混用初中多元路径

会话内的 `planner_state` 现按轮次整包覆盖，不再跨轮叠加旧的 `missing_facts` / `missing_fact_form`。因此当用户已经补充完“省份 / 分数 / 选科”等信息后，后续 assistant 消息不会再次重复展示上一轮的补充信息卡片。

对于“最近三次大考均分：目前未知”“省份：未知”这类显式未知输入，系统现会按“暂缺事实”处理：不会把该值当成有效筛选条件参与硬过滤，但会在 `facts_extractor.unknown_fact_keys` 中记录，并在多路径回复里明确提示该条件后续补充后才能进一步确认。

对于“高考省份：浙江；选科组合：物理；最近三次大考均分：目前未知”这类结构化补充信息，`main_planner` 与 `intent_tracker` 现默认按续答补 facts 处理，不会仅因出现“选科/选科组合”等字段名就误切换到 `subject_advisor` 等其他子 skill。

`runtime_skills/main_planner/references/04_新高考选科规则.md` 现已明确收口为“仅在已确认浙江时适用”的省份特定参考，且这条限制只针对高中选科讨论生效；小学和初中阶段默认不主动进入选科制度分析。对高中用户而言，省份未知时应先按 `docs/模拟升学/00_省份分类规则.md` 的口径确认省份类别，再决定后续选科追问方式。

多元路径规则层本轮还补了两条更严格的前置过滤：

- 当用户已选择 `career_orientation` 时，`convergence` 会按 `assets/generated/multiroute/path_catalog.json` 中规则文本里声明的职业兴趣做硬过滤；例如已选 `军警类` 时，不再把要求 `飞行员` 的 `三大招飞` 混入候选路径
- `budget_level` 现统一收口为 `>5万` / `<5万`，并兼容 `大于5万/年`、`5万元以下` 等表单或自然语言写法；对 `港澳升学`、`海外升学`、`中外合作办学` 这类带明确高预算门槛的路径，会在 `<5万` 时直接前置过滤
- `config/facts_schema.yml` 已同步补齐与 `config/fact_form_config.yml` 一致的表单元数据，包括 `input_type`、`placeholder`、`example`、`submit_mode` 和 `allowed_values`，避免表单层与 facts schema 层枚举口径继续漂移

## 架构总览

```text
用户消息
  -> FastAPI /api/v1/sessions/chat/stream（单一会话流入口）
  -> MainPlannerOrchestrator
  -> 恢复 / 构造 skill-runtime SessionState
  -> 同步 hailiang effective_facts 到 runtime global_facts
  -> Intent Router 根据 routing metadata / examples / optional embedding 判别意图
       -> general_chat 保持当前场景，生成 route_suggestions 按钮
       -> 用户点击后独立转场到 career_plan_entity 或其他专项 Skill
  -> 写回 facts / skill_states / candidate_paths / events
  -> SSE 或同步响应返回前端
```

核心入口：

- `src/hailiang_skills/api/main.py`
- `src/hailiang_skills/runtime_bridge/main_planner.py`
- `src/hailiang_skills/runtime_bridge/facts.py`

旧的 `RouterSkill`、`FactsExtractorSkill`、`PlannerSkill` 没有删除。它们现在主要服务于 hailiang 旧业务桥接链路，也就是当 `main_planner` 路由到 `mock_admission` 或 `multi_path_planning` 时继续参与执行。

## 目录结构

```text
src/hailiang_skills/
  api/                       FastAPI 接口
  core/                      会话上下文、facts、streaming、日志、场景状态
  llm/                       LLM client、配置、prompt registry
  runtime_bridge/            hailiang API 与 skill-runtime 的桥接层
  skill_runtime/             Hailiang 本地 skill metadata / 状态 / prompt 适配层
  skills/                    hailiang 原有业务 Skill
  storage/                   文件型 session/profile/facts 仓储

runtime_skills/
  main_planner/              升学规划顾问 / 历史目录名（skill_id=career_plan_entity）
  future_explore/            前景探路
  score_improve/             提分规划
  interest_explore/          兴趣探索
  subject_advisor/           选科参谋
  junior_multi_path_planning/  初中多元路径规划
  mock_admission/            桥接 hailiang AdmissionSkill
  multi_path_planning/       桥接 hailiang 高中 ConvergenceSkill
  <skill>/assets/            skill 本地轻量 assets / 索引 / 说明资源
  <skill>/references/        skill 本地知识文档
  <skill>/scripts/           status_track / profile_op 等脚本

assets/generated/
  admission/                 模拟升学结构化资产
  multiroute/                多元路径结构化资产
  school_intro/              院校介绍资产
  asset_registry.json        runtime asset lookup registry
  tool_registry.json         runtime tool registry

config/
  facts_schema.yml           facts 字段、scope、表单配置
  scenarios.yml              hailiang 旧业务场景/phase 元数据
  llm/qwen_dashscope.json    模型配置

docs/
  architecture/              架构文档
  模拟升学/                  模拟升学文档源
  多元升学路径/              多元路径 Excel 源
```

## Facts 生命周期

Hailiang 侧 facts 是三层模型：

- `shared_facts`：家庭共享信息，挂在 `user_id`
- `profile_facts`：孩子个体信息，挂在 `profile_id`
- `session_facts`：当前会话临时信息，挂在 `session_id`

运行时有效视图：

```text
effective_facts = shared_facts + profile_facts + session_facts
覆盖优先级：session > profile > shared
```

Skill Runtime 侧状态：

- `SessionState.global_facts`
- `SessionState.skill_facts`
- `SessionState.stage_facts`
- `SessionState.status_flags`
- `SessionState.route_history`

桥接规则：

- 进入 runtime 前：`context.known_facts -> state.global_facts`
- runtime 原生 Skill 执行后：`state.global_facts -> context.update_fact(...)`
- hailiang 旧业务执行后：`context.known_facts -> state.global_facts`

因此不同场景之间可以共享同一份上下文。例如用户先说“浙江物理 512 分”，再切到多元路径、提分规划或前景探路，这些 facts 会继续可用。

## 前端协议与调试字段

前端 SSE 事件保持统一：

- `run_started`
- `skill_status`
- `skill_lifecycle`
- `reasoning_delta`
- `final_text_delta`
- `message_block`
- `fact_changes`
- `final_message`
- `run_completed`

推理进度卡片现在按事件来源动态展示，而不是固定三段文案：

```text
intent / router       -> 意图判断或路由判断
planner               -> 推理规划
ms_agent_step_*       -> ms-agent plan 中的 steps[].action
script / resource     -> 调用脚本、加载资料等运行步骤
response              -> 正在生成回复
skill_lifecycle.finalized -> 当前 skill 真正进入总结完成生命周期
```

其中 `response=正在生成回复` 只是普通回复生成阶段，不等于当前 skill 已完成总结。只有后端产出 `skill_lifecycle` 且 `type=finalized`，或调试事件中出现 `skill_finalized`，才表示进入真正的 final summary 生命周期。`plan_summary_short` 来自 ms-agent planner 的同一次 JSON 输出，正常限制在 5-10 个中文字符；如果缺失或过长，后端会用规则压缩兜底。

当启用 `enable_thinking + return_reasoning` 时，thinking 会显示在消息内的“推理进度”卡片中；推理完成后前端会默认折叠，用户可手动展开查看完整内容。

前端调试时重点看两个字段：

- `main_planner_state.target_skill`：runtime 路由目标
- `active_skill`：当前实际执行业务 Skill
- `final_message.route_suggestions`：当前 assistant 消息下方要展示的可选规划主题按钮。来源通常是二次子 LLM 的 `llm_reply_analysis`，也可能是强格式兜底 `strong_format_fallback`

`route_suggestions_analyzed` / `route_suggestions_created` 调试事件用于判断后端是否真的产出了按钮数据：

- `route_suggestions_analyzed`：记录二次子 LLM 是否可用、是否失败、是否使用强格式 fallback、原始响应预览和建议数量
- `suggestion_source=llm_reply_analysis`：子 LLM 判断主回复清晰指向一个或多个子 Skill
- `suggestion_source=strong_format_fallback`：子 LLM 失败时，仅由 `进入【xxx】` / `进入「xxx」` 强格式精确兜底产生
- `is_final_summary=false` 且 `route_suggestions` 非空是合法状态，表示“场景选择卡片”，不是最终总结生命周期

`prompt_assembly` 调试事件现按分层记录：

- `layer=core`：基础 skill metadata、指令、状态、tool policy
- `layer=retrieval`：本轮命中的 references / local assets / generated assets
- `layer=final`：真正送给模型的最终 prompt

其中 `reference_strategy`、`retrieved_sources`、`retrieved_count`、`generated_asset_domains`、`local_asset_paths` 可帮助判断是否真的走了渐进式加载。

`core` prompt 中会注入 `# Runtime Clock`，包含 `Asia/Shanghai` 当前时间、日期、星期和 UTC 时间。模型回答“今天/明天/今年/当前招生季”等相对时间问题时，应以中国时区的该时间戳为准。

需要注意的是，`layer=retrieval` 和 `layer=final` 中真正送给模型的 prompt 现在默认是匿名化版本：模型只会看到 `Supporting Snippet 1/2/...` 这类抽象片段，不会看到具体 `references/xx.md` 文件名；真实来源继续只放在调试字段 `retrieved_sources` 中。

前端调试模式中的主要调试卡片现在也支持“一键复制当前卡片内容”，包括事件日志中的 Prompt 调试卡片、其他事件卡片，以及 Summary 面板中的 facts / 决策链路卡片，方便把当前上下文直接贴给后端排查或用于回归对比。

如果当前对话是由画像矩阵驱动推荐 scene，事件流里还会额外出现 `main_planner_profile_matrix`，用于展示本轮命中的画像维度、匹配行号和推荐 scene。

### Thinking / Reasoning

后端支持通过请求开关启用 Qwen 的 `enable_thinking` / `return_reasoning`，并在 SSE 下以 `reasoning_delta` 事件增量推送推理内容；前端会在“推理进度”卡片中以可折叠区域展示 thinking（完成后默认折叠）。

例子：

```text
模拟升学：
main_planner_state.target_skill = mock_admission
active_skill = admission

多元路径规划：
main_planner_state.target_skill = multi_path_planning
active_skill = convergence

前景探路：
main_planner_state.target_skill = future_explore
active_skill = future_explore
```

## 开场白

新建会话时，后端优先读取：

```text
runtime_skills/main_planner/SKILL.md
```

其中 `默认开场` 会作为第一条 assistant 消息写入 session。

只有 runtime 开场缺失时，才回退旧配置：

```text
config/session_opening.yml
```

已有历史 session 的 snapshot 不会被自动改写；新建 session 才会使用新的 runtime 开场。

## 数据资产

运行期唯一资产根目录：

```text
assets/generated/
```

模拟升学资产：

- `assets/generated/admission/province_flow_map.json`
- `assets/generated/admission/province_score_bands.json`
- `assets/generated/admission/tier_copywriting.json`
- `assets/generated/admission/asset_manifest.json`

多元路径资产：

- `assets/generated/multiroute/path_catalog.json`
- `assets/generated/multiroute/path_reason_templates.json`
- `assets/generated/multiroute/score_band_exposure_rules.json`
- `assets/generated/multiroute/action_timeline_templates.json`
- `assets/generated/multiroute/province_score_lines.json`
- `assets/generated/multiroute/question_bank.json`
- `assets/generated/multiroute/asset_manifest.json`

院校介绍资产：

- `assets/generated/school_intro/schools.json`
- `assets/generated/school_intro/asset_manifest.json`

Runtime registry：

- `assets/generated/asset_registry.json`
- `assets/generated/tool_registry.json`

不要再创建第二套 `data/generated`。当前 `src/hailiang_skills/skill_runtime/data_loader.py` 已统一读取 `<project_root>/assets/generated`。

同时，runtime skill 目录下现支持本地 `assets/`：

- `runtime_skills/<skill>/assets/`

这类本地 assets 适合放：

- 轻量说明文件
- 索引或样例
- 仅服务当前 skill 的 prompt/retrieval 辅助资料

推荐做法是：

- 结构化大资产仍放 `assets/generated/<domain>/`
- skill 私有的轻量资源放 `runtime_skills/<skill>/assets/`
- 在 `SKILL.md` 的 `assets.generated_domains` 中显式声明当前 skill 依赖哪些全局资产域

## Skill 标准

当前项目对 runtime skill 采用“Anthropic progressive disclosure + OpenClaw / AgentSkills 目录规范”的融合方案：

- `SKILL.md`
  - 定义 skill 元数据、提示词装载策略、retrieval 策略、assets 声明、debug 声明；入口类 skill 还可声明 `planner.scene_selection`
- `runtime_contract.json`
  - 定义运行时状态机、facts schema、routes、accepts_scenes
- `references/`
  - 存放知识文档，默认不再整包注入 prompt，而是按需检索片段
- `assets/`
  - 存放 skill 本地轻量资源
- `assets/generated/`
  - 存放共享结构化大资产

`main_planner` 与原生 child skill 现默认走渐进式 prompt 装载：

- `core`
  - 轻量 metadata、skill 指令、状态、tool policy
- `retrieval`
  - reference catalog、local asset catalog、命中的 snippets
- `final`
  - 真正发给模型的最终组合 prompt

需要区分两层边界：`src/hailiang_skills/skill_runtime/` 负责 Hailiang 侧的 metadata 解析、facts/state 同步、Prompt assembly、前端调试字段和旧业务 bridge；native Skill 每轮的标准 Skill 加载、plan、lazy reference/script/resource 装载与脚本沙箱执行，则通过共享 `agent_skill_runtime_core` 委托给 `ms-agent`。如果 `ms-agent` 或共享 core 不可用，正式对话链路会返回 runtime unavailable，不会静默退回自研 runtime。

入口类 skill 如果声明了 `planner.scene_selection.mode=profile_matrix`，则会额外按以下顺序做 scene recommendation：

- `profile_matrix`
  - 从 `planner.scene_selection.matrix_reference` 加载画像矩阵
- `registry_match`
  - 用子 skill 的 `accepts_scenes / triggers` 归一化可推荐 scene
- `keyword_fallback`
  - 只有前两层都无法命中时，才回退旧的关键词兜底逻辑

### SKILL.md YAML frontmatter 字段含义

`SKILL.md` 第一行如果是 `---`，系统会把两个 `---` 之间的 YAML 解析成 runtime metadata。当前链路里有两类使用方：

- `ms-agent` / 调试平台：按标准 Skill 协议识别 `SKILL.md`，并基于 `name`、`description`、`references/`、`scripts/`、`assets/` 做加载、分析规划和按需资源装载
- Hailiang 主 Agent：通过 `skill_runtime.skill_loader` 读取 frontmatter，再和 `runtime_contract.json` 合并，用于路由、Prompt 组装、检索、资产、工具策略、调试事件和场景推荐

标准 Skill 最少应包含：

```yaml
---
name: 示例 Skill
description: 这个 Skill 解决什么问题、适合什么用户请求。
---
```

Hailiang 中建议所有可运行 Skill 都补齐：

```yaml
---
name: 示例 Skill
skill_id: example_skill
description: 这个 Skill 解决什么问题、适合什么用户请求。
skill_type: native
entrypoint_role: child
accepts_scenes: [示例场景]
triggers: [示例场景, 示例问题]
routing:
  scene_name: 示例场景
  routing_examples:
    - 我想进入示例场景
---
```

字段说明：

| 字段 | 是否必填 | 系统什么时候用 | 默认值 / 备注 |
| --- | --- | --- | --- |
| `name` | 标准必填 | Skill 展示名、Prompt metadata、CLI / 调试展示；Intent Router 无 scene 时也会作为候选名兜底 | Hailiang 可空但不建议；ms-agent 标准 Skill 应填写 |
| `description` | 标准必填 | Skill 能力说明，供调试平台、ms-agent 和人工审核理解 Skill 边界 | Hailiang 当前不会用它做硬路由，但标准 Skill 应填写 |
| `skill_id` | Hailiang 强烈建议 | runtime registry 的身份补充；无 `runtime_contract.json` 时会用它生成默认 contract | 最好与目录名和 `runtime_contract.json.skill_id` 一致 |
| `version` / `author` / `tags` | 可选 | 展示、审计、调试和人工检索 | 不影响运行决策 |
| `skill_type` | Hailiang 建议 | 决定执行边界；`native` 走 ms-agent / skill-runtime 原生链路，`bridge` 走旧业务桥接 | 默认 `native` |
| `entrypoint_role` / `skill_role` | Hailiang 建议 | 区分入口和子场景；`main_planner` 用 `entry`，普通场景用 `child` | 默认 `child`；`runtime_contract.json.skill_role` 优先级更高 |
| `accepts_scenes` | 子场景建议 | main_planner 推荐 scene 归一化、Intent Router 别名匹配、route 校验提示 | 也可放在 `runtime_contract.json`；frontmatter 优先用于 metadata |
| `triggers` | 子场景建议 | Intent Router 的关键词 / 别名匹配；帮助用户直达 child skill | 默认空 |
| `routing.scene_name` | 子场景建议 | Intent Router 构造候选 route example 时使用的场景名 | 为空时回退 contract metadata、`accepts_scenes[0]` 或 `name` |
| `routing.routing_examples` | 子场景建议 | Intent Router 文本匹配和可选 embedding 召回的主要样例 | 默认空；新增 child skill 时最关键 |
| `routing.slot_facts` | 可选 | 描述进入该 skill 后通常要补哪些 facts；当前 router 不用它拦截进入 | 目标 skill 自己根据 `runtime_contract.json` 追问 |
| `routing.school_stage_scope` | 可选 | 表达适用学段，供路由和人工审核参考 | 如 `primary_junior`、`junior`、`senior`、`junior_senior`、`all` |
| `prompt_loading` | 可选 | 组装 core / retrieval / final prompt 时控制 SKILL、SessionState、工具、route targets、references、assets 是否进入上下文 | 有默认值；原生 Skill 推荐 `strategy: progressive` |
| `retrieval` | 可选 | 控制 reference context、catalog、snippet 长度和是否启用 Hailiang 补充召回 | 默认关闭；标准 `native + progressive` Skill 中，`enabled: true` 只承接 ms-agent 已加载的 references；只有显式 `supplemental_enabled: true` 才会运行 Hailiang retrieval 补充召回 |
| `assets` | 可选 | 声明本 skill 是否读取本地 `assets/`，以及可使用哪些共享 `assets/generated/<domain>` | `local_enabled: true` 时目录必须存在 |
| `tool_policy` | 可选 | 控制本轮是否允许先调工具、是否允许直接回答、最大工具轮数 | 默认允许直接回答，`max_tool_calls` 默认 0 |
| `debug` | 可选 | 控制 Prompt assembly、retrieval detail 等调试记录 | 默认记录 |
| `response_policy` | 可选 | 控制最终回复是否隐藏 reference 文件名 / 编号、是否做输出清洗 | 默认隐藏具体文件名和 reference id |
| `bridge` | bridge Skill 必填 | `skill_type: bridge` 时指向旧业务 Skill，例如 `admission` / `convergence` | 原生 `native` Skill 不需要 |
| `planner.intent_router` | 仅 `main_planner` 需要 | 控制不明确意图、长画像输入、embedding 阈值、LLM fallback 等入口路由策略 | child skill 不需要配置 |
| `planner.scene_selection` | 仅 `main_planner` 可选 | 控制画像矩阵推荐：从 reference 中读取推荐表，再落到 child skill scene | 不启用时走 route / keyword 兜底 |
| `requires`、`user_match_tags`、`trigger_rule`、`tool_dependency`、`target_region`、`bind_script`、`ref_path` | 可选扩展 | 当前主要用于调试平台、人工说明或向后兼容旧 Skill 描述 | 不作为 Hailiang 主链路的核心运行字段 |

几个优先级规则：

- `runtime_contract.json` 仍是 Hailiang 运行契约来源：`skill_id`、`skill_role`、`stages`、`facts`、`routes`、`accepts_scenes` 以 contract 为主
- 如果没有 `runtime_contract.json`，Hailiang loader 会用 `skill_id` / 目录名生成一个最小默认 contract；但主 Agent registry 当前只加载同时存在 `SKILL.md` 和 `runtime_contract.json` 的目录，所以正式接入仍应提供 contract
- `tools.yaml` 和 `runtime_contract.json` 是平台扩展：可以保留，但不能替代标准 `SKILL.md`、`references/`、`scripts/`、`assets/` 目录协议
- 新导入 Skill 的 canonical 目录名建议等于 `skill_id`，避免调试平台、ms-agent 和 Hailiang 主 Agent 对同一个 Skill 身份理解不一致

### 从调试平台导入通用 Skill

调试平台导出的包应保持标准目录结构：

```text
<skill_id>/
  SKILL.md
  runtime_contract.json
  tools.yaml              # 可选，调试平台 / ms-agent 扩展
  references/             # 可选
  scripts/                # 可选
  assets/                 # 可选
```

导入为普通 child skill：

1. 从调试平台导出 zip / skill 包，解压后确认根目录内只有一个 `SKILL.md`
2. 将目录放到 `runtime_skills/<skill_id>/`，并确保目录名、`SKILL.md.skill_id`、`runtime_contract.json.skill_id` 三者一致
3. `SKILL.md` 设置 `entrypoint_role: child`，通常设置 `skill_type: native`
4. 补齐或确认 `routing.scene_name`、`routing.routing_examples`、`accepts_scenes`、`triggers`，这样用户一开始直接表达明确场景时能被 Intent Router 直达
5. 在 `runtime_skills/main_planner/runtime_contract.json` 的 `routes` 中增加场景到 child skill 的映射：

```json
{
  "scene": "示例场景",
  "target_skill_id": "example_skill",
  "required_global_facts": [],
  "required_skill_facts": []
}
```

6. 如果希望用户先进入 `main_planner` 问诊后，被顾问推荐到这个 child skill，还需要更新 `runtime_skills/main_planner/references/06_用户画像&规划策略&可探索场景.md` 或 `main_planner` 的 `planner.scene_selection` 配置，让画像矩阵能产出对应 scene
7. 如新 Skill 引入新的 facts，更新 `config/facts_schema.yml`；如引入共享结构化资产，更新 `assets/generated/asset_registry.json` 和相关 domain 文件
8. 重启后端，让 runtime registry 重新加载 Skill 目录

导入为 `main_planner` 修改：

1. 不要新增第二个入口目录；只修改 `runtime_skills/main_planner/`
2. 保持 `SKILL.md` 中 `skill_id: main_planner`、`entrypoint_role: entry`、`skill_type: native`
3. 可以从调试平台导出的 `main_planner` 包中合并 `SKILL.md` 正文、`references/`、`scripts/`、`assets/`，但要保留 Hailiang 当前的 `planner.intent_router`、`planner.scene_selection`、`prompt_loading`、`retrieval`、`assets.generated_domains` 等运行配置，除非本次明确要改这些策略
4. 修改 `runtime_skills/main_planner/runtime_contract.json` 时必须保留已有 `routes`，新增 child skill 只追加 route，不要覆盖模拟升学、多元路径、选科、兴趣探索等已有路由
5. 如果调整了顾问推荐逻辑，要同步检查画像矩阵 reference 中的 scene 名称是否能被 child skill 的 `accepts_scenes` / `triggers` / main planner routes 归一化
6. 重启后端并测试两条入口：用户直接进 child skill，以及用户先进入 `main_planner` 问诊后再跳入 child skill

本地快速校验：

```bash
cd /Users/ayi/Project/hailiang-skill_v0201/hailiang-skills
PYTHONPATH=src .venv/bin/python - <<'PY'
from hailiang_skills.skill_runtime.skill_registry import load_local_skill_registry

registry = load_local_skill_registry("runtime_skills")
print(sorted(registry.bundles))
for skill_id, bundle in sorted(registry.bundles.items()):
    print(skill_id, bundle.contract.skill_role, bundle.runtime_metadata.skill_type)
PY
```

看到新增 `skill_id` 后，再启动后端做对话验证。

## 本地运行

### 后端

```bash
cd /Users/ayi/Project/hailiang-skill_v0201/hailiang-skills
export DASHSCOPE_API_KEY=your_api_key
export AGENT_SKILL_RUNTIME_CORE_PATH=/Users/ayi/Project/hailiang-skill_v0201/agent_skill_runtime_core
PYTHONPATH="src:$AGENT_SKILL_RUNTIME_CORE_PATH" .venv/bin/python -m uvicorn hailiang_skills.api.main:app \
  --host 127.0.0.1 \
  --port 8010 \
  --reload
```

健康检查：

```bash
curl http://127.0.0.1:8010/health | python -m json.tool
```

期望看到：

```json
{
  "runtime": {
  "entry_skill": "general_chat",
    "skills": [
      "future_explore",
      "interest_explore",
      "junior_multi_path_planning",
      "career_plan_entity",
      "mock_admission",
      "multi_path_planning",
      "score_improve",
      "subject_advisor"
    ]
  }
}
```

`skills` 会随 `runtime_skills/` 目录下已导入的标准 Skill 增减；当前核心升学链路至少应包含 `career_plan_entity`、`future_explore`、`interest_explore`、`score_improve`、`subject_advisor`、`junior_multi_path_planning`、`mock_admission`、`multi_path_planning`。

### 前端

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 4173
```

如果要让前端指向指定服务器后端，可写：

```js
// frontend/public/runtime-config.js
window.__HAILIANG_RUNTIME_CONFIG__ = {
  apiBaseUrl: "http://YOUR_SERVER_IP:8010",
  backendPort: 8010,
  userId: "debug-user"
};
```

## 一键部署

```bash
export DASHSCOPE_API_KEY=your_api_key
./deploy-all.sh
```

如果服务器环境出现 `CERTIFICATE_VERIFY_FAILED`（常见于 Python 使用了非系统 OpenSSL，默认 CA 路径为空或不完整），可在部署前显式指定 CA 文件，例如：

```bash
export SSL_CERT_FILE=/etc/pki/tls/cert.pem
./deploy-all.sh
```

如果服务器允许 Docker，`deploy-all.sh` 会自动拉起本地 `postgres`、`redis`
和 `otel-collector` 容器。此时 `env.local.sh` 中建议继续保持：

```bash
export HAILIANG_DATABASE_URL="postgresql+psycopg://hailiang:hailiang@127.0.0.1:5432/hailiang_skills"
export HAILIANG_REDIS_URL="redis://127.0.0.1:6379/0"
export START_INFRA="1"
```

脚本会在宿主机默认端口被占用时自动选择备用端口，并把上述本地连接串改写为
当前实际端口。只有当你使用外部托管 PostgreSQL/Redis 时，才需要把地址改成
真实远程地址，并设置 `START_INFRA=0`。

`deploy-all.sh` 会先用 `/health/ready` 检查 PostgreSQL、Redis 与审计加密是否就绪，
再额外请求 `/health` 校验 `runtime.entry_skill` 和必需的 runtime skills 是否已加载。

部署脚本会：

- 重建 `.venv`
- 安装项目依赖
- 检查 runtime 必需文件
- 在 `postgres` 模式下按需自动启动本地 Docker PostgreSQL/Redis/OTel
- 跑 `tests.test_runtime_bridge`
- 启动后端并检查 `/health.runtime`
- 构建并启动前端

## 验证命令

后端单测：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest tests.test_runtime_bridge -v
```

前端构建：

```bash
cd frontend
npm run build
```

真实接口 smoke：

```bash
SESSION_ID=$(curl -s -X POST http://127.0.0.1:8010/api/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"runtime-smoke"}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')

curl -s -X POST "http://127.0.0.1:8010/api/v1/sessions/$SESSION_ID/messages" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"runtime-smoke","content":"我想做模拟升学，浙江物理512分"}' \
  | python -c 'import json,sys; p=json.load(sys.stdin); print(p["active_skill"]); print(p["main_planner_state"]["target_skill"])'
```

期望输出：

```text
admission
mock_admission
```

## 修改资产

### 修改模拟升学文档

源文件：

```text
docs/模拟升学/*.md
```

重新编译：

```bash
python3 scripts/build_admission_assets.py
```

### 修改多元路径 Excel

源文件：

```text
docs/多元升学路径/最新人路规则（13+内蒙古）更至20260520.xlsx
```

重新编译：

```bash
python3 scripts/build_multiroute_assets.py
```

重新编译后建议重启后端，因为 runtime registry 和业务 Skill 会在进程启动/运行时读取资产。

## 新增场景

新增场景有两种推荐路径。

### Runtime 原生场景

适合：

- 主要靠 `SKILL.md`、references、`status_track.py`、tool 和 runtime 状态推进
- 不依赖 hailiang 旧业务规则代码

最小目录：

```text
runtime_skills/your_scene/
  SKILL.md
  runtime_contract.json
  references/             # 可选
  scripts/status_track.py # 可选
```

还需要：

- 在 `runtime_skills/main_planner/runtime_contract.json` 增加 route
- 在入口 skill 的 `SKILL.md` 中补充 `planner.scene_selection` 或更新对应 reference / triggers / accepts_scenes
- 如涉及新 facts，同步更新 `config/facts_schema.yml`
- 如涉及新资产，同步更新 `assets/generated/asset_registry.json`

### Hailiang 规则型场景

适合：

- 需要大量规则、排序、结构化资产筛选
- 需要输出 `candidate_paths`、`message_blocks`、引用、facts 表单等 hailiang 前端协议

接入步骤：

1. 在 `src/hailiang_skills/skills/` 新增 `BaseSkill`
2. 在 `src/hailiang_skills/api/main.py` 注册该 Skill
3. 在 `runtime_skills/` 新增 bridge skill 目录
4. 在 `MainPlannerOrchestrator.HAILIANG_TARGETS` 增加映射
5. 在 main planner runtime contract 增加 route
6. 如需要，更新 `config/scenarios.yml`

详细模板见：

- [Skill Runtime 融合架构与新增场景接入手册](docs/architecture/skill_runtime_integration.md)

## 推送代码

当前脚本默认推送到：

```text
https://github.com/ZhangYiPop/merge_hailiang_skill_runtime.git
```

运行：

```bash
./git-push.sh
```

脚本会检查 runtime 必需文件是否齐全，并提交：

- `src/hailiang_skills/skill_runtime/`
- `src/hailiang_skills/runtime_bridge/`
- `runtime_skills/`
- `assets/generated/`
- `tests/test_runtime_bridge.py`

## 当前局限

- runtime 原生 Skill 当前是 bridge 层切块流式，不是模型 token 原生 streaming
- `InMemorySessionRepository` 仍是文件快照 + 内存缓存，线上长周期运行建议接数据库
- facts schema 仍有部分自由字段，后续可以继续收敛为更强的 typed contract
- 当前 `SCENE_HINTS` 仍保留为兜底逻辑，但主推荐链路已优先走 `planner.scene_selection + accepts_scenes / triggers`；后续还可以继续把 route 决策层完全收敛到更强的 contract / planner metadata

## 一句话总结

这个工程现在的核心机制是：

> 用 `general_chat` 统一承接新会话入口，用户确认后进入 `career_plan_entity` 或其他专项 Skill；runtime 原生场景和 hailiang 旧业务桥接场景共享同一套 facts、资产、流式协议和前端调试面板。
