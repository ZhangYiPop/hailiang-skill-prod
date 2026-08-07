# CHANGELOG - 更新日志

本文档记录项目的最新功能升级与历史更新记录。

---

## 最新功能升级 (v2026.06)

### 2026-07-31

#### 新增云端 SSE 推流排查脚本

- 新增 `scripts/show_sse_trace.py`，支持按 `session_id`、`run_id`、`source_message_id`、`target_skill_id` 联合搜索真实 SSE 录制和事件日志
- 脚本默认同时检查三类文件：单轮 `run` 的 SSE 录制、同 `session` 的聚合 SSE 流，以及 `logs/sessions/{session_id}/events.jsonl` 事件日志
- 输出优先展示结构化摘要，包括 `wire_event`、`internal_event`、`seq`、`message_id`、`status`、`skill_transition`、`assistant` 文本片段，必要时可通过 `--raw` 回看原始 JSON 行
- 增加 `--show-head`、`--all-lines`、`--keyword`、`--max-matches` 等参数，方便云端无 `rg` 环境下做长期复用的接口排查
- 将 `show_sse_trace.py` 调整为兼容老版本 Python 的语法写法，避免云端执行时报 `future feature annotations is not defined`
- 更新 `README.md`，补充 `show_sse_trace.py` 的用法、目标日志文件位置以及和手写 `grep` 相比更适合云端联调的场景说明

### 2026-07-30

#### MS-Agent 规划与原生 Skill 正文生成合并

- 原生 Skill 的普通无工具场景现在由同一次模型调用同时生成 MS-Agent 执行计划和用户可见正文，减少规划完成后再次请求正文模型造成的首字延迟
- 合并请求改为读取上游模型流，并在 `assistant_message` 开始生成后通过既有 `final_text_delta` 实时透传；不再等待完整 JSON 返回后才模拟分块输出
- 工具/联网路由判断合并进同一次 MS-Agent JSON，正常轮次不再调用独立分类模型；合并结果缺失、截断或非法时才调用旧分类器兜底，也可通过 `HAILIANG_TOOL_ROUTING_MODE=standalone` 人工回切
- 合并路由覆盖 Web、RAG、选科资产、状态工具、MCP 与脚本候选；模型只提出意图，服务端继续按 capability、资产策略、MCP 白名单与参数范围取授权交集，客户端和模型都不能扩大权限
- 工具候选获服务端授权且即将执行时，才通过既有 `intent.steps` 展示清洗后的工具步骤；被拒绝的候选不会展示，执行失败则更新同一个步骤并进入原有安全兜底
- 无工具轮次可在 MS-Agent 生成 `assistant_message` 时直接流式输出；需要工具、脚本或动态问卷完整校验的场景继续缓冲正文，避免展示尚未经过工具结果或表单规则确认的内容
- 合并请求会携带当前 Skill 内容、会话历史、会话 Facts 与本地 `references/`；动态问卷继续复用既有 `assistant_message / state_patch / question_ids` 内部协议和 `fact_form` 输出
- 需要 MCP、本地工具、脚本执行或工具结果回填时继续走原有多轮链路；正文完成后的 `suggestion_route` 监控模型、按钮校验和 Skill 跳转逻辑保持不变
- `agent_skill_runtime_core.LoadedSkillContext` 新增可选 `combined_response`，使 Hailiang 服务端与 Skill 调试台共享同一份核心 runtime 返回结构
- Hailiang bridge 同时兼容尚未升级的旧版共享 core：缺少 `combined_response` 字段时改从本轮 planner 实例读取，仍不可用则安全回退原正文生成链路，避免部署版本不一致引发 `AttributeError`
- `hailiang.sse.v2`、`final_text_delta`、`form`、`route_suggestions`、`skill_transition` 及前端接口均未变更
- 正文完成后的 `suggestion_route` 监控模型仍保持独立调用，生成规则、按钮校验与点击跳转逻辑均未参与本次合并
- 修复同一 `intent.steps[].id` 在 active 与 completed 阶段被 MS-Agent 后续 action 改写 label 的问题；每个 stage 现在固定使用本轮首次公开 label，后续事件只更新 detail、summary、source 和状态，实时 SSE 与历史 `status_timeline` 保持一致
- 新增可配置的模拟推理进度：模型运行期间按间隔补充“分析用户需求”“核对关键信息”“整理相关资料”“形成规划方向”等阶段，并在正文前固定补充“正在总结信息”；通过 `progress_simulation` 或对应环境变量控制启用、间隔和最快展示时长，不改变 `hailiang.sse.v2`
- 模拟阶段等待改为基准间隔叠加随机抖动；真实模型状态或正文到达时立即打断等待，避免状态像固定定时器，也不会为了模拟进度额外延迟慢模型
- “正在总结信息”已接入正文首个 `final_text_delta` 的前置闸门，合并模式下即使正文流式提前返回，也保证该 label 先于正文首字到达
- 为并发的模拟进度线程与正文线程增加单轮 SSE 编码锁，保证 state 的 `seq`、快照和 assistant 累计内容按队列顺序发送，后续内容始终以前一条为前缀再追加新文本
- 修复正文已开始流式输出但 `intent.status` 仍为 `streaming` 的时序问题；现在首个正文 delta 前先发送 `progress_completed`，保证正文出现时 intent 已完成
- 将路由候选置信度、无明确路由上下文时的置信度、通用聊天卡片阈值和专业 Skill 切换阈值统一收口到 `config/intent_router.yml`；流式与普通 API 不再被 LLM provider JSON 中的旧阈值覆盖，仍保留旧字段作为兼容回退
- 增强 MS-Agent 思考步骤约束：`steps[].action` 作为 `intent.label` 时必须使用“正在 + 动作”的表达，服务端发送前统一补齐前缀并限制为不超过 12 个字符，避免模型输出过长或格式不一致

### 2026-07-29

#### 前后端联调文档补充：Skill 进出、消息标识与历史恢复

