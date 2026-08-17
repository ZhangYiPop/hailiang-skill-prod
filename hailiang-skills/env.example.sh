#!/usr/bin/env bash
# 私有环境变量模板：复制为 env.local.sh 后填写，切勿提交真实密钥。

# 必填：模型服务密钥。
export DASHSCOPE_API_KEY=""
export HAILIANG_EXTERNAL_API_KEY=""

# 必填：生产模式审计加密密钥。生成方式：
# .venv/bin/python -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
export HAILIANG_AUDIT_ENCRYPTION_KEY=""
export HAILIANG_AUDIT_KEY_ID="primary-2026-07"

# 算法服务由项目转发后端在私网内调用。它不验证浏览器 Token；调用方必须
# 在网络层限制为可信 BFF / 网关，并在创建或恢复会话时传 user_id、profile_id、session_id。

# 共享 Skill Runtime Core 的绝对路径。Mac 与服务器通常不同。
export AGENT_SKILL_RUNTIME_CORE_PATH="/absolute/path/to/agent_skill_runtime_core"

# 本地开发可将 main_planner 的纯本地 profile_op/output_sanitizer 脚本
# 从沙箱执行切换为快速子进程执行，减少首次思考阶段等待；未知脚本仍走沙箱。
# 生产环境如不需要可设为 false。
export HAILIANG_MS_AGENT_LOCAL_FAST_PATH="true"

# 默认由 MS-Agent 同一份 JSON 同时完成规划、工具路由和正文生成。
# 如需人工回退到独立工具分类模型，可设为 standalone 并重启服务。
export HAILIANG_TOOL_ROUTING_MODE="ms_agent"
# 合并 JSON 缺失或非法时是否仅在异常轮次调用旧分类器兜底。
export HAILIANG_TOOL_ROUTING_FALLBACK_ON_INVALID="true"

# Reasoning labels are emitted on a small cadence while the model is running.
# The minimum duration only affects very fast replies and can be disabled for
# latency-sensitive environments.
export HAILIANG_PROGRESS_SIMULATION_ENABLED="true"
export HAILIANG_PROGRESS_SIMULATION_INTERVAL_S="0.45"
export HAILIANG_PROGRESS_SIMULATION_JITTER_S="0.25"
export HAILIANG_PROGRESS_SIMULATION_MIN_DURATION_S="1.2"

# 内容安全服务；未配置时只使用本地规则降级检查。
export ALIBABA_CLOUD_ACCESS_KEY_ID=""
export ALIBABA_CLOUD_ACCESS_KEY_SECRET=""
export HAILIANG_SECURITY_QUARANTINE_KEY=""
export HAILIANG_SECURITY_ADMIN_TOKEN=""

# 本地默认连接。若 Linux/CentOS 上由 deploy-all.sh 自动拉起 Docker 中的
# PostgreSQL/Redis，请继续保持 127.0.0.1；脚本会按实际映射端口自动改写。
# 仅当你使用外部托管 PostgreSQL/Redis 时，才改成真实远程地址，并设置
# START_INFRA=0。
export HAILIANG_STORAGE_BACKEND="postgres"
export HAILIANG_DATABASE_URL="postgresql+psycopg://hailiang:hailiang@127.0.0.1:5432/hailiang_skills"
export HAILIANG_REDIS_URL="redis://127.0.0.1:6379/0"
export START_INFRA="1"

# 仅服务器部署时通常需要覆盖。
export BACKEND_PORT="8010"
export FRONTEND_PORT="4175"
export PUBLIC_API_BASE_URL=""
export DEFAULT_USER_ID="debug-user"
export HAILIANG_CORS_ORIGINS="http://127.0.0.1:4175,http://localhost:4175"
