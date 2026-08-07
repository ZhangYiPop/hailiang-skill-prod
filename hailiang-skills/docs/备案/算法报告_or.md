# 算法报告

报告名称：e生涯升学规划顾问大模型对话与技能路由算法报告  
报告版本：V1.0（项目现状版）  
编制日期：2026-07-03  
适用系统：`hailiang-skills` 面向升学咨询场景的 Skill Runtime 融合版对话引擎  

> 本报告用于配合大模型登记备案、算法备案或安全评估材料准备。当前项目以调用第三方大模型 API 为核心，不包含基础大模型训练或模型权重发布。算法备案填报时，可根据主管部门填报口径，将本系统拆分为“生成合成类算法”“调度决策类算法”“检索过滤类算法”或作为同一应用内的组合算法说明。

## 一、算法基本信息

| 项目 | 内容 |
| --- | --- |
| 算法名称 | e生涯升学规划顾问大模型对话与技能路由算法 |
| 算法版本 | V0.1.0 / 以实际上线版本为准 |
| 算法类型建议 | 生成合成类、调度决策类、检索过滤类 |
| 所属业务 | 教育升学规划咨询 |
| 应用场景 | 家长/学生通过对话咨询升学路径、选科、模拟升学、提分、兴趣特长、院校介绍等问题 |
| 输出形态 | 自然语言回复、结构化 message blocks、候选路径、facts 表单、下一步场景建议 |
| 是否直接影响录取/评价结果 | 否。系统仅提供辅助建议，不作为招生录取、考试评价、官方报考资格判断依据 |
| 是否使用用户数据训练模型 | 当前代码未体现用户数据训练、微调或回流基础模型机制 |

## 二、算法组成

本系统由三类算法能力组合而成：

### 2.1 生成合成类算法

调用第三方大模型 API，对用户输入、会话历史、facts、Skill 指令、检索片段和工具结果进行自然语言生成，输出升学咨询回复、追问、总结、风险提示和路径说明。

代码依据：

1. `src/hailiang_skills/llm/client.py`
2. `src/hailiang_skills/skill_runtime/llm_client.py`
3. `config/llm/qwen_dashscope.json`

### 2.2 调度决策类算法

根据用户意图、会话状态、场景锁、路由配置和 skill metadata，判断当前应进入哪个业务 skill，并控制是否继续当前场景、切换场景或回到主规划顾问。

代码依据：

1. `src/hailiang_skills/runtime_bridge/main_planner.py`
2. `src/hailiang_skills/skill_runtime/intent_router.py`
3. `src/hailiang_skills/core/loop_defense.py`
4. `runtime_skills/*/SKILL.md`
5. `runtime_skills/*/runtime_contract.json`

### 2.3 检索过滤类算法

对本地 references、local assets、`assets/generated` 结构化资产进行召回、过滤、排序和片段注入，辅助模型生成有依据的回复。

代码依据：

1. `src/hailiang_skills/skill_runtime/session.py`
2. `src/hailiang_skills/skill_runtime/tools.py`
3. `src/hailiang_skills/skill_runtime/asset_lookup.py`
4. `assets/generated/*`
5. `runtime_skills/*/references/*`

## 三、算法目标

1. 识别用户在升学咨询中的真实意图和学段场景。
2. 收集最小必要 facts，避免反复追问和无关采集。
3. 将用户问题路由到最合适的业务 skill。
4. 结合结构化升学资产和本地知识库生成可解释建议。
5. 对缺失条件、风险点、不确定政策和官方核验要求进行提示。
6. 将复杂升学咨询拆成可继续推进的下一步场景建议。

## 四、输入、处理和输出

### 4.1 输入数据

| 输入 | 示例 | 来源 |
| --- | --- | --- |
| 用户自然语言 | “浙江物理 580 分能上什么学校？” | 前端聊天输入 |
| 会话历史 | 多轮用户与 assistant 消息 | `SessionContext.messages` |
| 用户/孩子 facts | 年级、省份、选科、分数、预算、特长、职业兴趣等 | 手动填写、对话抽取、历史档案 |
| Skill metadata | routing examples、slot facts、skill type、prompt loading 策略 | `runtime_skills/*/SKILL.md` |
| Runtime contract | stages、routes、promote facts 等 | `runtime_contract.json` |
| 本地知识库 | 升学规则、选科规则、画像矩阵、兴趣特长规则 | `references/` |
| 结构化资产 | 模拟升学、多元路径、院校介绍、选科要求 JSON | `assets/generated/` 和 skill assets |
| 模型配置 | provider、base_url、model、temperature、max_tokens | `config/llm/qwen_dashscope.json` |

