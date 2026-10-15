# 模型无关（Model-Agnostic）设计说明及审计核验材料

| 文档属性 | 内容 |
| --- | --- |
| 文档用途 | 向上级管理人员和审查部门说明本项目模型无关设计及核验方法 |
| 适用系统 | `hailiang-skills`、AI 业务调试台、正式 SSE V2 对话服务 |
| 核验对象 | 对话模型、专家路由模型、Skill 规划模型、Embedding 模型的接入层 |
| 结论口径 | 业务编排与模型供应商解耦；通过配置接入符合约定协议的模型服务 |
| 证据来源 | 项目源代码、模型配置文件、运行日志、健康检查和替换测试记录 |

## 一、审查结论

本项目采用模型无关（Model-Agnostic）设计。业务代码不直接依赖某一模型的权重、SDK 或供应商专用调用方式，模型服务通过配置文件和统一的 OpenAI-compatible HTTP 接口接入。

更换模型时，保持以下业务层不变：

- SSE V2 输入输出协议；
- 专家团、专家、Agent、Skill 和 `AGENT.md` / `SKILL.md` 规则；
- ContextComposer、会话记忆、孩子档案和多孩子隔离；
- Native Questionnaire、脚本沙箱、reference 引用和证据校验；
- 内容安全、审计、诊断查询和候选修订测试链路。

变更范围集中在模型配置中的 `provider`、`base_url`、`model`、鉴权环境变量及模型能力参数。系统不以模型名称判断业务规则，也不把某一模型的内部思维格式、私有 SDK 对象或私有响应字段写入 Skill 和专家业务逻辑。

这里的“模型无关”是接入和业务编排层面的解耦，不表示不同模型的回答质量、速度、上下文上限或工具调用能力完全相同。替换后的模型仍需满足本项目规定的协议和能力验收条件。

## 二、项目中的模型调用边界

```text
业务入口（SSE V2 / 业务调试台）
        ↓
MainPlannerOrchestrator / AgentScopeExpertRuntime
        ↓
LLMConfig（配置）
        ↓
统一模型客户端
  ├─ LLMClient（基础编排 Skill）
  └─ OpenAICompatibleChatClient（Runtime Skill、Agent、工具调用）
        ↓
HTTP compatible endpoint
  ├─ POST /chat/completions
  └─ POST /embeddings（可选路由 Embedding）
```

模型只负责按当前提示和工具协议返回规划、结构化动作、工具调用或自然语言文本。专家策略、Skill 约束、表单条件、脚本权限、reference 选择和输出安全审核由平台运行时负责。

## 三、可核验的代码证据

| 核验点 | 项目证据 | 审查含义 |
| --- | --- | --- |
| 配置对象 | [`llm/config.py`](../src/hailiang_skills/llm/config.py) 中的 `LLMConfig` | `provider`、`base_url`、`model`、鉴权变量、超时和生成参数均为数据字段，不是业务代码常量。 |
| 配置加载 | [`llm/config.py`](../src/hailiang_skills/llm/config.py) 的 `load_llm_config(path)` | 配置可从 JSON 文件加载；测试和运行时可以注入不同配置对象。 |
| 基础客户端 | [`llm/client.py`](../src/hailiang_skills/llm/client.py) 的 `LLMClient` | 使用配置的 `base_url`、`model` 和鉴权变量调用 `/chat/completions`，支持普通和流式请求。 |
| Runtime 客户端 | [`skill_runtime/llm_client.py`](../src/hailiang_skills/skill_runtime/llm_client.py) 的 `OpenAICompatibleChatClient` | Runtime Skill、Agent 和工具调用共用统一客户端，不直接绑定 DashScope SDK。 |
| 运行时组装 | [`runtime_bridge/main_planner.py`](../src/hailiang_skills/runtime_bridge/main_planner.py) | 编排器由 `LLMConfig` 创建 Runtime 客户端，Skill 业务逻辑不创建供应商客户端。 |
| Embedding 解耦 | [`runtime_bridge/main_planner.py`](../src/hailiang_skills/runtime_bridge/main_planner.py) | Embedding 可通过独立 `base_url`、`model` 和鉴权变量接入，不影响聊天模型接口。 |
| 服务状态 | [`api/main.py`](../src/hailiang_skills/api/main.py) 的 `/health` | 返回当前 `provider`、`model` 和 `enabled`，可证明部署实例实际采用的配置。 |
| 配置测试 | [`tests/test_runtime_bridge.py`](../tests/test_runtime_bridge.py) | 测试使用临时 JSON 配置加载模型参数，并验证配置可控制运行时 Embedding 开关。 |

