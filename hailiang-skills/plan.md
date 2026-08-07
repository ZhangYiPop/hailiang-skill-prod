你提出的诉求是让整个升学路径规划、多路径收敛的流程有以下变化：

- 支持多样性对话和闲聊，意图识别需要敏感、包容，入口不能死板。

- 用户与路径互动要更灵活：对路径可以随时深挖、追问、暂停、不回答、告知感兴趣、表达疑问等，都能被 skill 化感知和响应。
- 需要 skill 的可组合、可扩展设计，便于后续每一种交互都能拆成独立 skill，加大模型决策和生成自由度。
- 全流程对用户是“自然柔性”，但系统全流程节点和状态都有 log（含路径状态、用户交互状态、风险提示等）。

## Skill Oriented Agent Structure（建议）

下面是按照“Skill”思想（更灵活的面向能力、可插拔、依赖大模型判断的流程）重构你的流程。每个step、交互、判断都以skill可调用单元实现，大模型负责调度与上下文“文档驱动”推理。可落地到LangChain、Flowise、LCEL等SkillRouter/Tool-Calling范式框架。

***

### 1. 顶层结构 Skill: Router

> 入口意图识别Skill

- input: 用户消息（支持闲聊、升学路径请求、对已有路径追问、场景切换等）
- 调用方式：优先大模型基于文档、场景、用户上下文综合判断「意图类型」
- 输出：
  - 闲聊: 转接到ChatSkill
  - 升学模拟: AdmissionSkill
  - 多路径规划: ConvergenceSkill
  - 路径深挖: PathDrillDownSkill（附带目标路径及上下文）
  - 终止/不回答: TerminateOrRecommendSkill

***

### 2. Admission Skill: 升学模拟Skill

- 基础交互Skill：收集基础信息（省份等）、成绩，并推理考试模式（必要时大模型调用省份考试文档说明）。
- 当用户主动闲聊或其它意图时，skill支持“主动让出”路由控制权。
- 推荐生成Skill：根据收集信息，调用大模型（或规则）生成推荐院校/层次&推荐理由。

***

### 3. Convergence Skill: 多路径规划与收敛Skill

> <br />

- 初始化Skill：生成候选路径集合
- 问答策略Skill：
  - 规则问答（固定模板/优先级）
  - LLM智能问答（基于路径收敛目标自动提问、判断何时停止）
- 用户互动Skill（核心升级点）：
  - 路径详情Skill：用户对候选某条路径发起“追问”或“深挖”，Skill自动给出：
    - 当前路径与已知信息的匹配度/风险评估（根据上下文和文档做大模型生成）
    - 明确提示「哪些信息缺失，需要补全」
    - 提供行动建议 or 路径简介
    - state/log: 记录路径追问状态
  - 用户“终止问答/不再补充”Skill：自动输出推荐的结果，生成理由和风险提醒
  - 用户对信息补全Skill：补充新信息后，重新评估风险与推荐

***

### 4. Chat Skill: 闲聊/小对话

独立Skill负责日常闲聊等情感交互，灵活插入，和主流程Skill兼容调度。

***

### 5. Logging/State Management Skill

> <br />

- 交互关键节点都自动log状态、路径匹配信息、补全建议、终止状况等。
- 用户无感，后台记录，便于追溯与优化。

***

## 流程示意（Skill路由视角）

1. 用户输入 → **RouterSkill**
   - 闲聊 → ChatSkill
   - 升学 → AdmissionSkill
   - 多路径规划 → ConvergenceSkill
     - 进入候选展示时可调用 PathDrillDownSkill 支持任意路径的追问与评估
     - 终止/不补全 → TerminateOrRecommendSkill
2. 任意流程节点 → 用户追问 → PathDrillDownSkill
3. 用户输入“不补充了”、“直接推荐” → TerminateOrRecommendSkill
4. 任一Skill到关键节点 → LoggingSkill 记录状态

***

## 伪代码展示（Skill-Graph风格）

