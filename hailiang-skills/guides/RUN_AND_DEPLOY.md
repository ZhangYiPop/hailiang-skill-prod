# 本地测试与服务器部署

这份项目现在有两条固定入口，请不要再手工串联多条启动命令。

## 第一次本地配置（Mac）

```bash
cd hailiang-skills
cp env.example.sh env.local.sh
chmod 600 env.local.sh
```

编辑 `env.local.sh`，至少填写：`DASHSCOPE_API_KEY`、
`HAILIANG_AUDIT_ENCRYPTION_KEY` 和 `AGENT_SKILL_RUNTIME_CORE_PATH`。
再运行：

```bash
./run.sh --bootstrap
```

它会安装 Python/前端依赖，启动 Docker 中的 PostgreSQL、Redis、OTel
Collector，执行 Alembic 迁移，后台启动后端，最后以前台方式启动 Vite 前端。
浏览器访问 `http://127.0.0.1:4175`。按 `Ctrl+C` 停止本次前后端进程；数据库
容器保留，数据不会丢失。

## 本地调试身份与转发服务

算法服务不提供登录接口，也不要求 `Authorization`。本地前端打开后，在“本地调试身份”
模式只需填写用户 ID、孩子姓名、学年和年级，页面会在点击进入时自动生成 `profile_id` 与
`session_id`；首次流式请求按单一接口契约创建会话，后续请求恢复它。需要复现指定三元组时，
可切换到“转发请求数据”模式手动填写 `profile_id/session_id`。

生产环境由项目后端/BFF 完成真实用户鉴权、档案归属校验，并将 `session_id`、`run_id`、JSON 字符串
`input` 与 `context_data` 一起转发到算法服务。算法服务必须仅部署在 BFF/网关可访问的私网，
不可直接暴露给浏览器或公网。

如果本机端口已被其它服务占用，脚本会复用本项目 Docker 容器的端口，并为后端
自动选择 `8010–8013`、为前端选择 `4175–4177` 中的可用端口；终端会打印最终地址。

日常启动只需：

```bash
./run.sh
```

## 从旧 logs/ 迁移历史数据

`logs/` 是旧的文件存储；数据库迁移只创建表，不会自动导入这些历史数据。首次
切到 PostgreSQL 时先备份 `logs/`，然后执行：

```bash
./run.sh --migrate-file-logs
```

脚本默认跳过数据库已有的会话，因此可重复执行。只有 `events.jsonl`、没有
`snapshot.json` 的旧会话无法恢复完整聊天记录，但事件仍会导入审计索引。

## 服务器部署

服务器也需放置私有的 `env.local.sh`（权限建议 `chmod 600`）。

- 如果服务器允许 Docker，`deploy-all.sh` 现在会自动启动本地
  `postgres`、`redis` 和 `otel-collector` 容器；此时建议继续保持：
  - `HAILIANG_DATABASE_URL=postgresql+psycopg://...@127.0.0.1:5432/...`
  - `HAILIANG_REDIS_URL=redis://127.0.0.1:6379/0`
  - `START_INFRA=1`
- 如果你使用外部托管 PostgreSQL/Redis：
  - 将地址改成真实远程地址
  - 设置 `START_INFRA=0`

然后：

```bash
./deploy-all.sh
```

部署脚本会重建虚拟环境、安装依赖；当 `START_INFRA=1` 且使用 `postgres`
存储时，会先尝试通过 Docker 自动拉起本地 PostgreSQL、Redis 和 OTel
Collector，再检查 PostgreSQL 和 Redis、执行 Alembic、构建前端并以两个
Uvicorn worker 启动。成功后检查：

```bash
curl http://127.0.0.1:8011/health/ready
curl http://127.0.0.1:8011/metrics
```

`/health/live` 仅检查进程存活；`/health/ready` 还检查 PostgreSQL、Redis 和
审计加密配置，因此部署探针应使用 `/health/ready`。`deploy-all.sh` 还会额外请求
`/health` 来校验 `runtime.entry_skill` 与运行时技能列表，避免把 readiness
响应误当成 runtime 健康响应。

## 查看日志与单请求耗时

本地运行时：

```bash
tail -f backend.local.log
docker compose logs -f postgres redis otel-collector
find logs/telemetry -maxdepth 1 -type f -print
```

每次请求的响应头包含 `X-Request-Id`。拿到 `trace_id`、`request_id` 或
`session_id` 后，可以查看该请求的节点生命周期：

```bash
PYTHONPATH=src .venv/bin/python scripts/show_request_trace.py <trace_id或request_id或session_id>
```

输出中的 `node`、`start_at`、`duration_ms`、`outcome` 就是节点耗时。
`/metrics` 是聚合统计，不用于查看单次请求；单次请求使用上述 Span 文件或
OTel Collector/Tempo。

## 重要安全规则

- `env.local.sh` 不提交 Git；`env.example.sh` 只作模板。
- 模型密钥、云服务密钥和已暴露过的密钥应立即在对应平台轮换。
- 生产必须使用 `HAILIANG_STORAGE_BACKEND=postgres`；`file` 模式只适合临时调试。
- 不要删除 `logs/`，直到 PostgreSQL 数据迁移与抽样核对完成。
