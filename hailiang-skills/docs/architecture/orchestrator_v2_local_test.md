# Orchestrator v2 本地完整流程自测

面向平台 Agent 编排（`orchestrator_v2`）的本地联调说明。App 仍走 chat SSE v2；内部由 Harness 适配层驱动：

`platform gate → orchestrate_head → 专家任务（depends_on / max_parallel）→ orchestrate_tail → recommend(jump_cards)`。

## 1. 启动前条件

- Docker Desktop 已 Running（至少要有 **Redis**；`storage_backend=file` 时也可只起 Redis）
- 已配置 `hailiang-skills/env.local.sh`（勿提交；可从同机其他 checkout 复制后改路径）
- `AGENT_SKILL_RUNTIME_CORE_PATH` 指向本仓库的 `agent_skill_runtime_core`
- 已打开灰度开关（写在 `env.local.sh`）：

```bash
export HAILIANG_ORCHESTRATOR_V2="1"
```

仅起 Redis（file 存储时常用）：

```bash
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
docker compose -f docker-compose.local.yml up -d redis
```

## 2. 一键启动

```bash
cd hailiang-skills
# 首次（postgres 模式）：./run.sh --bootstrap
# file 存储 + Redis 已起好时：
./run.sh --no-infra
```

默认地址（端口被占用时脚本会自动换备用端口，以终端输出为准）：

| 服务 | 地址 |
|------|------|
| 前端 | http://127.0.0.1:4175 |
| 后端 | http://127.0.0.1:8010 |
| 健康检查 | `GET /health/ready` → `redis: true`，且环境已设 `HAILIANG_ORCHESTRATOR_V2=1` |

日志：`backend.local.log`。Ctrl+C 停本次前后端；Redis 容器可保留。

## 3. 确认编排开关已生效

**必须新建会话**。旧会话不会自动带上 `orchestrator_v2`。

前端「本地调试身份」面板重新生成 `session_id` 后开聊。

可用接口抽查（按实际端口改）：

```bash
# 健康
curl -fsS "http://127.0.0.1:8010/health/ready" | python3 -m json.tool

# 创建/恢复会话后，看 session_meta.orchestrator_v2 是否为 true
# （具体 path 以当前 API 为准；Workbench/调试面板也可看会话元数据）
```

环境变量未开时，新会话 `orchestrator_v2=false`，走旧路径。

## 4. 推荐冒烟路径

在前端用**专家团**入口（如「学生成长专家团」），不要只开单专家。

### 场景 A：身份门禁（同 run 不出模型）

1. 新会话、画像未填身份/学段
2. 发送任意问题（如「升学怎么规划」）
3. **期望**
   - SSE `state.form` 含 `user_role`、`school_stage`（`form_id` 多为 `platform_identity_form`）
   - `state.round_experts.status` 为 `idle`
   - `jump_cards` 为空
   - 助手提示先补身份/学段，**不应**出现专家长文答复

### 场景 B：单专家直出

1. 补全身份=家长、学段=高中
2. 问：「高中升学路径怎么规划？」
3. **期望**
   - `round_experts.assigned_expert_ids` / `completed_expert_ids` 含升学相关专家
   - `status` 最终为 `completed`
   - `assistant.content` 为专家答复（经编排尾底线后直出，不改写）
   - `expert_context.expert_id` 有值

### 场景 C：多专家 / 串并行

1. 提问同时覆盖升学 + 家庭教育（或带「双专家」语义，视 head 规则）
2. **期望**
   - `assigned_expert_ids` ≥ 2
   - 过程中可见 `running` → `completed`
   - 多专家成功时内容经编排尾合并
   - 若 team `max_parallel=1` 或子任务 `depends_on` 有边，则后一个专家在前一个完成后再跑

### 场景 D：澄清 / 拒答

1. 信息极模糊 → 可能 `clarify`（澄清问句、无专家 assigned）
2. 明显超范围（如炒股）→ 可能 `reject_out_of_scope`

### 场景 E：jump_cards

1. 对话落到「可做测评/看课程」等意图
2. **期望** `state.jump_cards` 最多 1 条，且枚举在 recommend catalog 内

## 5. 如何看 SSE 字段

浏览器 DevTools → Network → `chat/stream`（或等价 SSE）→ EventStream / Preview。

关注最终 `state` 快照中的：

- `form` / `message_blocks`（fact_form）
- `round_experts`：`assigned_expert_ids`、`running_expert_ids`、`completed_expert_ids`、`failed_expert_ids`、`status`
- `jump_cards`
- `expert_context`
- `assistant.content`

本地也可用：

```bash
PYTHONPATH=src .venv/bin/python scripts/show_sse_trace.py --help
```

## 6. 自动化回归（不启前端）

```bash
cd hailiang-skills
PYTHONPATH=src .venv/bin/pytest -q \
  tests/test_orchestrator_e2e.py \
  tests/test_harness_chat_adapter.py \
  tests/test_orchestration_head.py \
  tests/test_platform_gate.py \
  tests/test_plan_executor.py \
  tests/test_jump_cards.py \
  tests/test_orchestration_config.py
```

## 7. 常见问题

| 现象 | 处理 |
|------|------|
| 行为与旧版一样 | 确认 `HAILIANG_ORCHESTRATOR_V2=1` 且**新建**了 session |
| `/chat/stream` 409 HARNESS_ENDPOINT_REQUIRED | 会话是 harness 但未打上 `orchestrator_v2`；重建会话或检查 middleware |
| Docker 起不来 | 打开 Docker Desktop → Running 后再 `./run.sh` |
| 无 LLM 密钥 | `env.local.sh` 填 `DASHSCOPE_API_KEY` |
| 旧会话无表单/无 round_experts | 预期：开关只冻结在创建时 |

## 8. 已知未完成（不影响主路径联调）

- Workbench 尚未把三个 system skill 做成独立发布对象（运行时可读种子/`runtime_system_skills`）
- Workbench 暂无 `collaboration_rules` 编辑控件（字段已在 bundle/快照）
- 真模型质量依赖 head/tail/recommend 提示词与线上数据，需人工评一轮

## 9. 关闭灰度

`env.local.sh` 中设 `HAILIANG_ORCHESTRATOR_V2=0`（或删掉），重启 `./run.sh`，再建新会话即可回到旧路径。