### 4.2 主要输出

| 输出 | 说明 |
| --- | --- |
| assistant_message | 面向用户展示的自然语言回复 |
| message_blocks | 表单、引用、状态进度、路径操作卡片等结构化 UI 块 |
| active_skill | 当前实际执行的业务 skill |
| facts_updated | 本轮更新的事实字段 |
| route_suggestions | 可进一步选择的规划主题 |
| suggested_paths | 多元路径或模拟升学候选路径 |
| risk_alerts | 业务风险提示 |
| session/events 日志 | 用于审计、调试和质量追踪 |

## 五、算法流程

```text
1. 接收用户消息
2. 加载 user/profile/session facts，合成 effective_facts
3. 记录本轮上下文和运行元数据
4. Intent Router 判断意图：
   - 明确意图：进入对应 child skill
   - 模糊规划诉求：进入 main_planner
   - 当前已有任务锁：优先续接当前 skill
5. Skill Runtime 构造 prompt：
   - Skill 指令
   - 会话状态
   - Runtime facts
   - route targets
   - 工具能力
   - 本轮匹配资产和检索片段
6. 按需调用工具：
   - status_track
   - local_rag
   - subject_requirements
   - bridge skill 结构化资产查询
7. 调用第三方大模型 API 生成或补全回复
8. 执行输出清洗与引用隐藏
9. 写回 facts、skill state、candidate paths、events
10. 返回前端同步响应或 SSE 流式响应
```

## 六、意图路由算法说明

### 6.1 路由依据

路由算法读取各 skill 的 metadata：

1. `routing.scene_name`
2. `routing.intent_clarity`
3. `routing.routing_examples`
4. `routing.slot_facts`
5. `routing.school_stage_scope`
6. `accepts_scenes`
7. `triggers`

系统支持的主要路由目标：

| Skill | 触发意图 |
| --- | --- |
| `main_planner` | 模糊规划诉求、综合画像判断、统一入口 |
| `mock_admission` | 模拟录取、可报学校、院校层次、分数能上 |
| `multi_path_planning` | 高中多元路径、普通高考之外的升学路径 |
| `junior_multi_path_planning` | 初中多元路径、中考保底、职教/普职融通 |
| `subject_advisor` | 选科、科目组合、目标专业约束 |
| `interest_explore` | 兴趣班、特长培养、适合什么赛道 |
| `score_improve` | 提分、学习方法、学习问题 |
| `future_explore` | 专业职业探索、前景探路、长期发展 |

### 6.2 路由策略

1. 明确需求不被 `main_planner` 前置拦截，可直接进入对应 child skill。
2. 用户意图不明确、只给零散画像或表达“想规划但不知道从哪里开始”时，进入 `main_planner`。
3. 长消息同时包含年级、成绩、兴趣、选科、专业困惑等多维信息时，优先交给 `main_planner` 生成画像简历和主矛盾判断。
4. 进入 child skill 后启用任务锁，补 facts、普通追问和旁支闲聊默认留在当前 skill。
5. 只有用户明确表达“换到/先看/继续刚才/回到”等切换语义，才触发场景切换。
6. 初中和高中多元路径分流：初中用户进入 `junior_multi_path_planning`，高中用户进入 `multi_path_planning`。

### 6.3 置信度和稳定性控制

`LoopDefense` 提供以下机制：

1. `skill_stability`：低置信度切换需要连续命中，减少误跳转。
2. `scenario_switch_lock`：限制同一轮场景频繁切换。
3. `fact_ask_once`：避免重复追问同一 facts。
4. `confidence_threshold`：为不同 skill 设置不同置信阈值。

## 七、检索增强与资产筛选算法说明

### 7.1 本地知识检索

Skill 的 `prompt_loading` 与 `retrieval` 配置控制知识注入：

| 配置 | 说明 |
| --- | --- |
| `include_references: on_demand` | references 不整包进入 prompt，按需加载 |
| `sources` | 支持 references、local_assets、generated_assets |
| `top_k` | 控制召回片段数量 |
| `snippet_chars` | 控制每个片段进入 prompt 的长度 |
| `include_catalog` | 是否向模型展示资源目录 |

