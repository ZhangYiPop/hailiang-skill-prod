# 环境配置说明

环境文件保存密码、端口和部署策略。测试和正式环境必须使用不同文件，禁止复制后只改一个端口。

```bash
sudo cp deploy/env/test.env.example /etc/hailiang-skills/test.env
sudo cp deploy/env/prod.env.example /etc/hailiang-skills/prod.env
sudo chmod 600 /etc/hailiang-skills/test.env /etc/hailiang-skills/prod.env
sudo nano /etc/hailiang-skills/test.env
```

## 必须填写的参数

| 参数 | 含义 | 测试 | 正式 |
| --- | --- | --- | --- |
| `DASHSCOPE_API_KEY` | 大模型服务密钥 | 测试密钥或共享密钥 | 生产密钥 |
| `HAILIANG_AUDIT_ENCRYPTION_KEY` | 审计加密密钥 | 独立密钥 | 独立密钥 |
| `HAILIANG_SECURITY_QUARANTINE_KEY` | 风控拦截证据加密密钥 | 独立密钥 | 独立密钥 |
| `HAILIANG_DATABASE_URL` | PostgreSQL 连接串 | `hailiang_skills_test` | `hailiang_skills` |
| `HAILIANG_STORAGE_BACKEND` | 会话、事实和审计的持久化后端 | 固定 `postgres` | 固定 `postgres` |
| `HAILIANG_BIND_HOST` | API 监听地址 | `127.0.0.1` | BFF 可访问的私网 IP |
| `HAILIANG_FRONTEND_BIND_HOST` | 内部测试网页监听地址 | 测试人员可访问的私网 IP | 测试人员可访问的私网 IP |
| `HAILIANG_PUBLIC_API_BASE_URL` | 浏览器调用 API 的地址 | `http://私网IP:8010` | `http://私网IP:8011` |
| `HAILIANG_CORS_ORIGINS` | 允许网页调用 API 的来源 | `http://私网IP:4175` | `http://私网IP:4176` |
| `AGENT_SKILL_RUNTIME_CORE_PATH` | 共享运行时核心目录 | 实际服务器路径 | 实际服务器路径 |
| `PYTHONPATH` | 源码和运行时核心路径 | `current-test/src:...` | `current-prod/src:...` |

阿里云内容安全与 DashScope 是两套独立凭证。若配置 `ALIBABA_CLOUD_ACCESS_KEY_ID` 和 `ALIBABA_CLOUD_ACCESS_KEY_SECRET`，应用使用云端审核；两者缺失时会自动使用本地敏感词库作为降级策略。本地词库覆盖很宽，测试中更可能误拦截正常的模型输出。`/health/ready` 的 `security.aliyun_available` 必须为 `true` 才表示云审核已实际启用。

数据库密码中若含 `@`、`:`、`/` 等字符，必须进行 URL 编码，否则数据库连接会失败。

## 不要修改的隔离参数

这些参数代表环境边界，写错后服务会拒绝启动：

| 参数 | 测试固定值 | 正式固定值 |
| --- | --- | --- |
| `HAILIANG_DEPLOY_ENV` | `test` | `prod` |
| `HAILIANG_STORAGE_BACKEND` | `postgres` | `postgres` |
| `BACKEND_PORT` | `8010` | `8011` |
| Redis URL 最后部分 | `/1` | `/2` |
| `HAILIANG_REDIS_KEY_PREFIX` | `hailiang:test:` | `hailiang:prod:` |
| `HAILIANG_LLM_RATE_LIMIT_QPS` | `5` | `45` |
| `HAILIANG_MAX_SSE_CONNECTIONS` | `10` | `100` |

生产环境禁止使用 `HAILIANG_BIND_HOST=0.0.0.0`。API 只应被 BFF 通过私网访问；安全组和防火墙也应只放行 BFF 所在网段。

## 原始对话与隐私开关

生产默认值如下，不建议修改：

```bash
HAILIANG_SSE_RECORDING_ENABLED=false
HAILIANG_LOCAL_SESSION_CACHE_ENABLED=false
HAILIANG_AUDIT_RAW_CONTENT_ENABLED=false
```

发生严重线上问题时，可临时将 `HAILIANG_AUDIT_RAW_CONTENT_ENABLED=true`，然后重启生产服务。原文只会进入加密审计库，不写普通 JSONL 日志；排查结束后必须改回 `false` 并重启。

`HAILIANG_SECURITY_QUARANTINE_KEY` 与审计密钥用途不同，必须配置且应使用另一把随机密钥。它只用于加密保存被风控拦截的输入或输出，方便受权限控制的事故复盘；缺失时应用应拒绝启动，避免出现“内容被拦截却没有可审计证据”的状态。

两把密钥分别生成，命令相同；每次执行都会生成一把新的密钥：

```bash
/opt/hailiang-skills/current-test/.venv/bin/python -c \
'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
```

不要将输出发到聊天、提交到代码仓库或写入普通日志。
