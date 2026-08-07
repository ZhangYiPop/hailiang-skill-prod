# 单节点工程化部署手册

这份手册面向第一次部署的算法/产品同学。你不需要理解 systemd、Docker 的全部细节；按步骤执行即可。

系统在一台服务器上运行两套 API 和两套内部测试网页：测试环境用于验证新版本，正式环境供 BFF 调用。网页是构建后的 React 静态文件，由 systemd 管理的小型 Python 服务提供，不使用 Vite Preview、Nginx 或负载均衡。

| 项目 | 测试 | 正式 |
| --- | --- | --- |
| API 端口 | 8010 | 8011 |
| 网页端口 | 4175 | 4176 |
| 数据库 | `hailiang_skills_test` | `hailiang_skills` |
| Redis | DB 1 | DB 2 |
| 模型限流 | 5 QPS | 45 QPS |
| 原始 SSE 记录 | 开启 | 默认关闭 |

## 一次性服务器准备

以下步骤只在新服务器执行一次，需要具有 `sudo` 权限。

1. 安装 Docker、Docker Compose、Python 3.11、Node.js、PostgreSQL 客户端工具 `pg_dump` 和 systemd。

2. 创建服务账号和固定目录：

```bash
sudo useradd --system --create-home hailiang
sudo mkdir -p /opt/hailiang-skills/releases
sudo mkdir -p /var/lib/hailiang-skills/{test,prod}/{logs,runtime,backups}
sudo chown -R hailiang:hailiang /var/lib/hailiang-skills
```

3. 创建 Docker 基础设施密钥文件 `/etc/hailiang-skills/infra.env`：

```bash
POSTGRES_SUPERUSER=postgres
POSTGRES_SUPERUSER_PASSWORD=请填写强密码
HAILIANG_PROD_DB_PASSWORD=生产数据库密码
HAILIANG_TEST_DB_PASSWORD=测试数据库密码
```

```bash
sudo chmod 600 /etc/hailiang-skills/infra.env
```

4. 在项目根目录启动数据库和 Redis：

```bash
sudo docker compose --env-file /etc/hailiang-skills/infra.env up -d postgres redis
sudo docker compose --env-file /etc/hailiang-skills/infra.env ps
```

首次启动会创建测试/生产数据库和账号。Docker 数据卷已经存在时，初始化脚本不会重新执行；不要删除线上数据卷来重复初始化。

5. 安装服务和日志轮转配置：

```bash
sudo cp deploy/systemd/hailiang-skills-api@.service /etc/systemd/system/
sudo cp deploy/systemd/hailiang-skills-web@.service /etc/systemd/system/
sudo cp deploy/logrotate/hailiang-skills /etc/logrotate.d/
sudo systemctl daemon-reload
```

## 准备一个可发布版本

每个版本必须放在如下目录：

```text
/opt/hailiang-skills/releases/20260805.1/
```

目录中至少要有：项目源码、`VERSION` 文件、`.venv/` 虚拟环境、`deploy/`、`src/`、`runtime_skills/` 和 `assets/generated/`。`VERSION` 文件只写版本号，例如 `20260805.1`。

首次准备版本时，在该目录执行：

```bash
sudo -u hailiang /usr/local/bin/python3.11 -m venv .venv
sudo -u hailiang .venv/bin/python -m pip install --upgrade pip setuptools wheel
sudo -u hailiang .venv/bin/python -m pip install -e .
```

同时必须将共享运行时核心部署到环境文件配置的 `AGENT_SKILL_RUNTIME_CORE_PATH`，例如 `/opt/agent-skill-runtime-core`。

## 日常新版本升级（按此顺序执行）

以下示例以新版本 `4.11.0.2` 为例。先将新源码上传至服务器暂存目录；不要覆盖正在运行的
`/opt/hailiang-skills/current-*` 目录，也不要删除旧版本目录。