```python
class SkillRouter:
    def route(self, user_input, context):
        intent = llm_intent_classify(user_input, context)
        if intent == 'chat':
            return ChatSkill().process(user_input, context)
        elif intent == 'admission':
            return AdmissionSkill().process(user_input, context)
        elif intent == 'convergence':
            return ConvergenceSkill().process(user_input, context)
        elif intent == 'drill_down':
            return PathDrillDownSkill().process(user_input, context)
        elif intent == 'terminate' or intent == 'no_more':
            return TerminateOrRecommendSkill().process(user_input, context)
        else:
            return ChatSkill().process(user_input, context)

# 各Skill按“单一能力原则”设计，全部支持上下文信息交互
```

***

## 重点说明

- **Intent Skill设计松耦合，所有子流程/路径“深挖”或终止/跳转都可Skill化，易于后续扩展；**
- **核心决策和生成交给LLM，对流程节点的灵活路由和可插拔组合提供极大自由度；**
- **每个节点都支持Logging/State记录及追踪，不影响用户体验。**
- **Skill设计使得闲聊（Chat）、路径理解与追问（DrillDown）、收敛与推荐（Convergence/Terminate）、信息补全、路径展示等功能独立，彼此可组合复用。**

***

### 一、整体平台架构方案

你需要支持**自主管理、私有化部署、多用户并发、灵活Skill组合**的AI工作流平台，推荐采用**现代Web后端+前端+LLM服务组合**的分层微服务架构：

#### 1. 总体架构视图

```
 ┌─────────┐   ┌──────────────┐    ┌───────────┐
 │  前端   │   │  后端API层   │    │LLM Service│
 │Vue/React│<->│FastAPI/Fastify│<->│OpenAI/Llama/GLM/自建模型│
 └─────────┘   └──────────────┘    └───────────┘
            ↑            │
            │            │
 ┌─────────────────────────────┐
 │  Skill Orchestrator 服务    │
 │  （技能路由、状态管理、     │
 │    日志、协程、消息队列等） │
 └─────────────────────────────┘
            │
 ┌─────────────────────────────┐
 │    存储（MongoDB/Postgres） │
 └─────────────────────────────┘
```

##### 各层说明

- **前端**：Vue3、React、AntDesign、Tailwind等，支持富媒体和Skill交互组件（聊天框/路径可视化/富交互）。
- **后端API**（推荐FastAPI/Python or Fastify/Node.js）：
  - **用户管理**：多用户隔离、权限系统（JWT/Session）。
  - **Skill Orchestrator**：
    - 动态加载Skill（Skill注册&生命周期管理）
    - 用户/会话路由（意图判别Skill/状态追踪/交互入口）
    - 调度LLM
    - 日志/状态写入数据库
  - **消息队列/异步调度**（可选，用于高并发Skill任务与模型推理分离，加速及弹性扩展，比如Celery/RabbitMQ/Redis Queue）
- **大模型服务层**：
  - 公有云API(OpenAI)、本地Llama/GLM/MiniCPM/ChatGLM/InternLM等，支持自建/切换。
- **数据库**：
  - MongoDB（推荐，无模式、易扩展、天然适合Skill日志/对话）、Postgres/MySQL（配合数据分析）。

##### 扩展说明

- 所有Skill均为微服务或注入模块，支持配置启停、版本升级、热部署。
- Skill输入/输出可序列化，方便日志与追溯。
- 支持多租户/多用户并发（WebSocket、长轮询、HTTP）。
- 前后端解耦、API接口REST/GraphQL标准，便于跨端调用。

***

### 二、Skill设计方案（落地规格）

#### 1. Skill管理原则

- 每个Skill为一个**标准接口能力单元**，可独立测试、热插拔。
- 嵌套/调用关系通过**Orchestrator**调度与管理（Skill Manifest）。
- 所有Skill输入输出都以**SkillContext**为交换格式（用户信息、对话上下文、路由意图、Skill状态、日志字段）。
- Skill可声明自身需要用到的文档/上下文、模型推理参数等。

#### 2. Skill标准接口（伪代码）

