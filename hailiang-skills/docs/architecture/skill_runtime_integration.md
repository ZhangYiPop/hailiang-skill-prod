# Skill Runtime 融合架构与新增场景接入手册

## 1. 当前统一后的目录约定

当前对外 Skill ID 以 `career_plan_entity` 为准。历史目录名
`runtime_skills/main_planner/` 和代码模块 `runtime_bridge/main_planner.py` 暂时保留，
仅用于兼容已有文件路径；加载后的 bundle、路由候选、工具栏目录和 SSE 新字段均使用
`career_plan_entity`。新会话先进入 `general_chat`，用户点击建议卡或工具栏按钮后才
进入升学规划顾问。

本项目现在不再保留独立顶层 `skill_runtime` 包，也不再保留第二套 `data/generated` 资产目录。

统一后的目录如下：

- `src/hailiang_skills/skill_runtime/`
  - 原 `skill-runtime` 的运行时机制，现在收敛到 `hailiang_skills` 命名空间内。
  - 负责 `SKILL.md` / `runtime_contract.json` 加载、runtime session state、route router、tool specs、asset lookup、status_track 等通用能力。

- `src/hailiang_skills/runtime_bridge/`
  - hailiang API 与 runtime 机制的适配层。
  - 入口类为 `MainPlannerOrchestrator`。
  - 负责把 FastAPI 会话、三层 facts、旧业务 Skill、runtime 原生 Skill 串成统一链路。

- `runtime_skills/`
  - 所有 runtime 可发现的场景 Skill 目录。
  - 包含 runtime 原生 Skill：
    - `e生涯升学顾问v017` -> `career_plan_entity`
    - `e生涯前景探路v001` -> `future_explore`
    - `e生涯提分规划v001` -> `score_improve`
    - `e生涯兴趣探索v001` -> `interest_explore`
    - `e生涯选科参谋v001` -> `subject_advisor`
  - 也包含 hailiang 旧业务的 runtime bridge Skill：
    - `mock_admission` -> 桥接旧 `AdmissionSkill`
    - `multi_path_planning` -> 桥接旧 `ConvergenceSkill`
  - 每个 skill 目录现在统一支持：
    - `SKILL.md`
    - `runtime_contract.json`
    - `references/`：知识文档，可参与按需检索
    - `assets/`：skill 本地轻量 assets / 索引 / 说明资源
    - `scripts/`：status_track / profile_op 等辅助脚本

- `assets/generated/`
  - 唯一的运行期结构化资产根目录。
  - 原 hailiang 资产仍在这里：
    - `admission/`
    - `multiroute/`
    - `school_intro/`
  - 原 runtime 的 registry 也统一迁到这里：
    - `asset_registry.json`
    - `tool_registry.json`

## 2. 当前对话链路如何嫁接

FastAPI 启动入口：

```python
hailiang_skills.api.main:create_app()
```

创建 app 时会注册 hailiang 旧业务 Skill：

- `RouterSkill`
- `FactsExtractorSkill`
- `PlannerSkill`
- `AdmissionSkill`
- `ConvergenceSkill`
- `PathDrillDownSkill`
- `SchoolIntroSkill`
- `TerminateOrRecommendSkill`
- `ChatSkill`

但真正对话入口已经切到：

```python
MainPlannerOrchestrator
```

每轮用户消息进入后，主链路是：

```text
用户消息
  -> MainPlannerOrchestrator.handle_message()
  -> 构造/恢复 skill_runtime SessionState
  -> 同步 hailiang effective facts 到 runtime global_facts
  -> main_planner 根据 runtime_contract 路由
  -> 如果命中 runtime 原生 Skill：
       future_explore / score_improve / interest_explore / subject_advisor
       直接走 skill_runtime 的模型回复与 status_track
  -> 如果命中 hailiang bridge Skill：
       mock_admission -> AdmissionSkill
       multi_path_planning -> ConvergenceSkill
       继续复用 hailiang 旧规则、旧资产、旧回复结构
  -> runtime 产生的新 global_facts 同步回 hailiang 三层 facts
  -> 保存 session snapshot
```

### 2.1 为什么 `active_skill` 仍会显示 admission / convergence

这是正常的。

`career_plan_entity` 是可进入的升学规划顾问和子场景路由器；新会话由 `general_chat` 先接待，
用户确认后才进入它。当它路由到 hailiang 旧业务时，真正执行业务的是旧 Skill：

```text
main_planner -> mock_admission -> admission
main_planner -> multi_path_planning -> convergence
```

因此前端应同时看：