## 四、当前配置证据

正式配置文件为：

```text
config/llm/qwen_dashscope.json
```

该文件只描述接入参数，例如：

```json
{
  "provider": "dashscope_compatible",
  "base_url": "https://<compatible-endpoint>/v1",
  "model": "<model-name>",
  "api_key_env": "DASHSCOPE_API_KEY",
  "timeout_s": 120,
  "temperature": 0,
  "max_tokens": 384000
}
```

密钥只通过环境变量注入，不写入 Skill、Agent、前端或普通审计日志。审计材料中应使用脱敏后的配置副本，不提交真实密钥、完整 URL 中的敏感查询参数或生产令牌。

## 五、审计人员可执行的替换核验

### 5.1 替换原则

核验应在独立测试部署或临时副本中进行，不直接修改生产实例。选用另一个能够提供本项目约定接口的模型服务，或使用内部兼容协议 Mock 服务。测试仅替换配置，不修改业务代码、Skill 包、数据库结构或 SSE V2 请求。

### 5.2 替换配置样例

将测试副本的 `config/llm/qwen_dashscope.json` 替换为以下脱敏结构，字段保持一致：

```json
{
  "provider": "audit_openai_compatible",
  "base_url": "http://127.0.0.1:9009/v1",
  "model": "audit-model",
  "api_key_env": "AUDIT_LLM_API_KEY",
  "timeout_s": 30,
  "temperature": 0,
  "max_tokens": 4096,
  "enable_thinking": false,
  "return_reasoning": false,
  "embedding": {
    "enabled": false
  }
}
```

同时在测试进程中设置非生产测试密钥：

```bash
export AUDIT_LLM_API_KEY='audit-only-placeholder'
```

API 服务入口当前默认读取项目 `config/llm/qwen_dashscope.json`；因此审计替换测试应使用独立测试副本替换该文件。Skill Runtime CLI 也支持通过 `--llm-config` 直接指定临时配置文件。两种方式均不需要修改业务代码。

### 5.3 核验步骤

1. 记录替换前代码版本、配置文件 SHA-256、Skill/Agent/Expert Team 版本和数据库 active deployment 标识。
2. 仅替换测试副本中的模型配置文件及对应测试密钥环境变量。
3. 启动服务并调用 `/health`，确认返回的 `llm.provider`、`llm.model` 已变为替换后的值，Skill 列表和运行时状态不变。
4. 使用与原模型相同的 SSE V2 输入，验证仍能返回合法的 `intent`、`assistant`、`session`、`error` 和完成状态。
5. 覆盖以下业务链路：普通专家直答、Skill 路由、Native Questionnaire、脚本调用、reference 引用、专家转交和候选修订手动测试。
6. 检查日志/trace：请求进入统一客户端，endpoint 为替换配置的 `/chat/completions`，没有出现供应商 SDK 专用调用；脚本、表单和安全审核事件仍然产生。
7. 对流式模型响应进行验证：响应使用 `data: ...` 增量块和 `[DONE]` 结束标记，平台能够正常合并 `assistant` 内容和 `intent` 状态。
8. 恢复原配置，重新记录配置 SHA-256，并确认服务健康状态恢复。

### 5.4 通过标准