```python
class Skill:
    def __init__(self, skill_id: str, config: Dict):
        self.skill_id = skill_id
        self.config = config

    def can_handle(self, user_input, context) -> bool:
        ...

    def run(self, user_input, context) -> Dict:
        # 要么返回 result，要么下发下游Skill任务
        return {"result": ..., "status": ..., "next_skill": ..., "log": ...}
```

#### 3. 主要Skill分解示例

| Skill 名称                  | 说明                              |
| :------------------------ | :------------------------------ |
| IntentDetectSkill         | 意图识别（指向闲聊/升学/路径追问等），支持 fallback |
| AdmissionSkill            | 模拟升学主线技能（信息收集、成绩输入、院校推荐）        |
| ConvergenceSkill          | 路径多元收敛技能（子路径生成、基于问答收敛）          |
| PathDrillDownSkill        | 针对某路径深挖、详情说明、补全建议（可动态插入）        |
| TerminateOrRecommendSkill | 用户“回答终止”与自动总结推荐，风险点提醒           |
| ChatSkill                 | 闲聊与情感慰问，开场、过渡                   |
| LoggingSkill              | 关键节点打点、详细日志与追溯、不影响用户体验          |
| UserProfileSkill（可选）      | 用户画像管理、习惯/偏好存储                  |
| RiskEvaluateSkill（可选）     | 对路径/决策的风险评估，返回友好提示、必要信息补全建议     |
| ActGuideSkill（可选）         | 路径落地行动指南生成                      |

> 注：每个Skill既可独立调度，也能被组合/嵌套（支持多级流转、回退）。

#### 4. Skill互相组合与扩展

- 所有Skill在 Orchestrator 的分发表（Skill Manifest/Registry）注册，统一本体发现和调用。
- 支持 Skill 的热更新和动态插拔。
- Skill可以声明依赖、返回链式执行建议（类似函数调用链路——Skill A返回`next_skill`“Skill B”，Orchestrator自动派发）。

#### 5. Skill配置和文档

- 每个Skill有自身的配置文件（YAML/JSON）声明可用性、依赖模型、需要的库、输入输出预期。
- 支持Skill文档自动解析（Skill Docstring → 富文本可编辑、前端展示）。

#### 6. Example: Skill调度流程（简化）

```plaintext
[用户输入]-> RouterSkill -> [返回Intent: 路径追问]
    -> PathDrillDownSkill
      -> (多轮交互/补全) -> [终止]-> TerminateOrRecommendSkill
    [期间所有节点都由LoggingSkill自动打点记录]
```

***

### 三、框架选型建议

- **后端**：FastAPI/Python（自带异步、易集成模型任务）或 Fastify/Node.js
- **Skill管理**：可用FastAPI路由+importlib热加载，或Celery任务+Redis排队
- **大模型服务**：huggingface/transformers搭建本地模型推理、同步云API
- **多用户&安全**：JWT认证、多Session隔离
- **部署方案**：Docker Compose或Kubernetes，方便横向扩展和私有化
- **日志与监控**：MongoDB实时存Skill阶段状态，ELK/Prometheus监控

***

### 四、方案优势

1. 支持**私有部署**、**高并发**、**Skill自管**、**模型自由选择**；
2. 每个Skill解耦，支持灵活组装/升级，流程逻辑靠LLM判别更柔性；
3. 丰富日志留痕，便于AI行为追踪和产品数据分析；
4. 易于团队扩展和二次开发（Skill增删、模型切换、UI自定义）。

***

Skill型架构下，\*\*用户全局已知信息（事实、约束、答案、选择）\*\*必须随时抽取并结构化保存，供所有Skill调用。这是支撑“跳转灵活、追问补问随时生效、上下文信息协同”的核心。以下是详细的实现与设计建议。

***

## 1.全局信息抽象层设计（Facts/Slots/Memory）

### —— Skill Context/Shared Memory

建议建立一个“会话全局上下文对象”（如`SkillContext`或`SessionMemory`），用于**每步Skill抽取到关键信息后的**归档、复用、融合。结构如下：

