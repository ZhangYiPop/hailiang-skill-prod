# 隔离 API 冒烟部署

`deploy-smoke.sh` 用于在已有旧测试服务的服务器上，以独立 PostgreSQL 数据库、Redis DB 和运行目录启动一份临时 API，验证转发服务器到指定 Base URL 的真实调用链。

它不启动或重建 Docker 容器、不安装 systemd 服务，也不改动 `deploy-all.sh`。默认只启动 API；传入 `--with-frontend` 时才会构建并启动内部前端。CentOS 7 会使用 `constraints/linux-legacy-ms-agent.txt` 和清华 PyPI 镜像安装兼容 wheel。

## 首次准备

1. 先确保 PostgreSQL 和 Redis 已运行。脚本不会启动容器；使用当前 Docker 初始化配置时，冒烟环境复用 `hailiang_test` 账号并使用专用数据库 `hailiang_skills_test_multi_profile_v1_smoke`，不要对常规测试库或生产库执行冒烟迁移。
2. 复制 `deploy/env/smoke.env.example` 为私有文件，例如 `/etc/hailiang-skills/smoke-8015.env`；填写真实值后执行 `chmod 600 /etc/hailiang-skills/smoke-8015.env`。其中 `HAILIANG_SECURITY_ADMIN_TOKEN` 必须为该冒烟实例单独生成的随机值。
3. 两把加密密钥分别生成，不能复用：

```bash
python3.11 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
```

## 部署

若端口为空闲：

```bash
./deploy-smoke.sh --env ./env.8015.sh
```

若确认旧的 Hailiang Uvicorn 服务可以由这次测试接管端口：

```bash
./deploy-smoke.sh --env ./env.8015.sh --replace-port
```

脚本只会对命令行中包含 `hailiang_skills.api.main:app` 且监听相同端口的进程发送 `SIGTERM`；其他进程会拒绝停止。
迁移前脚本会直接读取 `alembic_version`；数据库非空且不是
`0001_multi_profile_runtime` 时立即退出，不会执行 `stamp` 或覆盖表。

日常重新启动、依赖未变化时可跳过安装：

```bash
./deploy-smoke.sh --env ./env.8015.sh --skip-install
```

## 同时启动内部前端

在私有环境文件中设置 `HAILIANG_FRONTEND_BIND_HOST`、`FRONTEND_PORT` 与
`HAILIANG_PUBLIC_API_BASE_URL`，然后执行：

```bash
./deploy-smoke.sh --env ./env.8015.sh --replace-port --with-frontend
```


### 并行 8011 / 5178 Harness SSE v3 烟测

仓库中的 `env-8011.example.sh` 只有变量骨架和 `REPLACE_*` 占位符，不含任何服务器地址、路径、账号或密钥。服务器首次部署时复制为忽略的私有文件，再填写实际值：

```bash
cp env-8011.example.sh env.8011.sh
chmod 600 env.8011.sh
# 编辑 env.8011.sh：填写所有 REPLACE_*，并确保数据库与 Redis 命名空间独立于 8010。
```

`env.8011.sh` 使用后端 `8011` 与前端 `5178`；它应启用本地 `run.sh` 的 Harness v2 上下文策略和 SSE v3 开关。若已有私有 `env.8010.sh`，可在私有文件中 source 它，再仅覆盖 8011 的端口、数据库、Redis、运行目录和日志目录。

```bash
./deploy-smoke.sh --env ./env.8011.sh --replace-port --with-frontend
curl --fail http://REPLACE_SERVER_BIND_HOST:8011/health/ready
curl --fail -I http://REPLACE_SERVER_FRONTEND_HOST:5178/
```


脚本会将浏览器运行时 API 地址写入 `frontend/dist/runtime-config.js`，并自动把前端 Origin 追加到 `HAILIANG_CORS_ORIGINS`。带 `--replace-port` 时，脚本也会向监听 `FRONTEND_PORT` 的既有 `scripts/static_frontend_server.py` 发送 `SIGTERM` 后再启动新前端；若该端口属于其他程序，脚本仍会拒绝停止，避免误杀服务。

成功后检查：

```bash
curl -fsS http://REPLACE_PRIVATE_IP:8015/health/ready
tail -n 100 smoke-8015.log
```

再将转发服务器的 Base URL 临时指向该地址，完成一条真实转发请求。测试结束后，使用 `kill -TERM <脚本输出的PID>` 停止临时进程；隔离数据库和 Redis DB 可保留用于下次测试。