- 更新 `guides/API_DOCUMENTATION.md`，补充 `run_id / message_id / source_message_id / source_interaction_id` 的语义边界与典型用途，明确它们不能混用
- 在 `API_DOCUMENTATION.md` 中新增 toolbar 进入 Skill、推荐卡片进入 Skill、退出 Skill 三种动作的请求差异说明，并补充 `route suggestion is no longer current` 等常见联调错误的触发条件
- 在 `API_DOCUMENTATION.md` 中补充 Native Questionnaire 表单的“双阶段提交”说明：`interactions` PATCH 负责表单状态同步，后续 `chat/stream` 负责把答案继续送进当前 Skill
- 更新 `guides/SSE_RESPONSE_CONTRACT.md`，补充标识符语义、退出 Skill 的历史持久化行为、推荐卡片点击约束，以及历史恢复时 `GET /sessions/{session_id}` 和 `GET /sessions/{session_id}/context` 的职责划分
- 更新 `guides/SSE_FRONTEND_MOCK_EXAMPLES.md`，新增推荐卡片点击请求、表单提交后续发 chat 请求、退出 Skill 请求以及历史恢复预期的 mock 说明
- 更新 `README.md`，补充接口联调相关文档入口，方便前端和 BFF 在联调时快速定位字段语义与动作时序说明

### 2026-07-21

#### Linux `deploy-all.sh` 自动拉起本地 Docker 基础设施

- `deploy-all.sh` 在 `HAILIANG_STORAGE_BACKEND=postgres` 且 `START_INFRA=1` 时，新增自动拉起本地 Docker `postgres`、`redis`、`otel-collector` 的能力
- 复用 `docker-compose.yml` 里的现有服务定义，并复用已运行容器的端口映射；若宿主机默认端口被占用，会自动选择备用端口
- 当 `HAILIANG_DATABASE_URL` / `HAILIANG_REDIS_URL` 使用 `127.0.0.1` 或 `localhost` 时，脚本会按实际映射端口自动改写连接串后再执行 PostgreSQL/Redis 探活与 Alembic
- 远程托管 PostgreSQL/Redis 仍然支持；如需跳过本地 Docker 基础设施，可设置 `START_INFRA=0`
- 更新 `env.example.sh`、`README.md` 和 `guides/RUN_AND_DEPLOY.md`，补充 Linux/CentOS 部署时的推荐配置与使用说明
- 修复 `deploy-all.sh` 的 runtime 健康检查误用 `/health/ready` 响应的问题；现在脚本会先用 `/health/ready` 校验 readiness，再单独请求 `/health` 校验 `entry_skill` 与 runtime skills，避免误报 missing skills

#### 真实 SSE 本地落盘

- 新增真实 SSE 明文落盘能力，支持把后端实际发给前端的流式线协议保存到本地，便于前端同学联调和回放
- `config/runtime.yml` 新增 `sse_recording` 配置，采用开关控制，默认关闭，可按需在本地或指定环境开启
- 普通聊天流 `POST /sessions/{session_id}/messages/stream` 与 Skill 转场流 `POST /sessions/{session_id}/skill-transitions/stream` 现在都会记录真实出流
- 落盘目录按 `session_id` 组织，同时保留两层文件：
  - `logs/sessions/{session_id}/sse/{run_id}.jsonl`：单轮真实流
  - `logs/sessions/{session_id}/sse/session_stream.jsonl`：串起该会话全部 `run_id` 的总流
- 每条记录同时保留 `raw_sse`、`wire_event`、解析后的 `payload`、`source_endpoint` 等信息，方便直接回放、grep 和转成前端 mock
- 更新 `README.md`，补充真实 SSE 本地落盘的开启方式和目录说明

#### 对话 SSE 完整数据流文档

- 新增文档 `docs/architecture/conversation_sse_dataflow.md`，系统梳理当前项目中“用户发消息 -> 后端编排 -> SSE 推流 -> 前端渲染 -> 表单/转场回流”的完整数据流
- 文档覆盖普通问答、正文增量、推理进度、`fact_form`、`path_actions`、`route_suggestions`、Skill 转场、取消、风控拦截、失败和旧流被新消息覆盖等主要分支
- 明确 `POST /messages/stream` 是当前主对话入口，模型回复通过同一条 SSE 大流返回；`POST /skill-transitions/stream` 负责进入/退出/切换 Skill；`POST /runs/{run_id}/cancel` 负责取消当前运行
- 补充 unified-v1 SSE 协议说明，明确 `system / message / card / done` 到前端业务事件的映射关系，以及前端如何还原为 `final_text_delta / message_block / final_message / run_completed`
- 更新 `README.md`，新增该数据流文档入口，并声明对话链路和交互协议的权威说明位置

#### SSE 前端联调 Mock 示例

- 新增文档 `guides/SSE_FRONTEND_MOCK_EXAMPLES.md`，提供贴近当前 `unified-v1` 协议的前端联调示例流
- 示例覆盖普通正文流、正文加 `fact_form`、正文加 `path_actions`、正文加 `route_suggestions`、Skill 转场、取消、风控拦截和运行失败等主要场景
- 文档按前端适配视角整理了 `wire event`、`card_type`、`payload.code`、推荐归一化结构和最小状态机，便于直接实现 parser、UI mock 和状态收敛
- 更新 `README.md`，补充前端联调示例文档入口，便于前端同学直接查阅

### 2026-07-07

#### Embedding 独立网关配置

- `LLMConfig.embedding` 新增 `base_url` 配置，允许聊天模型和 embedding 模型分开走不同的 compatible 网关
- `main_planner` 构造 `EmbeddingClient` 时，优先使用 `embedding.base_url`，未配置时继续回退到顶层 `base_url`，保持旧配置兼容
- `config/llm/qwen_dashscope.json` 已补充 `embedding.base_url=https://dashscope.aliyuncs.com/compatible-mode/v1`，用于将 embedding 请求单独切到 DashScope 官方 compatible 接口
- 更新 `README.md` 中的 embedding 配置说明，明确 `/chat/completions` 与 `/embeddings` 支持分开走不同前缀
- 该改动仅调整 embedding 请求地址选择逻辑，不修改路由阈值、skill 命中规则和主顾问问诊锁行为