```python
class SkillContext:
    def __init__(self):
        self.known_facts = {}   # 结构化事实 dict (slot/value/来源/时间)
        self.user_profile = {}  # 用户静态画像
        self.skill_states = {}  # 各skill本地状态（比如路径列表/选择等）
        self.logs = []          # 日志记录

    def update_fact(self, slot, value, source_skill, timestamp=None):
        # 可加入置信度、最近更新时间
        self.known_facts[slot] = {
            "value": value,
            "source": source_skill,
            "updated_at": timestamp or now()
        }
```

#### 结构化事实示例（JSON）

```json
{
  "user_province": {"value": "广东", "source": "AdmissionSkill", "updated_at": "..."},
  "score_type": {"value": "高考", "source": "AdmissionSkill"},
  "simulated_score": {"value": 372, "source": "AdmissionSkill"},
  "subject_batch_limit": {"value": "本科线以下", "source": "AdmissionSkill"}
}
```

***

## 2.事实抽取入口（每个Skill中）

每个Skill都**必须**在获得新信息（如解析用户回答、生成推荐结果、LLM输出后结构化提取）时，调用`context.update_fact`写入核心事实。

- AdmissionSkill提分数，用`context.update_fact("simulated_score", "372", ...)`
- DrillDownSkill补充风险结果，也写入
- 用户随时补答/追答，也都增量合并到context

Skill**在处理时不得盲用上下文原始文本，而应直接查skillContext**。

***

## 3.Skill的输入输出加上Facts字段

Skill对外接口建议统一嵌带context，一切筛选、推荐、收敛逻辑均以context中的facts为判断基础。

```python
def run(self, user_input, context: SkillContext):
    known_facts = context.known_facts
    # 以下用Facts进行路径筛选、推荐、追问等
```

***

## 4.二次抽象建议（Fact类型槽位）

可以用字典、结构体或槽位（slot filling）建表，方便统一管理、标准化、提升兼容性。\
如：

- Province/考试省份
- Score/成绩
- Stream/文理科/选择科目
- AcademicLimit/本科/专科/线下
- 兴趣倾向/家庭预算

***

## 5.强结构化示例——自动抽取与存储：

```python
def extract_and_store_facts(user_input, context):
    facts = llm_extract_facts(user_input) # LLM或规则抽取
    for slot, value in facts.items():
        context.update_fact(slot, value, source_skill="XXX")
```

***

## 6.Skill间联动（场景）

- AdmissionSkill采集的省份/高考分数，ConvergenceSkill在路径规则筛选直接取用，无需“重复追问”。
- DrillDownSkill针对某路径“本科线以下适配”，直接用Facts自动判定适合度风险。
- TerminateSkill直接基于当前facts总结推荐与风险，无需集成原始聊天记录。

***

## 7.日志与追溯

每条fact写入都自动带skill、时间、来源，便于回溯及后续模型fine-tune。

***

## 8.实现建议（生产化）

- context对象建议持久化(如MongoDB/Redis)，以防断线和支持多端访问。
- 支持Fact历史追踪，冲突时可按时间或置信度加权。
- 事实结构建议经过团队梳理标准化slot表，后续可便捷解析和扩展。

***

### 总结

- **Skill间信息共享的基础是结构化“已知事实/槽位”。**
- **每个Skill发现的新事实都必须写入context，所有Skill优先查询和依赖context。**
- **事实抽取强依赖LLM-parse能力，需配合后续准确性测试。**
- **这种结构极大增强平台“路径灵活跳转/补全/追溯/多角色协同/并发容错”等能力。**

## 需具体的fact schema设计、技能间context传递实现代码，或LLM自动信息抽取prompt

我目前没有结构化的文档在./doc下，
"多元升学路径"和"模拟升学"
需要对这些文档进行skill的改造，需要写一套脚本做相关的信息的shcema例如规则和一级升学大类、路径介绍等等，需要你去看我的excel文件（docs/多元升学路径/最新人路规则（13+内蒙古）更至20260520.xlsx）来综合设计