- `active_skill`
  - 当前实际执行 Skill。
- `main_planner_state.target_skill`
  - runtime main_planner 的路由目标。

### 2.2 流式输出与推理进度协议

前端的 SSE 协议保持不变：

```text
run_started
skill_status
final_text_delta
message_block
final_message
run_completed
```

为了让 runtime 原生 Skill 和旧 hailiang 业务在前端表现一致，`MainPlannerOrchestrator` 统一发出旧版三段推理进度：

```text
intent  -> 意图判断
planner -> 推理规划
final   -> 正在总结输出
```

两类 Skill 的流式策略不同，但前端协议一致：

- hailiang bridge Skill：
  - 例如 `mock_admission -> admission`、`multi_path_planning -> convergence`
  - 旧业务里的 `llm_compose_reply()` / `llm_polish_structured_reply()` 会直接使用 `reply_delta_callback` 输出模型 token/chunk。

- runtime 原生 Skill：
  - 例如 `future_explore`、`score_improve`、`interest_explore`、`subject_advisor`
  - 当前 skill-runtime 的工具循环是非 streaming 的完整回复模式。
  - bridge 会在 runtime 回复完成后，把最终文本切块推送为 `final_text_delta`。
  - 这样前端“推理进度”与正文增量展示保持一致；如果后续 skill-runtime client 支持原生 streaming，只需要替换 bridge 内部实现，不需要改前端协议。

相关代码位置：

- `src/hailiang_skills/core/streaming_runner.py`
  - 负责把 `status_callback` 转成 `skill_status`
  - 把 `reply_delta_callback` 转成 `final_text_delta`
- `src/hailiang_skills/runtime_bridge/main_planner.py`
  - 负责统一发出 `intent/planner/final` 三段状态
  - 对 runtime 原生 Skill 做最终回复切块

## 3. Facts 生命周期如何统一

hailiang 原有 facts 有三层：

- `shared_facts`
- `profile_facts`
- `session_facts`

有效上下文为：

```text
effective_facts = shared_facts + profile_facts + session_facts
```

runtime 内部使用：

```text
SessionState.global_facts
SessionState.skill_facts
SessionState.stage_facts
```

桥接规则在 `src/hailiang_skills/runtime_bridge/facts.py`：

- 进入 runtime 前：
  - `context.known_facts` -> `state.global_facts`
- runtime 原生 Skill 执行后：
  - `state.global_facts` -> `context.update_fact(...)`
- hailiang 旧业务 Skill 执行后：
  - `context.known_facts` 再同步回 `state.global_facts`

因此无论用户先走 runtime 原生 Skill，还是先走模拟升学/多元路径规划，facts 都会在同一会话中持续共享。

## 4. 资产如何统一

唯一运行期资产根目录：

```text
assets/generated/
```

hailiang 旧业务读取资产时仍通过：

```python
hailiang_skills.skills.assets.load_json("assets/generated/...")
```

runtime 读取资产时通过：

```python
hailiang_skills.skill_runtime.data_loader.default_generated_data_dir()
```

现在它返回：

```text
<project_root>/assets/generated
```

因此两边共用同一套结构化资产。

如果新增资产：

1. 优先放入 `assets/generated/<domain>/`
2. 更新或新增该 domain 的 `asset_manifest.json`
3. 如果希望 runtime asset lookup 能识别该资产域，更新 `assets/generated/asset_registry.json`
4. 如果该资产需要 tool 能力，更新 `assets/generated/tool_registry.json`

不要再创建 `data/generated`。

同时建议采用“两层资产”：

1. `runtime_skills/<skill>/assets/`
   - 轻量、skill 私有、便于随 skill 一起版本化的资源
   - 例如说明文档、映射索引、prompt 示例、小型参考表
2. `assets/generated/<domain>/`
   - 共享结构化大资产
   - 例如 admission / multiroute / school_intro 的 JSON 资产

`SKILL.md` 中的 `assets.generated_domains` 用来声明当前 skill 依赖哪些共享资产域。

## 4.1 Skill 标准与渐进式 Prompt 加载

当前项目采用融合式 skill 标准：

- `SKILL.md`
  - 主流 Agent Skills 风格的 metadata 入口
  - 包含 `skill_type`、`entrypoint_role`、`prompt_loading`、`retrieval`、`assets`、`debug`
- `runtime_contract.json`
  - stage / facts / routes / accepts_scenes 的运行时契约

prompt 装载分三层：

- `core`
  - skill metadata、技能正文、会话状态、tool policy、route context
- `retrieval`
  - reference catalog、本地 assets catalog、命中的 reference / asset snippets