#### Embedding 分批与本地样本缓存

- `EmbeddingClient` 新增 `max_batch_size`，对 route examples 的远程 embedding 请求自动分批，解决 DashScope 单次最多 10 条样本的限制
- 新增 `skill_runtime/embedding_cache.py`，将路由样本向量持久化到 `Path.cwd()/.skill_runtime_cache/intent_router_embeddings/`
- `IntentRouter` 启动时优先命中本地样本缓存，只对新增或变更样本重新请求 embedding，并在写回时清理已删除样本的旧缓存
- 本地缓存 hash 口径包含 `skill_id + source + text + model + base_url`，支持模型或 endpoint 切换后的自动失效
- 调试信息新增缓存命中、miss、存量清理数量和 embedding 分批次数，便于直接通过 `snapshot.json` 排查初始化状态
- 第一版只缓存 route examples，不缓存用户 query；query 仍保持实时 embedding，失败时继续降级到文本匹配

#### Intent Router 前置短路原因日志

- `IntentRouter` 的前置短路分支新增 `debug_payload.routing_short_circuit`，统一记录 `stage / rule / reason / details`
- 长画像优先进 `main_planner` 时，会在 `snapshot.json` 里落出字符数、画像关键词命中数、命中词列表和分句数量，方便直接核对为什么没走 embedding 候选比较
- `main_planner_route` 事件现在不仅记录跨 skill 跳转，也会记录 `main_planner / stay / clarify / recommend_switch / resume` 等未切换决策，保证 `events.jsonl` 能完整复现路由判断链路

### 2026-06-11

#### Intent Router 直接路由与场景锁

- 新增可配置前置 `IntentRouter`：用户意图明确时直接进入 child skill；用户意图不明确、只给零散画像信息或表达“想规划但不知道从哪里开始”时进入 `main_planner`
- `main_planner` 不再作为所有明确问题的必经前置；`高一怎么选科`、`浙江物理512分能上什么学校`、`成绩上不去怎么提分`、`初中有美术特长有什么路径` 等表达可直接命中对应 skill
- `main_planner` 增加 `consultative` 顾问锁：进入顾问问诊后，用户继续补充年级、成绩、特长等画像事实时不会被关键词自动切走，直到用户明确切换或确认推荐场景
- 新增长段画像输入保护：当用户一次性输入较长、多维度孩子情况时，优先进入 `main_planner` 做画像简历和子场景推荐，避免被段落中的局部关键词提前拉进 child skill
- child skill 增加 `task` 任务锁：进入具体场景后，结构化补 facts、普通追问和旁支表达默认留在当前 skill；只有明确“换到/先看/继续刚才/回到”等语义才允许切换或恢复
- 各 runtime skill 的 `SKILL.md` frontmatter 新增 `routing` 配置，包含 `scene_name`、`routing_examples`、`slot_facts` 和可选 `school_stage_scope`；其中 `slot_facts` 只给目标 skill 追问槽位使用，router 不会用它拦截进入
- `mock_admission` 补充“浙江物理类580分可以上哪些学校”等 `routing_examples`，这类分数 + 科类 + 学校诉求由配置语料命中模拟升学，而不是在 router 代码里写死话术
- 接入可选 DashScope compatible embeddings：由 `config/llm/qwen_dashscope.json` 的 `embedding.enabled` 控制开关；开启且 `DASHSCOPE_API_KEY` 可用时，router 可用 `text-embedding-v4` 对 `routing_examples` 做语义召回；无 key、调用失败或低置信时自动降级到配置文本匹配与保守兜底
- 保留原有多元路径学段分流和中断恢复：多元路径学段未知时仍会先追问年级，用户补 `初二` / `高一` 后继续进入对应 skill；“继续刚才”仍可恢复被打断的 child skill
- 更新 `README.md`，说明新路由架构、routing metadata 配置示例、场景锁语义和 embedding 降级策略
- 新增/更新回归测试，覆盖明确意图直达、不明确意图进入 `main_planner`、场景锁、跨场景切换、恢复上一任务和 embedding 降级

#### 选科参谋业务规则接入

- `subject_advisor` 从占位版升级为正式高中选科规划顾问规则版，整合阶段识别、需求类型、冲突标签、最小信息收集、诊断推理、方案生成、结果校验和边界处理流程
- `subject_advisor` 的适用范围扩展为“初高中衔接选科参谋”：初中生从画像推荐跳转过来时，默认做高中选科前置认知、学科优势观察和准备建议，不直接给最终选科组合
- 强化 `subject_advisor` 的对话节奏门禁：前 3 轮只做阶段性判断和关键问题验证，默认第 4-5 轮才输出最终推荐；基础信息齐全时也会继续补充风险偏好、价值排序、家庭协同或学校资源等验证信息
- `runtime_contract.json` 补齐选科场景所需 facts，包括 `decision_stage`、`demand_type`、`conflict_tags`、候选组合、学科成绩/百分位、目标专业/职业、家庭意见、约束条件、风险偏好、推荐方案和行动建议
- 扩展 `routing_examples`，覆盖“想学医怎么选科”“已选组合后悔”“家长和孩子选科意见冲突”“学校不开目标组合”等常见选科入口
- 新增回归测试，确认选科参谋 Skill 已加载业务工作流和诊断字段，不再只是占位说明

#### Runtime 时间戳注入

- `PromptAssembly` 的 core prompt 新增 `# Runtime Clock`，每轮向模型注入中国时区 `Asia/Shanghai` 的当前时间、日期、星期和 UTC 时间
- 模型回答“今天/明天/今年/当前招生季”等相对时间问题时，有明确时间抓手，避免沿用训练语料中的旧时间感
- 新增回归测试，确认 prompt 中包含 `china_timezone=Asia/Shanghai` 和 `china_datetime`

#### 前端调试面板增强