检索范围限制：

1. local RAG 仅检索当前 skill 已加载 references。
2. 对仓库级 `assets/generated` 只读取注册或匹配到的资产域。
3. 不允许模型任意读取项目外文件。
4. 未命中支撑资产时，系统提示模型采用 fallback 风格，不得猜测具体政策或院校细节。

### 7.2 结构化资产

| 资产域 | 来源 | 用途 |
| --- | --- | --- |
| `admission` | `docs/模拟升学/*.md` 编译 JSON | 模拟升学、省份分类、分数段判断 |
| `multiroute` | 多元升学路径 Excel 编译 JSON | 多元路径候选、过滤、风险提示 |
| `school_intro` | 院校介绍 Excel 编译 JSON | 院校介绍文案 |
| `subject_requirements` | 选科规划 Excel 编译 JSON | 专业/职业/选科组合要求查询 |

### 7.3 规则过滤

多元路径、模拟升学等场景会在模型生成前先进行结构化筛选。例如：

1. 按省份、选科、分数、预算、职业兴趣、特殊身份等 facts 筛选候选路径。
2. 对预算高门槛路径，在用户预算 `<5万` 时前置过滤。
3. 对职业兴趣存在硬条件的路径，按职业兴趣过滤。
4. 对未知 facts 不做硬过滤，并在回复中提示补充后才能确认。

## 八、生成算法说明

### 8.1 模型调用方式

当前系统通过 OpenAI Compatible 协议调用第三方 `/chat/completions`：

| 参数 | 当前配置 |
| --- | --- |
| provider | `dashscope_compatible` |
| model | `deepseek-v4-flash` |
| temperature | `0` |
| max_tokens | `8000` |
| timeout | `120s` |
| stream | 支持 |
| enable_thinking | 默认关闭 |
| return_reasoning | 默认关闭 |

### 8.2 Prompt 组成

模型输入由以下部分组成：

1. Skill Metadata。
2. Runtime Clock。
3. Skill Instructions。
4. Session State。
5. Active Skill。
6. Asset Registry Policy。
7. Matched External Assets。
8. Runtime Facts。
9. Skill Route Targets。
10. Tool Capabilities。
11. Routing Hint。
12. Tool Calling Protocol。
13. Turn Guardrail。
14. Asset Lookup Result。
15. Conversation History Snapshot。
16. Retrieved Context。
17. Tool Results。

### 8.3 工具调用控制

1. 每轮工具调用次数受限，避免循环。
2. 空工具调用会被识别和拦截。
3. 工具结果进入 prompt 时默认隐藏内部 source path。
4. 生成完成后会执行 `_sanitize_assistant_reply`，移除异常角色前缀和内部引用文件名。

## 九、模型训练与数据来源

当前项目未体现以下行为：

1. 使用用户数据训练基础模型。
2. 对第三方大模型进行本地微调。
3. 发布或下载模型权重。
4. 建立自动化 RLHF 或在线学习流程。

项目使用的数据主要包括：

1. 项目内人工维护的规则文档、Skill 指令和参考知识库。
2. Excel/Markdown 编译生成的结构化业务资产。
3. 用户会话过程中主动提供或表单填写的 facts。
4. 第三方模型服务返回的自然语言结果。

备案前需由主体确认：

1. 第三方供应商是否将输入输出用于模型训练。
2. 是否存在数据跨境传输。
3. 是否有人工标注团队接触用户会话数据。
4. 是否有生产环境日志脱敏和保留周期制度。

## 十、安全、公平与可解释性

### 10.1 安全控制

| 控制项 | 当前机制 |
| --- | --- |
| 不确定性控制 | 未匹配资产时要求 fallback，不得猜测 |
| 输出清洗 | 隐藏内部引用文件名和 reference 编号 |
| 工具调用限制 | 每轮调用次数限制，重复工具调用拦截 |
| 场景稳定 | 置信阈值、任务锁、连续命中控制 |
| 审计追踪 | 会话 snapshot、events、prompt assembly、retrieval details |
| 事实来源 | facts 记录 source type、source id、source label |
| 缺失事实提示 | 缺少关键 facts 时生成表单或追问 |

### 10.2 公平性要求

算法不得基于民族、地域、性别、家庭经济条件等因素作歧视性判断。相关字段仅可用于识别政策条件、预算约束、路径门槛或个性化咨询需求，不得输出贬损、排斥、刻板化结论。