```bash
export VERSION=4.11.0.2
export SOURCE_ROOT=/home/hljy/tmp/hailiang-skill_sensitive_v02121
export RELEASE=/opt/hailiang-skills/releases/$VERSION

sudo mkdir -p "$RELEASE"
sudo rsync -a \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude 'frontend/node_modules' \
  --exclude 'frontend/dist' \
  "$SOURCE_ROOT/hailiang-skills/" \
  "$RELEASE/"

echo "$VERSION" | sudo tee "$RELEASE/VERSION"
sudo chown -R hailiang:hailiang "$RELEASE"

sudo -u hailiang /usr/local/bin/python3.11 -m venv "$RELEASE/.venv"
sudo -u hailiang "$RELEASE/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
sudo -u hailiang "$RELEASE/.venv/bin/python" -m pip install -e "$RELEASE"
```

服务器已配置国内 pip 镜像时，最后两条会自动使用它；若没有配置，应在命令末尾增加
`-i https://pypi.tuna.tsinghua.edu.cn/simple --timeout 120 --retries 5`。

若新版本**明确包含** `agent_skill_runtime_core` 的兼容变更，再单独安排该共享依赖的升级和测试；
不要在常规 API 发版时无条件覆盖 `/opt/agent-skill-runtime-core`，因为测试和生产环境会共用它。

每次升级都要核对新模板是否新增了环境变量，但绝不能用模板覆盖 `/etc/hailiang-skills/*.env`。
在 test 和 prod 环境文件中将版本号改为本次版本：

```ini
HAILIANG_RELEASE_VERSION=4.11.0.2
```

若新版本修改了 systemd unit 或 logrotate 文件，再安装并重载 systemd：

```bash
sudo install -m 0644 "$RELEASE/deploy/systemd/hailiang-skills-api@.service" /etc/systemd/system/
sudo install -m 0644 "$RELEASE/deploy/systemd/hailiang-skills-web@.service" /etc/systemd/system/
sudo install -m 0644 "$RELEASE/deploy/logrotate/hailiang-skills" /etc/logrotate.d/
sudo systemctl daemon-reload
```

确认基础设施仍然可用：

```bash
cd "$RELEASE"
sudo docker compose --env-file /etc/hailiang-skills/infra.env ps
```

如果 PostgreSQL 或 Redis 不是 `running`，先恢复它们：

```bash
sudo docker compose --env-file /etc/hailiang-skills/infra.env up -d postgres redis
```

## 部署测试环境

先按《环境配置说明》创建 `/etc/hailiang-skills/test.env`。然后部署版本：

```bash
cd "$RELEASE"
set -a
source /etc/hailiang-skills/test.env
set +a
./deploy/bin/promote-release.sh test "$VERSION"
```

验证：

```bash
sudo systemctl status hailiang-skills-api@test.service --no-pager
sudo systemctl status hailiang-skills-web@test.service --no-pager
curl -fsS http://<服务器私网IP>:8010/health/ready
```

只有响应中的 `status` 为 `ok`，才可进入正式发布。

## 部署正式环境

正式发布前必须已完成测试部署和业务冒烟验证。然后：

```bash
cd "$RELEASE"
set -a
source /etc/hailiang-skills/prod.env
set +a
./deploy/bin/promote-release.sh prod "$VERSION"
```

脚本会备份生产数据库、执行数据库迁移、切换版本软链接、优雅重启服务并检查健康状态。生产服务使用 `current-prod` 软链接；`current` 是其兼容别名。测试服务独立使用 `current-test`，不会因测试新版本而改动运行中的生产程序。

验证：

```bash
sudo systemctl status hailiang-skills-api@prod.service --no-pager
sudo systemctl status hailiang-skills-web@prod.service --no-pager
curl -fsS http://<服务器私网IP>:8011/health/ready
```

## 启停与回滚

```bash
./deploy/bin/test-start.sh
./deploy/bin/test-stop.sh
./deploy/bin/prod-start.sh
./deploy/bin/prod-stop.sh
./deploy/bin/test-web-start.sh
./deploy/bin/test-web-stop.sh
./deploy/bin/prod-web-start.sh
./deploy/bin/prod-web-stop.sh
./deploy/bin/rollback-prod.sh
```

不要使用旧的 `deploy-all.sh` 或 `stop-all.sh`；它们已废弃，且不会再执行 `nohup` 或强制杀进程。