- 会话事件流新增 `tool_result` 与 `retrieval_context` 调试事件，分别记录 runtime 实际执行的工具调用结果，以及 prompt 组装阶段自动检索命中的 reference / local asset 片段
- 前端调试模式新增“工具 / 检索结果”卡片区，可查看工具名、调用参数、来源、命中内容预览、完整结果和结构化 JSON，方便排查模型是否真实调用工具、命中了哪些资产
- 前端事件日志卡片新增“下载日志”按钮，可按当前 `session_id` 下载包含 `snapshot.json` 与 `events.jsonl` 的 zip 包，便于本地排查和复现

### 2026-06-10

#### 多元路径过滤与 Facts 配置对齐

- `convergence.py` 新增基于资产规则的前置硬过滤：当用户已选择 `career_orientation` 时，只保留命中所选职业兴趣的路径；例如选了 `军警类` 后，不再把要求 `飞行员` 的 `三大招飞` 混入候选路径
- 多选职业兴趣时，路径过滤改为“用户已选集合”和“路径要求集合”求交；没有交集的路径会在进入候选集前直接剔除，而不是只做弱打分
- `budget_level` 现统一收口为 `>5万` / `<5万`，并兼容 `大于5万/年`、`5万元以下`、`5万/年以上` 等写法；`common.py`、`schemas/facts.py`、`convergence.py` 的事实规范化和过滤逻辑已同步到同一口径
- 依据 `assets/generated/multiroute/path_catalog.json` 中的预算规则，对 `中外合作办学`、`港澳升学`、`海外升学` 等明确高预算路径增加 `<5万` 前置过滤，避免低预算家庭仍看到高预算方向
- `config/facts_schema.yml` 已补齐与 `config/fact_form_config.yml` 一致的表单元数据与枚举口径，重点同步了 `budget_level`、`career_orientation`、`exam_qualification_status`、`special_identity_tags`、`hukou_years` 等字段的 `allowed_values / placeholder / submit_mode`
- 新增后端回归测试，覆盖职业兴趣硬过滤、5 万预算阈值过滤，以及表单值到事实存储值的规范化映射

### 2026-06-09

#### 成熟 Agent 风格的引用可见性控制

- 为 runtime metadata 新增 `response_policy`，支持按 skill 声明 `citation_visibility`、是否允许提及来源类别、是否允许输出文件名/参考编号、以及是否启用最终回复 sanitize
- `response_policy` 现以内置默认值生效，新增 skill 时通常不需要在每个 `SKILL.md` 里重复声明；仅当少数 skill 需要放开或改写默认引用展示策略时再单独覆盖
- `main_planner` 的 `SKILL.md` 已去掉重复的 `response_policy` 配置，只保留业务层规则文案，避免 metadata 样板在后续新增 skill 时反复复制
- `session.py` 现默认对模型侧 retrieval context 做匿名化处理：`Reference Catalog` / `Local Asset Catalog` / `Retrieved Knowledge Snippets` 不再把 `references/*.md` 文件路径和标题直接喂给模型，而是改成摘要计数和 `Supporting Snippet N`
- 保留前后端调试能力：`PromptAssembly.retrieved_items` 与 `prompt_assembly.retrieved_sources` 仍保存真实命中来源，调试面板和事件日志可继续查看实际命中了哪些 reference
- `cli.py` 与 `main_planner.py` 的最终回复输出新增引用清洗逻辑；即便模型偶发复述 `参考文献06`、`references/xx.md`，也会在用户可见层改写为泛化的“平台内知识库/平台内规则”表达
- 前端调试模式为主要调试卡片新增“一键复制当前卡片内容”按钮，覆盖 Prompt 调试卡片、其他事件卡片，以及 Summary 面板里的 facts / 决策链路卡片
- 更新回归测试，覆盖匿名化 retrieval prompt、response policy schema，以及用户可见回复中的引用清洗行为

#### main_planner 画像矩阵驱动推荐

- 为 `main_planner` 的 `SKILL.md` 新增 `planner.scene_selection` 配置，正式声明 `mode=profile_matrix`、画像矩阵引用文件和匹配字段，不再把 `06_用户画像&规划策略&可探索场景.md` 仅当作普通 reference
- 新增 `skill_runtime/profile_matrix.py`，支持解析 `06_用户画像&规划策略&可探索场景.md` 表格，并按 `学段 / 成绩 / 兴趣特长` 聚合推荐 scene
- `main_planner.py` 现优先按子 skill registry metadata 做显式 scene 命中，再在用户表达“迷糊 / 拿不准 / 不知道怎么规划”时走画像矩阵推荐；原有 `SCENE_HINTS` 退居兜底
- `main_planner` 在多元路径续接时，会把用户直接补充的 `初二 / 高一` 等学段表达同步写回 `grade` fact，保证后续子 skill 复用稳定
- `prompt` 检索侧增强中文 token 切分，避免“浙江高中选科规则是什么”这类整句中文无法命中 reference 的问题；同时下调 generated asset 在 retrieval 排序中的优先级，减少其压过 `references/` 的情况
- `session.py` 修正“显式声明 `generated_domains: []` 却仍默认暴露全部 generated assets 域”的问题
- 更新 `mock_admission`、`multi_path_planning`、`interest_explore`、`score_improve` 的 `triggers`，让显式意图更多通过 `SKILL.md` 元数据命中，而不是靠桥接层硬编码
- 更新回归测试，新增画像矩阵配置与“初中 + 特长 + 迷茫表达”自动推荐到 `junior_multi_path_planning` 的覆盖

#### Skill Runtime 标准化与渐进式 Prompt 加载