- `final`
  - 真正发送给模型的最终 prompt

其中 runtime 原生 skill 默认使用 `progressive` 策略：

- `references/` 不再默认整包进入 system prompt
- 优先只暴露 catalog / summary
- 标准 `native + progressive` Skill 默认只把 ms-agent 本轮 lazy load 命中的 references 注入 `retrieval` 层
- Hailiang 自研 retrieval 只作为补充召回；需要处理更复杂、更长的 md 或 assets 时，再显式配置 `retrieval.supplemental_enabled: true`

前端和 `events.jsonl` 中的 `prompt_assembly` 事件也同步记录：

- `layer`
- `reference_strategy`
- `retrieved_sources`
- `retrieved_count`
- `generated_asset_domains`
- `local_asset_paths`

## 5. 新增 runtime 原生场景 Skill

适合场景：

- 新业务主要由 `SKILL.md` + references + status_track 管理。
- 需要 runtime 的通用状态协议、tool 调用、RAG、本地资产约束。
- 不依赖大量旧 hailiang 规则代码。

接入步骤：

1. 新建目录：

```text
runtime_skills/<your_skill_name>/
  SKILL.md
  runtime_contract.json
  references/            # 可选
  scripts/status_track.py # 可选
```

2. 在 `runtime_contract.json` 中声明：

```json
{
  "skill_id": "your_skill_id",
  "skill_role": "child",
  "stages": [
    {"id": "init", "kind": "entry", "required_facts": []},
    {"id": "collect", "kind": "collection", "required_facts": ["grade"]},
    {"id": "analyze", "kind": "analysis", "required_facts": []}
  ],
  "facts": {
    "global": ["grade", "score_level", "talent"],
    "skill": ["scene_goal", "conclusion_summary"],
    "stage": {
      "collect": ["scene_goal"]
    },
    "exports": {
      "promote_to_global": [],
      "share_with_parent_skill": ["conclusion_summary"]
    }
  },
  "routes": [],
  "accepts_scenes": ["你的场景名"],
  "metadata": {
    "module_group": "child",
    "scene_name": "你的场景名"
  }
}
```

3. 在 `runtime_skills/e生涯升学顾问v017/runtime_contract.json` 的 `routes` 增加入口路由：

```json
{
  "scene": "你的场景名",
  "target_skill_id": "your_skill_id",
  "required_global_facts": [],
  "required_skill_facts": []
}
```

4. 在 `src/hailiang_skills/runtime_bridge/main_planner.py` 的 `SCENE_HINTS` 增加关键词：

```python
("你的场景名", ("关键词1", "关键词2"))
```

5. 如有新事实字段：

- 如果 hailiang 侧也要读写，加入 `config/facts_schema.yml`
- runtime 侧则同步更新对应 `runtime_contract.json` 的 facts schema

6. 运行验证：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest tests.test_runtime_bridge -v
curl http://127.0.0.1:8010/health | python -m json.tool
```

7. 流式与前端检查：

- 新 Skill 不需要直接认识前端 SSE。
- 只要它通过 runtime 原生路径返回最终文本，bridge 会自动推送：
  - `skill_status: intent / planner / final`
  - `final_text_delta`
  - `final_message`
- 如果新 Skill 自带 `scripts/status_track.py`，它回写的：
  - `global_facts_patch`
  - `skill_facts_patch`
  - `stage_facts_patch`
  - `status_flags_patch`
  - `route_signal`
  会自动进入 runtime `SessionState`，随后通过 facts bridge 同步到 hailiang `context.known_facts`。

## 6. 新增基于旧 hailiang 规则的场景 Skill

适合场景：

- 新场景依赖大量结构化规则、排序、推荐、资产筛选代码。
- 更像现有 `AdmissionSkill` / `ConvergenceSkill`。
- 需要保留 `SkillResult`、`candidate_paths`、`message_blocks` 等 hailiang 前端协议。

接入步骤：

1. 新增 hailiang 业务 Skill：

```text
src/hailiang_skills/skills/your_rule_skill.py
```

实现：

```python
from hailiang_skills.skills.base import BaseSkill, SkillResult


class YourRuleSkill(BaseSkill):
    skill_name = "your_rule_skill"

    def __init__(self, llm_client=None) -> None:
        self.llm_client = llm_client

    def run(self, user_input: str, context) -> SkillResult:
        facts = {key: record.value for key, record in context.known_facts.facts.items()}
        # 读取 assets/generated 下的统一资产
        # 输出 SkillResult
        return SkillResult(assistant_message="...")
