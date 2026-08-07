# 隔离 API 冒烟部署

`deploy-smoke.sh` 用于在已有旧测试服务的服务器上，以独立 PostgreSQL 数据库、Redis DB 和运行目录启动一份临时 API，验证转发服务器到指定 Base URL 的真实调用链。

它不启动或重建 Docker 容器、不构建前端、不安装 systemd 服务，也不改动 `deploy-all.sh`。CentOS 7 会使用 `constraints/linux-legacy-ms-agent.txt` 和清华 PyPI 镜像安装兼容 wheel。

## 首次准备

1. 在已有 PostgreSQL 容器中创建一个独立的角色和数据库，例如 `hailiang_smoke_411` 与 `hailiang_skills_test_411`。不要对旧的 `hailiang_skills_test` 执行本版本的迁移。
2. 复制 `deploy/env/smoke.env.example` 为项目根目录的私有文件，例如 `env.8015.sh`；填写真实值后执行 `chmod 600 env.8015.sh`。
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

日常重新启动、依赖未变化时可跳过安装：

```bash
./deploy-smoke.sh --env ./env.8015.sh --skip-install
```

成功后检查：

```bash
curl -fsS http://REPLACE_PRIVATE_IP:8015/health/ready
tail -n 100 smoke-8015.log
```

再将转发服务器的 Base URL 临时指向该地址，完成一条真实转发请求。测试结束后，使用 `kill -TERM <脚本输出的PID>` 停止临时进程；隔离数据库和 Redis DB 可保留用于下次测试。