- 为 `main_planner`、`future_explore`、`interest_explore`、`score_improve`、`subject_advisor`、`junior_multi_path_planning` 补齐标准化 `SKILL.md` frontmatter，新增 `skill_type`、`entrypoint_role`、`prompt_loading`、`retrieval`、`assets`、`debug` 等元数据
- `skill_loader.py` 改为使用正式 YAML frontmatter 解析，并新增 runtime metadata 模型；`runtime_contract.json` 继续承载 stages / facts / routes 契约
- runtime skill 目录统一支持本地 `assets/`，原生 skill 已补齐 `runtime_skills/<skill>/assets/` 目录；本地轻量 assets 与全局 `assets/generated` 形成混合资产模式
- `session.py` 新增分层 `PromptAssembly`：`core / retrieval / final`，`references/` 不再默认整包注入 prompt，而是通过轻量检索按需注入 top-k snippets
- `asset_lookup.py` 与本地 RAG 文档遍历已支持 skill 本地 `assets/` 和 skill 声明的 `generated_domains`
- `main_planner` 的 `prompt_assembly` 事件改为记录 `layer`、`reference_strategy`、`retrieved_sources`、`retrieved_count`、`generated_asset_domains`、`local_asset_paths`
- 前端 `EventPanel` 已增加 prompt layer、skill type、reference strategy、检索来源和资产域展示，便于调试渐进式加载
- 新增文档 `docs/architecture/skill_standard.md`，并更新 `README.md`、`docs/architecture/skill_runtime_integration.md`
- 新增回归测试：`tests/test_skill_metadata_schema.py`、`tests/test_prompt_progressive_loading.py`，并补充 `tests/test_runtime_bridge.py` 的 prompt layer 校验

#### 初中多元路径 skill 拆分

- 新增 `runtime_skills/junior_multi_path_planning/`，作为专门承接初中多元路径规划的 runtime skill
- `main_planner` 现会结合会话里的学段线索做多元路径分流：初中命中 `junior_multi_path_planning`，高中继续命中 `multi_path_planning`
- 当用户表达多元路径意图但当前学段未知时，系统会先追问孩子年级，并保留待续接的多元路径场景；用户补充 `初二` / `高一` 后可自动继续进入对应 skill
- `multi_path_planning` 的语义收口为高中 / 新高考导向，多元路径不再默认混用初中场景
- `runtime_skills/e生涯升学顾问v017/SKILL.md` 与参考文档 `06_用户画像&规划策略&可探索场景.md` 已同步更新，明确初中的多元路径要进入专门的初中 skill
- 新增 runtime bridge 回归测试，覆盖初中多元路径 skill 注册与路由命中
- 修正 `runtime_skills/e生涯升学顾问v017/references/04_新高考选科规则.md` 的适用边界：该文档现在仅在已确认省份为浙江时才可引用；省份未知时需先按 `docs/模拟升学/00_省份分类规则.md` 做省份分类，避免把用户误默认成浙江
- 进一步收窄上述约束的适用范围：这条“不要默认按浙江处理”的规则仅针对高中选科讨论；小学和初中阶段默认不主动进入选科制度分析
- 将该参考文档标题进一步收口为“高中选科制度引用边界（浙江 3+3）”，降低其被误理解为通用全国规则的风险

### 2026-06-08

#### 部署脚本增强：HTTPS 证书链兜底

- `deploy-all.sh` 启动后端时自动探测常见系统 CA bundle 路径，并在未显式配置时设置 `SSL_CERT_FILE`
- 启动后端进程时透传 `SSL_CERT_FILE/SSL_CERT_DIR`，避免服务器环境出现 `CERTIFICATE_VERIFY_FAILED`

#### Thinking / Reasoning + 流式展示增强

- DashScope compatible 的流式解析兼容更多返回形状：在 `delta` 为空时可从 `message` 字段回落解析内容与 reasoning，避免开启 thinking 后出现“只有 thinking 没有主回复”
- SSE `final_message` 增加 `reasoning` 字段；同步接口 `/messages` 同样返回 `reasoning`，便于前端在非增量场景下展示/回放
- SSE 增加定期 `: ping` 心跳，改善部分代理/网关下的流式刷新与连接保活
- 前端把 thinking 合并到消息内的“推理进度”卡片中展示，推理完成后默认折叠
- `main_planner` 路由到 hailiang bridge skill / fallback chat 时，若下游未主动流式输出，bridge 层会补发分块回复，改善 skill-runtime 模式下的非流式观感
- `main_planner` 与 `intent_tracker` 新增多元路径规划自然语言别名识别，像“除了普通高考还有什么路”“还有哪些升学路径”会直接路由到 `multi_path_planning`
- 修复 `planner_state` 跨轮残留：用户补完“省份 / 分数 / 选科”后，旧的 `missing_facts` 和补充信息卡片不会在后续 assistant 消息里重复出现
- 修复显式未知事实处理：像“最近三次大考均分：目前未知”会按暂缺条件处理，不再被当成有效筛选值；系统会记录 `unknown_fact_keys` 并在多路径回复中提示后续补充后再进一步确认
- 修复结构化补充信息误路由：像“高考省份：浙江；选科组合：物理”这类续答补 facts 不会再因为字段名命中关键词而误切到 `subject_advisor`

### 2026-06-03

#### 会话开场白配置

- 新增后端开场白配置文件：`config/session_opening.yml`
- 新增配置加载模块：`src/hailiang_skills/core/session_opening_config.py`
- `POST /api/v1/sessions` 现会在新建会话时按配置写入第一条 assistant 开场白，并在返回体中附带 `opening_message`
- `SessionContext.add_message()` 现统一写入 `created_at`，便于会话快照和前端消息时间展示复用
- 前端创建新会话后会直接渲染后端返回的开场白，不再出现“新会话创建后聊天区为空白”的情况

### 接口 & Agent 联动功能优化

围绕"接口 & Agent 联动功能优化"，当前仓库已补齐以下主干能力：

#### 后端 API 层

- facts 模型从旧的 `user_facts + session_facts` 升级为 `shared_facts + profile_facts + session_facts + effective_facts`
- 身份模型明确为：
  - `user_id`：家长/账号
  - `profile_id`：孩子档案
  - `session_id`：某个孩子下的聊天会话
- `POST /api/v1/sessions` 现支持在创建时显式绑定 `user_id` 与 `profile_id`
- 新增按当前孩子查看历史会话接口：`GET /api/v1/users/{user_id}/profiles/{profile_id}/sessions`
- 新增会话标题更新接口：`PATCH /api/v1/sessions/{session_id}`
- 新增 profile 管理接口：
  - `GET /api/v1/users/{user_id}/profiles`
  - `POST /api/v1/users/{user_id}/profiles`
  - `GET /api/v1/users/{user_id}/profiles/{profile_id}`
  - `PATCH /api/v1/users/{user_id}/profiles/{profile_id}`