### 10.3 可解释性

系统通过以下方式提升可解释性：

1. 输出候选路径时展示适配理由、缺失条件、风险提示。
2. 对 facts 变更记录 before/after、scope、source。
3. 对 route suggestions 记录来源，如 `llm_reply_analysis` 或 `strong_format_fallback`。
4. 对检索命中记录 retrieved sources 和事件日志，供调试和审计使用。
5. 前端 message blocks 展示表单、引用、路径操作和状态进度。

## 十一、算法风险与缓解措施

| 风险 | 说明 | 缓解措施 |
| --- | --- | --- |
| 生成幻觉 | 模型可能编造政策、学校、分数线 | 结构化资产优先、未命中 fallback、官方核验提示 |
| 错误路由 | 用户意图被错误分发到其他 skill | 路由 examples、学段 scope、任务锁、LoopDefense |
| 过度确定 | 对升学路径给出确定性承诺 | 风险提示、缺失 facts 表示、人工复核建议 |
| 隐私泄露 | 会话和孩子 facts 进入日志 | 鉴权、访问控制、日志脱敏和保留周期需补齐 |
| 未成年人风险 | 学业和特长建议可能制造焦虑 | Prompt 风格约束、禁止制造焦虑，建议内容安全审核 |
| 供应商风险 | 第三方模型合规或数据使用不明 | 供应商备案号、DPA、调用日志和训练使用限制 |
| 内容安全风险 | 输出违法违规或不适宜内容 | 输入输出安全审核、拒答策略、投诉举报机制 |

## 十二、测试与评估建议

备案前建议建立评估集并形成测试记录：

### 12.1 功能准确性测试

1. 模拟升学：省份、选科、分数齐全与缺失场景。
2. 多元路径：预算、职业兴趣、特殊身份、未知分数场景。
3. 选科参谋：目标专业、目标职业、组合覆盖、已选后悔场景。
4. 兴趣探索：小初阶段、特长经历、负面反馈和标签选择场景。
5. 路由测试：同一句话命中不同学段、明确/模糊意图、场景切换。

### 12.2 安全测试

1. 违法违规内容输入。
2. 隐私泄露诱导。
3. 对未成年人不适宜建议。
4. 歧视性或焦虑营销表达。
5. 要求编造政策、学校、分数线。
6. prompt injection：要求泄露系统提示词、reference 文件名、API key。

### 12.3 评估指标

| 指标 | 建议口径 |
| --- | --- |
| 意图路由准确率 | 标准样例中正确进入目标 skill 的比例 |
| facts 抽取准确率 | 关键字段抽取与人工标注一致比例 |
| 缺失 facts 追问准确率 | 是否只追问当前场景必要字段 |
| 幻觉率 | 输出中无依据学校/政策/数字的比例 |
| 安全通过率 | 安全测试集中正确拒答或安全改写比例 |
| 用户纠错闭环 | 用户更正 facts 后是否覆盖生效 |
| 日志可追溯率 | 问题样例能否定位 prompt、工具和检索来源 |

## 十三、上线与运维要求

1. 建立算法版本管理，记录模型配置、Skill 文件、资产版本和提示词变更。
2. 每次更新 `config/llm/qwen_dashscope.json`、`runtime_skills/*/SKILL.md`、`assets/generated/*` 后执行回归测试。
3. 对高风险回复建立抽检机制。
4. 建立用户投诉、纠错、删除和人工复核流程。
5. 对生产日志设置访问审批、加密存储、定期清理和审计。
6. 对第三方模型 API 调用失败、超时、异常输出建立降级策略。
7. 对生成内容增加显式或隐式标识，确保用户可识别 AI 生成内容。

## 十四、结论

e生涯升学规划顾问算法是一个以第三方大模型为生成核心、以本地 Skill Runtime 为调度核心、以结构化升学资产和本地知识库为依据的组合型 Agent 算法。其主要算法能力包括生成合成、意图路由、技能调度、检索增强、结构化过滤和输出安全清洗。

当前项目没有基础模型训练或微调流程，用户数据主要用于当前会话承接、孩子画像管理和个性化咨询。代码中已具备一定的可解释和风险控制机制，但生产上线和备案前仍需补齐供应商资质确认、内容安全审核、真实鉴权、日志脱敏、未成年人个人信息保护、生成内容标识、测试评估记录和人工复核机制。
