# 系统概览

当前仓库已经补齐 3 条最小落地链路：

- `scripts/build_multiroute_assets.py`
  - 将多元升学路径 Excel 编译为 JSON 资产。
- `scripts/build_admission_assets.py`
  - 将模拟升学 Markdown 编译为 JSON 资产。
- `src/hailiang_skills/api/main.py`
  - 暴露最小 FastAPI 服务，串起 Router、Admission、Convergence、DrillDown、Terminate 五类 Skill。

运行顺序建议：

1. 先执行两个 build 脚本，生成 `assets/generated/*`。
2. 再启动 `uvicorn hailiang_skills.api.main:app --reload`。
3. 通过 `/api/v1/sessions` 与 `/api/v1/sessions/{session_id}/messages` 进行调试。

## 当前 LLM 主导节点

- `router`
  - 使用 `config/llm/qwen_dashscope.json` 中的 DashScope 兼容模型做意图识别与目标 skill 选择
  - 如果未配置 `DASHSCOPE_API_KEY`，回退到启发式规则
- `planner`
  - 在 router 之后执行，决定本轮 `target_skill`、`response_mode`、`missing_facts`
- 具体 skill
  - `admission` / `convergence` / `path_drilldown` / `terminate_or_recommend` 会先使用结构化资产筛选，再由 LLM 负责自然语言总结与追问提示

## 模型配置

- 配置文件：`config/llm/qwen_dashscope.json`
- 环境变量：`DASHSCOPE_API_KEY`
- 健康检查：`/health` 会返回当前模型名和 `enabled` 状态
