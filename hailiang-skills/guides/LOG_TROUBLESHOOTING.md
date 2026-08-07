# 日志与故障排查手册

先判断问题属于哪一类：服务没启动、依赖没连上、模型限流、单次会话异常，还是发布后异常。不要先重启或删除日志。

## 最常用的四条命令

```bash
# 看正式服务是否正在运行
sudo systemctl status hailiang-skills-api@prod.service

# 看最近 200 行启动/报错日志
sudo journalctl -u hailiang-skills-api@prod.service -n 200 --no-pager

# 实时看正式服务日志
sudo journalctl -u hailiang-skills-api@prod.service -f

# 实时查看内部测试网页服务
sudo journalctl -u hailiang-skills-web@prod.service -f

# 看健康检查
curl -fsS http://<服务器私网IP>:8011/health/ready
```

测试环境将命令中的 `prod` 替换为 `test`，端口替换为 `8010`。

## 按现象排查

### 服务启动失败

执行：

```bash
sudo systemctl status hailiang-skills-api@prod.service
sudo journalctl -u hailiang-skills-api@prod.service -n 200 --no-pager
```

常见报错及处理：

| 现象 | 处理 |
| --- | --- |
| `DASHSCOPE_API_KEY is required` | 检查 `/etc/hailiang-skills/prod.env` 是否填写模型密钥。 |
| `does not match service instance` | 检查环境名、数据库名、Redis DB 和 Redis 前缀是否为 prod 固定值。 |
| `not writable` | 检查 `/var/lib/hailiang-skills/prod` 是否存在且 `hailiang` 用户可写。 |
| `Redis/rate limiter is not ready` | 执行 `docker compose ps`，检查 Redis 容器、端口和 Redis URL。 |
| 数据库连接错误 | 检查 PostgreSQL 容器、数据库账号、密码 URL 编码及数据库名。 |

### 健康检查返回 not_ready

`/health/ready` 同时检查 PostgreSQL、Redis、审计配置和模型限流器。先执行：

```bash
docker compose ps
docker compose logs --tail 100 postgres
docker compose logs --tail 100 redis
```

然后确认环境文件中的数据库连接串使用正确库名：测试为 `hailiang_skills_test`，正式为 `hailiang_skills`。

### 收到 429 或 MODEL_RATE_LIMITED

- HTTP `429 LLM_RATE_LIMITED`：请求还没有建立 SSE 连接，就未获得第一个模型调用名额。客户端应等待响应头 `Retry-After: 1` 后重试。
- SSE `MODEL_RATE_LIMITED`：SSE 已经开始，后续重试或补偿模型调用被保护性拒绝。客户端应结束本次流，并让用户重新发起。

如果频繁发生，检查 BFF 是否重复重试、测试环境是否共用同一模型 Key，或是否真的需要提高上游配额；不要直接把生产限流改到 50 以上。

### 某个会话回复异常

从 BFF、响应头或日志中拿到 `trace_id`、`request_id` 或 `session_id` 后执行：

```bash
PYTHONPATH=src .venv/bin/python scripts/show_request_trace.py --env prod <trace_id>
PYTHONPATH=src .venv/bin/python scripts/show_sse_trace.py --env prod --session-id <session_id>
```

测试环境会保存原始 SSE，适合复现。正式环境默认只保留关联 ID、耗时和错误信息，不保留完整对话原文。

## 日志轮转

JSONL 日志目录为 `/var/lib/hailiang-skills/<env>/logs`，每天轮转、压缩并保留 30 天。

```bash
sudo logrotate -d /etc/logrotate.d/hailiang-skills
```

`-d` 是演练模式，不会真实删除或切割日志。不要手工删除当前正在写入的日志文件。

## 正式版本回滚

如果新版本启动后健康检查失败或业务验证失败：

```bash
/opt/hailiang-skills/current-prod/deploy/bin/rollback-prod.sh
sudo systemctl status hailiang-skills-api@prod.service
```

该操作只回滚应用程序，不回滚数据库 SQL；这是为了避免数据被反向迁移损坏。