- facts 管理接口扩展为三层：
  - shared：`/api/v1/users/{user_id}/facts*`
  - profile：`/api/v1/users/{user_id}/profiles/{profile_id}/facts*`
  - session：`/api/v1/sessions/{session_id}/facts*`
- 保留 `POST /api/v1/users/{user_id}/facts:clear-by-source` 作为 shared facts 的按来源清理入口
- 会话上下文与消息返回新增：
  - `profile_id`
  - `profile_name`
  - `shared_facts`
  - `profile_facts`
  - `session_facts`
  - `effective_facts`
- 继续支持流式消息接口：`POST /api/v1/sessions/{session_id}/messages/stream`
- SSE 最终消息同样返回三层 facts、profile 信息与推理状态

#### 后端 Skill 层

- 场景编排从单一 `path_recommendation` 正式拆成两个已上线业务场景：
  - `admission_simulation`：模拟升学
  - `multi_path_planning`：多元路径规划
- `scenarios.yml` 现明确区分两套 phase：
  - `admission_simulation`：`collect_info -> admission_analysis -> school_lookup -> final_recommend`
  - `multi_path_planning`：`collect_info -> match_paths -> deep_drill -> school_lookup -> final_recommend`
- 继续收口双场景配置：
  - 修复 `multi_path_planning.collect_info` 下重复 `skill_candidates` 键的 YAML 覆盖风险
  - 明确 `multi_path_planning.collect_info` 只承接 `router / facts_extractor / planner`，真正的路径匹配从 `match_paths -> convergence` 开始
- `skills_registry.yml` 中的业务 skill 主归属已重排：
  - `admission -> admission_simulation`
  - `convergence / path_drilldown / terminate_or_recommend -> multi_path_planning`
  - `school_intro -> admission_simulation`，但通过 `scenarios.yml` 在两场景共享 `school_lookup`
- `skills_routing_config.yml` 已补场景级 fallback 语义：
  - `admission` 类关键词默认落 `admission_simulation`
  - `convergence / path_drilldown` 类关键词默认落 `multi_path_planning`
- `facts_schema.yml` 现区分：
  - 两场景共享的基础 facts
  - `admission_simulation` 更偏学校/模拟升学侧的 facts
  - `multi_path_planning` 更偏路径规划/路径偏好侧的 facts
- `facts_config.get_facts_by_scenario()` 已支持 `scenario_group`，可以正确读取双场景共享 facts
- `ScenarioEngine / router / planner / prompt_registry` 已去掉对单一 `path_recommendation` 场景的默认依赖
- router 现在会同时决策 `target_skill + target_scenario`，并兼容两类“补 facts 连续处理”：
  - `admission_simulation` 下补充分数/省份/学校相关信息时，保持 admission 连续处理
  - `multi_path_planning` 下补充预算/民族/户籍等路径判定条件时，保持 convergence 连续处理

- `planner.missing_facts` 现只允许使用 `facts_schema.yml` 中已定义且启用的 fact_key，防止模型自由发挥生成未知字段
- `fact_form` 在过滤后没有可渲染字段时不会出卡片
- facts 写入与收敛判定补充了标准化处理，重点修复了 `浙江省` / `浙江` 这类省份格式不一致导致的规则误判
- 修复 SSE 运行期回调被写入 `session_meta` 导致的 `Object of type function is not JSON serializable` 异常
- 修复 router 对 follow-up facts 的误判：
  - 只有当前消息里显式提到路径时，才会强制走 `path_drilldown`
  - `预算水平：... / 民族信息：... / 学考是否合格：...` 这类“字段: 值”补充，会优先识别为事实补充而非路径深挖
- planner 现会复用 `convergence` 的 `partial candidate / missing_slots` 逻辑：
  - 在多元路径首轮里，即使 `student_province / subject_group / score_total` 已齐，也会从“部分条件满足路径”里抽取高价值缺失 facts
  - 这些缺失 facts 会直接生成结构化 `fact_form`，用于补充预算、民族、学考等高影响字段
- planner 的高价值 facts 表单触发范围已从 `collect_info` 扩展到 `match_paths`：
  - 即使已经进入路径收敛阶段，只要仍存在 `partial candidates`，也会继续产出结构化 `fact_form`
  - 避免出现“自然语言说还缺信息，但没有结构化表单”的割裂体验
- `fact_form` 字段顺序已加优先级：
  - `student_region`
  - `special_identity_tags`
  - `physical_requirements`
  - 其余字段按字段名稳定排序
- `convergence` 的“部分条件满足”回复现会优先对齐 `planner.missing_facts`：
  - 只展示当前结构化 `fact_form` 中实际会出现的字段
  - 避免正文写出一组缺失项，但表单里实际出现的是另一组字段
- 前端 facts 表单自动提交生成的追问消息改为使用中文分号 `；` 分隔字段：
  - 例如 `民族信息：汉族；户籍年限：3 年；监护人户籍一致性：true；学籍年限：3 年`
  - 便于在聊天输入/消息展示中直观看出字段边界

#### 前端交互层

- 首页已升级为三栏布局：
  - 左侧：测试账号、孩子切换、当前孩子历史会话
  - 中间：连接控制、Facts 管理区、聊天区
  - 右侧：摘要与事件流
- 左侧历史会话现支持：
  - 只展示当前孩子的聊天记录
  - 点击某条历史会话恢复聊天记录、facts、候选路径、skill 状态与事件
  - 会话标题内联重命名
- 前端 store 已扩展为“登录态 + profile + 会话列表 + 当前会话”模型
- 本地假登录从固定默认账号升级为“可切换测试账号”
- 前端新增 auth provider 抽象，当前 demo 登录只是一种实现，后续可替换为真实业务后端登录结果
- 当前测试账号的最近 `profile_id/session_id` 会按账号维度本地保存，切回账号后可恢复最近工作区
- 测试账号在首次登录或切换到全新 `user_id` 时，前端会自动创建默认孩子档案 `孩子 1`，不再停留在“没有孩子”的空状态
- 顶部状态区新增 `Scenario` 展示，和 `Active Skill` 一起显示当前对话所处的场景/技能状态
- 新增“对话模式 / 调试模式”切换：
  - 对话模式隐藏右侧调试卡片
  - 调试模式展示响应摘要、资产支撑、未覆盖维度、资产列表、模拟升学命中、学校问询、候选路径、事实快照、决策链路、事件日志等卡片