```

2. 在 `src/hailiang_skills/api/main.py` 注册：

```python
from hailiang_skills.skills.your_rule_skill import YourRuleSkill

...
YourRuleSkill(llm_client),
```

3. 新增 runtime bridge Skill 目录：

```text
runtime_skills/your_rule_scene/
  SKILL.md
  runtime_contract.json
```

`runtime_contract.json` 示例：

```json
{
  "skill_id": "your_rule_scene",
  "skill_role": "child",
  "stages": [
    {"id": "init", "kind": "entry", "required_facts": []},
    {"id": "analyze", "kind": "analysis", "required_facts": []}
  ],
  "facts": {
    "global": ["student_province", "subject_group", "score_total"],
    "skill": ["scene_goal", "conclusion_summary"],
    "stage": {},
    "exports": {
      "promote_to_global": [],
      "share_with_parent_skill": ["conclusion_summary"]
    }
  },
  "routes": [],
  "accepts_scenes": ["你的规则场景名"],
  "metadata": {
    "module_group": "hailiang",
    "scene_name": "你的规则场景名",
    "hailiang_skill": "your_rule_skill"
  }
}
```

4. 在 `src/hailiang_skills/runtime_bridge/main_planner.py` 的 `HAILIANG_TARGETS` 增加映射：

```python
"your_rule_scene": {
    "skill": "your_rule_skill",
    "scenario": "your_scenario_id",
    "scene": "你的规则场景名",
    "phase": "analyze",
},
```

5. 在 main_planner 的 runtime contract 增加 route。

6. 如果该场景需要旧 `ScenarioEngine` 的 phase 展示，则更新：

```text
config/scenarios.yml
```

7. 如果该场景有新资产：

- 放入 `assets/generated/<domain>/`
- 在 `asset_registry.json` 登记 domain
- 业务 Skill 统一从 `assets/generated/...` 读取

## 7. 注册与匹配的统一原则

以后无论新增哪类场景，都按下面四层检查：

1. runtime 是否能发现：
   - `runtime_skills/<scene>/SKILL.md`
   - `runtime_skills/<scene>/runtime_contract.json`

2. main_planner 是否能路由：
   - `runtime_skills/e生涯升学顾问v017/runtime_contract.json` 的 `routes`
   - `main_planner.py` 的 `SCENE_HINTS`

3. 如果是 hailiang 规则型场景，bridge 是否能桥接：
   - `HAILIANG_TARGETS`
   - `api/main.py` 注册具体 `BaseSkill`

4. facts 和资产是否统一：
   - facts 字段进入 `config/facts_schema.yml`
   - 资产进入 `assets/generated`
   - runtime asset lookup 需要更新 `assets/generated/asset_registry.json`

5. 前端与流式是否统一：
   - `/api/v1/sessions/chat/stream` 返回递增的 `event: state` 快照，最后追加 `event: done`
   - runtime 状态会累加为 `intent.steps`，正文累加为 `assistant.content`
   - `session.active_skill` 与 `skill_transition` 能看到 runtime 路由目标
   - hailiang bridge Skill 的 `active_skill` 仍是实际业务 Skill，例如 `admission` / `convergence`

## 8. 新场景接入最小验收清单

新增任意场景后，至少完成以下检查：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest tests.test_runtime_bridge -v
curl http://127.0.0.1:8010/health | python -m json.tool
```

如果是 runtime 原生 Skill：

- `/health.runtime.skills` 中出现新 `skill_id`
- `main_planner_state.target_skill` 能路由到新 `skill_id`
- 前端推理进度显示：
  - `意图判断`
  - `推理规划`
  - `正在总结输出`
- 回复正文以 `final_text_delta` 形式出现

如果是 hailiang 规则型 Skill：

- `main_planner_state.target_skill` 是 bridge skill id
- `active_skill` 是实际业务 skill name
- 返回中保留业务结构：
  - `candidate_paths_brief`
  - `message_blocks`
  - `facts_updated`
  - 对应 skill state

## 9. 快速自检命令

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m unittest tests.test_runtime_bridge -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python - <<'PY'
from hailiang_skills.api.main import create_app
app = create_app()
print(app.title)
PY
curl http://127.0.0.1:8010/health | python -m json.tool
```

期望 `/health` 中包含：

```json
{
  "runtime": {
    "entry_skill": "general_chat",
    "skills": [
      "future_explore",
      "interest_explore",
      "main_planner",
      "mock_admission",
      "multi_path_planning",
      "score_improve",
      "subject_advisor"
    ]
  }
}
```