| 项目 | 通过标准 |
| --- | --- |
| 配置生效 | `/health` 显示替换模型，服务启动不需要改业务代码。 |
| 协议兼容 | 普通响应、流式响应、结构化动作和工具调用符合统一客户端可解析格式。 |
| 业务链路 | 同一专家、Skill、表单、脚本和引用链路可以继续执行。 |
| 安全链路 | 输入/输出安全状态、拦截、隔离和审计字段不因模型替换而消失。 |
| 数据隔离 | `profile_id`、session、turn、候选修订和专家包关系不改变。 |
| 可回退性 | 恢复原配置后行为回到原模型，期间没有迁移或改写业务数据。 |
| 代码不变 | 替换前后业务源代码、Skill 包和数据库迁移版本一致。 |

## 六、统一协议要求

### 6.1 非流式响应

聊天模型至少返回以下 OpenAI-compatible 结构：

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "模型回复内容"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

### 6.2 流式响应

平台读取 `data:` 行中的 `choices[0].delta.content`，以 `[DONE]` 作为结束标记，并独立记录首个模型增量、首个正文增量、完成原因和 token 用量。

### 6.3 工具和结构化动作

Runtime 优先使用标准 `tools` / `tool_calls` 结构；不支持原生工具调用的兼容服务，运行时可以使用 JSON action 回退路径。工具授权、Skill 授权和脚本沙箱权限仍由平台验证，模型不能通过响应内容扩大权限。

## 七、模型替换后的边界说明

模型替换不会改变平台的业务控制权：

- 模型不能决定未锁定的 Skill 是否可调用；
- 模型不能绕过 Agent、专家团和 profile 授权；
- 模型不能决定 questionnaire 的安全字段是否可收集；
- 模型不能直接执行宿主机脚本或访问其他孩子档案；
- 模型生成的正文、表单、脚本结果和引用内容仍经过平台的证据校验和安全审核；
- 模型输出的路由、工具调用和结构化 JSON 无法解析时，平台按既定降级或可重试错误处理。

因此，替换模型只改变“模型服务实现”和可能的质量/时延表现，不改变平台的安全边界、业务授权和数据处理责任。

## 八、审计材料清单

提交给审查人员的证明材料包括：

1. 本文档及项目架构图。
2. 脱敏后的模型配置文件，展示 `provider`、`base_url`、`model` 和鉴权变量的配置关系。
3. 统一模型客户端源码位置及版本文件哈希。
4. 替换前后的 `/health` 输出，密钥和内部地址脱敏。
5. 替换测试的请求/响应摘要、SSE 事件摘要和业务链路结果。
6. 替换前后代码版本、Skill/Agent/Expert Team 版本、数据库 active deployment 标识。
7. 测试日志中的模型 endpoint、模型名、请求目的、耗时、finish reason 和错误降级信息；不提交完整 prompt、用户隐私或密钥。
8. 失败场景记录：兼容接口不可用、模型响应格式错误、工具调用不支持时，平台是否按既定错误或回退路径处理。

## 九、审计记录模板

```text
核验日期：
核验人员：
代码版本 / commit：
原配置 SHA-256：
替换配置 SHA-256：
原 provider / model：
替换 provider / model：
原模型 endpoint（脱敏）：
替换模型 endpoint（脱敏）：
数据库 active deployment：
Skill / Agent / Expert Team 版本：
健康检查结果：通过 / 不通过
非流式协议：通过 / 不通过
流式协议：通过 / 不通过
工具调用或 JSON action：通过 / 不通过
Skill 路由和表单：通过 / 不通过
脚本与 reference：通过 / 不通过
安全审核与审计：通过 / 不通过
数据隔离：通过 / 不通过
回退原配置：通过 / 不通过
结论：
备注：
```

## 十、最终证明口径

审查时可使用以下简要表述：

> 本系统的业务编排、Skill 策略、专家授权、表单、脚本沙箱、引用和安全审核均运行在平台侧。大模型通过统一配置对象和 OpenAI-compatible HTTP 客户端接入，模型名称、服务地址和鉴权变量不写入业务规则。通过在独立测试副本中仅替换模型配置，并使用相同 SSE V2 请求验证普通回复、Skill 路由、表单、脚本、引用、安全审核和审计链路均可运行，可以证明系统具备模型无关的接入设计。替换模型可能造成质量、时延和能力差异，但不会改变平台的业务授权、安全边界和数据隔离机制。