- `展示模式 / 主题模式 / 注释` 控制卡已从左侧栏移动到页头右侧：
  - 改成更紧凑的双行控制区
  - 减少顶部垂直占高，也避免左侧栏信息过长
- 对话模式下进一步调整布局：
  - `对话区域` 上移并占用更宽的中间栏
  - 右侧栏顺序改为 `测试登录` -> `孩子档案` -> `连接控制` -> `Facts 管理`
  - 左侧栏只保留当前孩子历史会话等对话上下文入口
- 页面容器已加宽并缩小左右 padding，缓解长回复和结构化信息在聊天区中过窄的问题
- 新增“白天模式 / 夜间模式”切换：
  - store 新增 `themeMode`
  - 通过 `body[data-theme]` 驱动全局浅色主题覆盖，统一调整页面背景、卡片、输入框、文本与 markdown 展示
- 对话模式下，顶部 `消息数 / Active Skill / Scenario / 事件数` 四张状态卡会直接隐藏，避免分散主聊天区注意力
- 白天模式继续补齐按钮态适配：
  - 覆盖主按钮、次按钮、模式切换按钮的边框、文字、背景和 hover 颜色
  - 修复浅色背景下按钮文字过浅、选中态不明显的问题
- Facts 管理区已升级为三层编辑：
  - 家庭共享 facts
  - 当前孩子 facts
  - 当前会话 facts
- 点击“填写 / 更新 Facts”打开弹窗时，现在会把当前已保存的 facts 自动回填到表单中，不再只显示空白字段
- SummaryPanel 已同步展示 `shared_facts / profile_facts / session_facts / effective_facts`
- assistant 气泡继续支持通用 `message_blocks`：
  - `status_timeline`
  - `fact_form`
  - `path_actions`
  - `citations`
- 前端保留同步 `POST /sessions/{id}/messages` 作为 SSE fallback

---

## 历史更新日志

### 2026-05-29

#### Prompt 前端调试展示 + 预生成示例（步骤0）

- **BaseSkill 基类增强**：[base.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/base.py)
  - 新增 `_last_prompt_info` 属性和 `get_prompt_for_llm()` 方法
  - 每个 Skill 的 LLM 调用后写入 `_last_prompt_info`，供 Orchestrator 读取

- **Prompt 记录辅助函数**：[common.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/common.py)
  - 新增 `build_prompt_record()` 和 `build_skill_prompt_record()` 函数
  - 标准化 prompt 记录结构
  - 支持把对应的 `llm_response` 一并写入 `prompt_assembly` 事件

- **Orchestrator 事件记录**：[orchestrator.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/core/orchestrator.py)
  - 每次 Skill 的 `run()` 完成后，读取 `_last_prompt_info` 写入 `prompt_assembly` 事件

