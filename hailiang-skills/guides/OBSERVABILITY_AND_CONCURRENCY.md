# 全链路可观测性与并发运行手册

## 已落地的请求链路

每个 HTTP 请求会接受或生成 `X-Request-Id`，并在响应中返回。W3C
`traceparent` 由 OpenTelemetry SDK 继续传播。应用事件、Runtime JSONL 和
SSE worker 继承同一个 `TelemetryContext`，其中包含 `request_id`、
`trace_id`、`span_id`、`session_id`、`profile_id`、`user_id` 与路由。

每个 Span/Event 使用 UTC `start_at` / `end_at` 与单调时钟
`duration_ms`。当前已覆盖：HTTP、Facts hydrate/persist、回合锁、编排器、
LLM 请求/流、会话读取/保存、流后处理和 PostgreSQL 的核心读写。异常会以
节点名和异常类型计数，正文不写入普通日志。

Prometheus 指标在 `/metrics` 提供，且仅使用低基数标签：

- `hailiang_request_duration_seconds{route,method,status_code}`
- `hailiang_node_duration_seconds{node,outcome,skill_id}`
- `hailiang_sse_active_connections`
- `hailiang_sse_time_to_first_token_seconds{skill_id}`
- `hailiang_errors_total{node,error_type}`

`route` 在指标中使用 FastAPI 的路由模板，而不是带 `session_id` 的实际 URL。
按 `request_id` 或 `trace_id` 可从 API 日志、运行时日志和会话事件还原一次
调用；按 `session_id` 可定位会话的持久化状态和事件索引。

## 生产存储与保密

生产必须设置：

```bash
export HAILIANG_STORAGE_BACKEND=postgres
export HAILIANG_DATABASE_URL='postgresql+psycopg://...'
export HAILIANG_REDIS_URL='redis://.../0'
export HAILIANG_AUDIT_ENCRYPTION_KEY='<32-byte URL-safe base64 AES-GCM key>'
export OTEL_EXPORTER_OTLP_ENDPOINT='http://otel-collector:4318'
```

PostgreSQL 是 `advisor_sessions`、三层 Facts、Profiles、会话事件索引和
`advisor_audit_payloads` 的真源。会话保存带 `version` 乐观锁；冲突返回
`409`，客户端重新拉取 context 后重试安全操作。文件快照/JSONL 仅在
`HAILIANG_STORAGE_BACKEND=file` 的本地开发模式使用。

模型请求和模型响应的完整正文进入 `advisor_audit_payloads`，由 AES-GCM
加密；普通事件和 Runtime 日志只保留 SHA-256、长度和审计 ID。审计表默认
90 天过期。用每日 CronJob/worker 执行：

```bash
PYTHONPATH=src .venv/bin/python scripts/purge_expired_audit.py
```

审计解密不应暴露为业务 API。数据库审计角色和密钥轮换由部署平台管理；密钥、
Authorization、Cookie 和完整 Prompt 不能加入 Trace 属性或 Grafana/Loki。

## 100 路 SSE 的控制面

Redis 持有每个 `session_id` 的 generation token；新回合覆盖 token，旧流在
输出和持久化边界都会停止，因此不能覆盖新回合。每个实例还启用有界 SSE
worker、100 路全局活动流、每用户 3 路和 5 秒排队超时。饱和时返回 `429` 与
`Retry-After: 5`，队列与 SSE 事件缓存不会无限增长。浏览器断开会使生成器
取消当前 generation，后台 worker 随后停止输出/写入。

建议的初始部署值：2–4 Uvicorn worker、每实例 `HAILIANG_STREAM_WORKERS=100`，
并根据模型网关实际并发额度下调总 SSE 上限。模型、Redis、PostgreSQL故障均会
在 readiness、错误计数和对应 Span 中显现；发布前必须为模型网关配置连接/读取
超时与容量隔离。

## 健康检查、Collector 与告警

- `/health/live`：进程存活。
- `/health/ready`：生产模式同时校验 PostgreSQL、Redis 和审计加密密钥。
- `/metrics`：Prometheus 格式。

`docker-compose.yml` 启动 PostgreSQL、Redis、OTel Collector，并在启动 API 前
执行 `alembic upgrade head`。`deploy/otel-collector.yml` 是一个最小 Collector；
生产环境应将 trace exporter 指向 Tempo/托管追踪，将 logs 指向 Loki，并由
Prometheus 抓取 Collector 的 `:9464`。

至少配置以下告警：5 分钟 5xx/模型错误率、P95/P99 请求与 LLM 时长、TTFT、
活动 SSE 接近 100、回合/数据库锁等待、Redis/PostgreSQL 失败和审计写入失败。
看板至少按 `route`、`skill_id`、`provider` 展示请求瀑布、LLM、SSE、数据库和
错误容量。

## 压测与验收

先用 mock LLM 执行 100 路持续 SSE，再覆盖同会话抢占、慢响应、断连、模型错误
与 Redis/PostgreSQL 短暂失败。验收条件：无跨会话写入，旧 generation 不覆盖新
状态；每个请求可通过 `request_id/trace_id/session_id` 还原；P95/P99、TTFT、
错误率、连接数可在指标系统查询；历史会话、Facts、表单、反馈和 Skill 转场在
PostgreSQL 重启恢复后仍一致。