- **前端 EventPanel 增强**：[EventPanel.tsx](file:///Users/ayi/Project/hailiang-skills/frontend/src/components/EventPanel.tsx)
  - 新增 `PromptAssemblyCard` 组件（青色高亮展示）
  - 在调试面板中展示每个阶段的完整 prompt 内容和变量值
  - 新增独立按钮，可展开查看对应的 `LLM Response`

- **前端 StatusPill 增强**：[StatusPill.tsx](file:///Users/ayi/Project/hailiang-skills/frontend/src/components/StatusPill.tsx)
  - 新增 `"info"` tone（青色主题）

- **Prompt 示例生成脚本**：[generate_prompt_examples.py](file:///Users/ayi/Project/hailiang-skills/scripts/generate_prompt_examples.py)
  - 根据配置运行后生成 8 个静态 `.md` 文件供业务预览
  - 输出目录：`config/prompts_examples/`

#### Skill 路由配置外置（步骤1）

- **路由关键词配置**：[skills_routing_config.yml](file:///Users/ayi/Project/hailiang-skills/config/skills_routing_config.yml)
  - Skill 功能描述 + 触发关键词
  - 预留未来场景扩展关键词

- **Skill 注册表**：[skills_registry.yml](file:///Users/ayi/Project/hailiang-skills/config/skills_registry.yml)
  - Skill 元信息、归属场景、入口阶段、可跳转目标
  - 预留规划中场景的 Skill 占位

- **配置加载模块**：[routing_config.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/core/routing_config.py)
  - `get_routing_keywords()` / `get_skill_registry()` / `get_skill_info()` 等函数

- **Router 改造**：[router.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/router.py)
  - 硬编码关键词改为从配置文件读取
  - 新增 `_match_keyword_fallback()` / `_has_explicit_terminate_intent()` 使用配置

#### Facts 配置外置（步骤2）

- **Facts Schema**：[facts_schema.yml](file:///Users/ayi/Project/hailiang-skills/config/facts_schema.yml)
  - 统一维护 fact 的 `label`、`value_type`、`extractor`、`scenario`、`enabled`
  - 预留学生画像 / 选科专业 / 兴趣行动计划等规划中场景的 fact 占位

- **Facts 配置加载模块**：[facts_config.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/core/facts_config.py)
  - 提供 `get_fact_schema()` / `get_fact_labels()` / `get_enabled_fact_keys()` 等方法

- **Asset Support 改造**：[asset_support.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/asset_support.py)
  - `format_fact_label()` 改为从 `facts_schema.yml` 读取 label

- **FactsExtractor 改造**：[facts_extractor.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/facts_extractor.py)
  - fallback 抽取字段集合改为按 `facts_schema.yml` 的启用项过滤
  - 新增 `_build_fallback_updates()`，把 fallback 抽取聚合到统一入口

#### Scenario 框架 + LoopDefense（步骤3）

- **Scenario 配置**：[scenarios.yml](file:///Users/ayi/Project/hailiang-skills/config/scenarios.yml)
  - 定义当前已上线的 `path_recommendation` 场景
  - 预留 `profile_building`、`subject_selection`、`interest_plan` 三个规划中场景

- **Loop 防御配置**：[loop_prevention.yml](file:///Users/ayi/Project/hailiang-skills/config/loop_prevention.yml)
  - 管理 `scenario_switch_lock`、`skill_stability`、`fact_ask_once`、`confidence_threshold`

- **ScenarioEngine**：[scenario_engine.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/core/scenario_engine.py)
  - 负责初始化当前场景、根据 skill 推进 phase、记录 phase transition 事件

- **LoopDefense**：[loop_defense.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/core/loop_defense.py)
  - 负责 skill 稳定性判断、fact 追问去重记录、场景切换计数

- **Orchestrator 集成**：[orchestrator.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/core/orchestrator.py)
  - 会话首次进入时初始化 scenario / phase
  - 执行具体 skill 前应用稳定性防御
  - 记录 `scenario_initialized`、`phase_transition`、`loop_defense_triggered` 等事件

#### Router 场景感知改造（步骤4）

- **Router 场景感知**：[router.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/router.py)
  - 新增 `target_scenario` / `requested_scenario`
  - 当用户表达"画像 / 选科专业 / 兴趣行动计划"意图时，可识别目标场景
  - 对于仍处于 `planning` 的场景，当前版本只记录场景意图，不直接切换执行链路

- **场景关键词匹配**：[routing_config.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/core/routing_config.py)
  - 新增 `get_scenario_keyword_match()`
  - 使用"最长关键词优先"解决"兴趣"与"兴趣行动计划"的匹配歧义

- **Orchestrator 场景切换保护**：[orchestrator.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/core/orchestrator.py)
  - Router 请求切换场景时，先检查场景是否 `active`
  - 再经过 `LoopDefense` 的 `scenario_switch_lock` 判断
  - 对未上线场景写 `scenario_switch_skipped` 事件而不是直接切换

#### 环境依赖变化

- **项目依赖**：[pyproject.toml](file:///Users/ayi/Project/hailiang-skills/pyproject.toml)
  - 新增 `PyYAML>=6.0`
  - 原因：`routing_config.py`、`facts_config.py`、`scenario_engine.py`、`loop_defense.py` 都已开始读取 YAML 配置

#### Planner 阶段感知改造（步骤5）

- **Planner 阶段感知**：[planner.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/planner.py)
  - 修复错误导入，改为从 `llm.service` 读取 `build_context_snapshot` / `safe_complete_json`
  - 新增 `_phase_aware_fallback()`，按 `current_scenario` / `current_phase` 生成 fallback plan
  - 在 `collect_info` 阶段优先追问关键 fact，在 `match_paths` / `deep_drill` / `school_lookup` / `final_recommend` 阶段输出对应目标
  - 规划结果增加 `scenario_id` / `phase_id`

- **Planner Prompt 补充**：[prompt_registry.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/llm/prompt_registry.py)
  - 明确要求结合 `context.interaction_state.current_scenario` / `current_phase` 做规划

- **后端启动问题修复**
  - 修复前：`planner.py` 从错误模块导入 `build_context_snapshot`
  - 修复后：已验证 [main.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/api/main.py) 可正常 import，uvicorn 可启动，`/health` 可访问

#### 排除类路径约束修复

- **Facts Schema 补充**：[facts_schema.yml](file:///Users/ayi/Project/hailiang-skills/config/facts_schema.yml)
  - 新增 `excluded_path_ids` / `excluded_primary_categories`
  - 用于表达"除了某条路径/某类路径，看看别的方案"这类排除式探索约束

- **公共抽取函数**：[common.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/common.py)
  - 新增 `has_alternative_exploration_intent()` / `extract_excluded_targets()`
  - 统一识别"除了 X 还有什么路 / 不考虑 X 看别的路径"这类语义

- **FactsExtractor 改造**：[facts_extractor.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/facts_extractor.py)
  - 把被排除路径写入 `excluded_*` facts
  - 自动把被排除目标从 `focus_path_ids` / `focus_primary_categories` 中剔除，避免同一目标同时被当作"关注"和"排除"

- **Convergence 改造**：[convergence.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/convergence.py)
  - 生成候选路径前先消费 `excluded_*` facts 做过滤
  - 在事件和 ranking snapshot 中记录排除条件，便于调试链路回放

- **Planner / Prompt 补充**：[planner.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/planner.py)、[prompt_registry.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/llm/prompt_registry.py)
  - 当当前语义是"排除已知路径后看其他方案"时，优先把目标收敛到 `convergence`
  - 避免继续把被排除路径误判成 `path_drilldown`

- **PathDrillDown 防误入保护**：[path_drilldown.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/path_drilldown.py)
  - 当输入明显是替代路径探索、且当前没有明确点名要深挖的路径时，阻止单路径深挖
  - 写入 `path_drilldown_guard_blocked` 事件，方便定位误入问题

- **消息链路运行时修复**
  - 修复前：[path_drilldown.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/skills/path_drilldown.py) 调用 `build_prompt_record()` 但未导入，导致前端发送第二轮深挖消息时后端返回 500
  - 修复后：补齐 `build_prompt_record` 导入，并清理未使用的 `build_skill_prompt_record` 导入
  - 已验证：全新 uvicorn 实例下，`/sessions`、首轮 admission、二轮 `path_drilldown` 请求均返回 `200`

- **SSE 序列化异常修复**
  - 修复前：流式消息链路把 `status_callback` / `reply_delta_callback` 这类运行期函数写入 `context.session_meta`，后续保存 `snapshot.json` 时触发 `Object of type function is not JSON serializable`
  - 修复后：[streaming_runner.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/core/streaming_runner.py) 在持久化前清理运行期回调；[session_logging.py](file:///Users/ayi/Project/hailiang-skills/src/hailiang_skills/core/session_logging.py) 对 `session_meta` 做 JSON 安全清洗，避免不可序列化对象再次写盘失败
