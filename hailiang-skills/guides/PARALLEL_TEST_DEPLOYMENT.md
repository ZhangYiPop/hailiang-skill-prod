# 同一台服务器部署新版测试服务与正式服务

本流程分为两个阶段：先部署隔离的 `test-next` 做验收，再部署与旧正式服务并行的新正式实例。两阶段都使用既有的不可变发布目录和 systemd 服务，不使用 `deploy-smoke.sh`。`deploy-smoke.sh` 只用于临时烟测；它不管理 `/opt/hailiang-skills/current-*` 软链接或正式 systemd 实例。

适用于当前 `ecs-syagent1` 服务器的已知现状：PostgreSQL 容器名为 `41101-postgres-1`，Redis 容器名为 `41101-redis-1`；旧正式 API 使用 `8015`，旧正式前端使用 `4176`；`8010`、`5177`、`8022` 可用于新版并行测试。当前旧正式 API/前端是手工启动进程，不是已加载的参数化 systemd 服务，因此部署新版前必须先安装 systemd 模板。

这里的并行实例名固定为 `test-next`。它与原有 `test`、`prod` 实例隔离：

| 项目 | 原有实例 | 新并行实例 |
| --- | --- | --- |
| systemd | `hailiang-skills-*@test` / `@prod` | `hailiang-skills-*@test-next` |
| 发布链接 | `current-test` / `current-prod` | `current-test-next` |
| PostgreSQL | 旧数据库 | `hailiang_skills_test_multi_profile_v1_next` |
| Redis | 原有库与前缀 | DB `3`，前缀 `hailiang:test-next:` |
| API / 前端端口 | 原端口 | `8010` / `5177` |
| 本地状态 | 原目录 | `/var/lib/hailiang-skills/test-next/` |

## 一次性准备

以下命令在服务器执行。先确认端口没有被占用：

```bash
sudo ss -lntp | grep -E ':(8010|5177|8022)\b' || true
```

### 先确认当前提示符

下面会出现两种提示符：

```text
[hljy@服务器目录]$   ← Linux 命令行，只执行 docker、cd、sudo 等命令
postgres=#            ← PostgreSQL 命令行，才能执行 CREATE ROLE、CREATE DATABASE 等 SQL
```

看到 `postgres=#` 才执行 SQL。看到 `[hljy@...]$` 时，不要输入 `CREATE DATABASE`，否则会出现 `bash: CREATE: command not found`。

创建独立数据库账号和空数据库。不要把密码写进 shell 历史；该命令会交互式要求输入两次密码。

```bash
sudo docker exec -it 41101-postgres-1 \
  sh -lc 'psql -U "$POSTGRES_USER" -d postgres'
```

进入 PostgreSQL 后，看到 `postgres=#`，先只执行这一条：

```sql
CREATE ROLE hailiang_test_next LOGIN;
```

看到 `CREATE ROLE` 并重新出现 `postgres=#` 后，再单独执行：

```sql
\password hailiang_test_next
```

看到 `Enter new password` 和 `Enter it again` 时分别输入两次密码。重新出现 `postgres=#` 后，再执行：

```sql
CREATE DATABASE hailiang_skills_test_multi_profile_v1_next
  OWNER hailiang_test_next;
```

最后再执行：

```sql
\q
```

不要把多条命令一起粘贴；每一条都要等重新出现 `postgres=#` 后再执行。

如果角色或数据库已经存在，不要重复执行 `CREATE`；改为检查并复用它们：

```sql
\du hailiang_test_next
\l hailiang_skills_test_multi_profile_v1_next
```

创建 `/etc/hailiang-skills/test-next.env`：

```bash
export SOURCE_ROOT=/home/hljy/tmp/gitlab/tmp/hailiang-skill_sensitive_v02121
test -f "$SOURCE_ROOT/hailiang-skills/deploy/env/test-next.env.example"
sudo install -m 600 -o root -g hailiang \
  "$SOURCE_ROOT/hailiang-skills/deploy/env/test-next.env.example" \
  /etc/hailiang-skills/test-next.env
sudoedit /etc/hailiang-skills/test-next.env
```

`SOURCE_ROOT` 必须是服务器上实际上传并解压的源码根目录，不是开发机上的 `/Users/ayi/...` 路径。如果之后使用版本暂存目录上传，则改成对应的 `/opt/hailiang-staging/<版本号>`。

将其中的数据库用户名、密码、私有 IP、模型与安全配置替换为真实值。数据库 URL 必须指向 `hailiang_skills_test_multi_profile_v1_next`；不要指向任何已有 `test` 或 `prod` 数据库。

安装参数化服务单元（只需一次）：

```bash
sudo install -m 644 deploy/systemd/hailiang-skills-api@.service /etc/systemd/system/
sudo install -m 644 deploy/systemd/hailiang-skills-web@.service /etc/systemd/system/
sudo install -m 644 deploy/systemd/hailiang-skills-workbench@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hailiang-skills-api@test-next.service
sudo systemctl enable hailiang-skills-web@test-next.service
sudo systemctl enable hailiang-skills-workbench@test-next.service
```

如果服务器上原来没有这些 unit，安装后可以先只启动新版测试实例；不要停止当前手工运行的旧正式进程：

```bash
sudo systemctl start hailiang-skills-api@test-next.service
sudo systemctl start hailiang-skills-web@test-next.service
```

## 每次发布

用新版本号发布，不能覆盖已经存在的 release 目录：

```bash
sudo -i
export SOURCE_ROOT=/home/hljy/Project/hailiang-skill_sensitive_v02121
export VERSION=4.11.0.2
cd "$SOURCE_ROOT/hailiang-skills"
./deploy/bin/deploy-version.sh test-next "$VERSION" "$SOURCE_ROOT"
```

脚本只会对 `test-next` 的新空数据库执行迁移，随后切换 `current-test-next` 并启动三个 `@test-next` 服务；不会触碰 `current-test`、`current-prod`、旧数据库或旧 Redis 前缀。

## 以后每次代码更新的发布流程

以后不要复用已经发布过的版本号，也不要直接修改正在运行的 release 目录。每次代码更新都使用一个新的版本号，例如：

```text
4.11.0.2 → 4.11.0.3 → 4.11.0.4
```

### 第一步：本地打包并上传代码

这台服务器不使用 Git 更新代码。请在本地开发机执行打包，使用新的版本号；不要把日志、虚拟环境、前端依赖、运行时缓存或环境密钥打进压缩包：

```bash
cd /Users/ayi/Project/hailiang-skill_sensitive_v02121
export VERSION=4.11.0.3
tar \
  --exclude='hailiang-skills/.git' \
  --exclude='hailiang-skills/.venv' \
  --exclude='hailiang-skills/.venv-*' \
  --exclude='hailiang-skills/node_modules' \
  --exclude='hailiang-skills/frontend/dist' \
  --exclude='hailiang-skills/logs' \
  --exclude='hailiang-skills/runtime*' \
  --exclude='hailiang-skills/*.env' \
  --exclude='hailiang-skills/env*.sh' \
  --exclude='hailiang-skills/.dbg' \
  --exclude='hailiang-skills/.skill_runtime_cache' \
  -czf "/tmp/hailiang-skills-${VERSION}.tar.gz" \
  hailiang-skills
```

确认压缩包存在后上传到服务器：

```bash
scp "/tmp/hailiang-skills-${VERSION}.tar.gz" \
  hljy@服务器IP:/tmp/
```

将上面的 `hljy@服务器IP` 替换为实际服务器登录地址，例如 `hljy@10.153.3.146`。如果服务器禁止 `scp`，使用现有的文件传输工具上传到服务器的 `/tmp/` 目录即可。

在服务器上解压到新的暂存目录：

```bash
sudo -i
export VERSION=4.11.0.3
export SOURCE_ROOT="/opt/hailiang-staging/${VERSION}"
mkdir -p "$SOURCE_ROOT"
tar -xzf "/tmp/hailiang-skills-${VERSION}.tar.gz" \
  -C "$SOURCE_ROOT"
test -f "$SOURCE_ROOT/hailiang-skills/pyproject.toml"
```

确认 `test -f` 没有报错后，才继续发布。每次上传都要使用新的 `VERSION`，不能覆盖已经发布过的版本目录。

### 第二步：先发布到 test-next

```bash
cd "$SOURCE_ROOT/hailiang-skills"
./deploy/bin/deploy-version.sh test-next "$VERSION" "$SOURCE_ROOT"
```

这个命令会自动完成：

- 复制代码到 `/opt/hailiang-skills/releases/$VERSION`；
- 创建该 release 的 Python 虚拟环境并安装依赖；
- 对 `test-next` 数据库执行 Alembic 迁移；
- 更新 `current-test-next`；
- 重启测试 API、工作台和前端。

如果迁移失败，停止在测试环境排查，不要手动修改旧正式数据库。查看日志：

```bash
sudo journalctl -u hailiang-skills-api@test-next.service -n 200 --no-pager
sudo systemctl status hailiang-skills-api@test-next.service \
  hailiang-skills-workbench@test-next.service \
  hailiang-skills-web@test-next.service --no-pager
```

### 第三步：测试验收

先确认服务健康：

```bash
curl --fail http://私网IP:8010/health/ready
curl --fail -I http://私网IP:5177/
```

然后在测试工作台验证：

- Skill、专家、专家团的候选测试；
- 表单、Markdown、专家转交和流式回复；
- 脚本、工具、引用和调用轨迹；
- 无孩子上下文与孩子切换；
- 正式发布前的校验和测试证据。

只有测试通过后，才进入正式发布步骤。

### 第四步：发布正式版本

如果允许新版替换当前正式服务，先完成旧正式数据库备份，然后执行：

```bash
cd "$SOURCE_ROOT/hailiang-skills"
./deploy/bin/deploy-version.sh prod "$VERSION" "$SOURCE_ROOT"
```

该命令会对正式环境执行迁移、更新 `current-prod`、重启正式服务并检查健康状态。正式环境的数据库迁移由脚本执行，不要另外手动运行 Alembic。

如果旧 App 必须继续使用旧服务，则不能执行上面的 `prod` 替换流程。应等待 `prod-next` 并行实例支持完成，使用独立正式端口和数据库发布新版，再由 BFF 按 App/租户定向路由。旧服务保持 `8015/4176` 不动。

### 第五步：保留旧版本和回滚依据

每次发布后检查软链接：

```bash
sudo ls -la /opt/hailiang-skills | grep -E 'current|previous'
```

`previous-prod` 和 `previous-test` 指向上一个已发布版本。出现问题时先停止流量扩大，再保留日志和数据库状态；不要删除 release 目录或直接覆盖软链接，回滚应使用项目提供的回滚脚本或经过确认的软链接切换方案。

## 验证与排错

```bash
curl --fail http://私网IP:8010/health/ready
curl --fail -I http://私网IP:5177/
sudo systemctl status hailiang-skills-api@test-next.service \
  hailiang-skills-workbench@test-next.service \
  hailiang-skills-web@test-next.service --no-pager
sudo journalctl -u hailiang-skills-api@test-next.service -n 200 --no-pager
```

数据库迁移失败时停止，不要改旧库重试。先确认当前 URL：

```bash
sudo grep '^HAILIANG_DATABASE_URL=' /etc/hailiang-skills/test-next.env
```

它必须是 `.../hailiang_skills_test_multi_profile_v1_next`。新库为空时，迁移会从当前代码的基线开始创建全部结构。

## 新正式服务并行部署（仅在 test-next 验收后执行）

不要把新版迁移直接打到旧正式数据库，也不要停止旧正式服务。旧正式服务继续使用 `8015/4176` 和原数据库；新版正式服务必须使用独立端口、独立数据库、独立 Redis 前缀和独立 systemd 实例。旧 App 继续请求旧地址，新 App 或指定租户由 BFF 定向请求新版地址。

> 注意：当前仓库已经支持 `test-next`，但还没有把 `prod-next` 作为正式环境实例加入发布脚本。要实现下面的双正式服务，需要先扩展环境校验、systemd 实例和发布脚本支持 `prod-next`；不能把 `test-next` 冒充正式环境，也不能让两个服务抢占同一个端口。

### 1. 备份并保留旧正式配置

```bash
sudo -i
set -a
source /etc/hailiang-skills/prod.env
set +a
mkdir -p /var/lib/hailiang-skills/prod/backups
OLD_DUMP_URL="$(/opt/hailiang-skills/current-prod/.venv/bin/python - <<'PY'
import os
print(os.environ['HAILIANG_DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://', 1))
PY
)"
pg_dump --dbname="$OLD_DUMP_URL" --format=custom \
  --file="/var/lib/hailiang-skills/prod/backups/before-new-runtime-$(date +%Y%m%d%H%M%S).dump"
cp -a /etc/hailiang-skills/prod.env "/root/prod.env.before-new-runtime-$(date +%Y%m%d%H%M%S)"
unset OLD_DUMP_URL
```

确认命令成功后才继续。不要把备份文件、`prod.env` 或任何密码提交到 Git。

### 2. 创建新版正式数据库

```bash
sudo docker exec -it 41101-postgres-1 \
  sh -lc 'psql -U "$POSTGRES_USER" -d postgres'
```

在 `postgres=#` 中，先只执行这一条：

```sql
CREATE ROLE hailiang_prod_next LOGIN;
```

看到 `CREATE ROLE` 并重新出现 `postgres=#` 后，执行：

```sql
\password hailiang_prod_next
```

按提示输入两次新的正式数据库密码。重新出现 `postgres=#` 后，再执行：

```sql
CREATE DATABASE hailiang_skills_multi_profile_v1_next
  OWNER hailiang_prod_next;
```

看到 `CREATE DATABASE` 后，再执行：

```sql
\q
```

不要在 `[hljy@...]$` 的 Linux 命令行下输入这些 SQL。

### 3. 准备新版正式实例配置

不要覆盖正在运行旧服务使用的 `/etc/hailiang-skills/prod.env`。应新增独立的 `prod-next.env`，除下列项目外复制正式环境的模型、安全和阿里云防护配置：

```ini
HAILIANG_DEPLOY_ENV=prod-next
BACKEND_PORT=新版正式服务端口（不能是8015）
FRONTEND_PORT=新版正式前端端口（不能是4176）
WORKBENCH_PORT=新版正式工作台端口
HAILIANG_DATABASE_URL=postgresql+psycopg://hailiang_prod_next:新密码@127.0.0.1:5432/hailiang_skills_multi_profile_v1_next
HAILIANG_REDIS_URL=redis://127.0.0.1:6379/2
HAILIANG_REDIS_KEY_PREFIX=hailiang:prod:next:
HAILIANG_LOG_DIR=/var/lib/hailiang-skills/prod/logs
HAILIANG_STATE_DIR=/var/lib/hailiang-skills/prod/runtime
PYTHONPATH=/opt/hailiang-skills/current-prod-next/src:/opt/agent-skill-runtime-core
```

当前代码还需要先扩展发布脚本和环境校验，使其支持 `prod-next`；完成这项支持后，再安装 `@prod-next` 的 systemd 实例并发布新版。不能把 `prod-next` 配置写进旧 `prod.env`，也不能让两个服务抢占同一个端口。

当前服务器旧正式进程继续使用 `8015/4176`；`8010/5177` 仅属于 `test-next`，不能直接作为新版正式服务端口，除非明确将其作为新版正式实例的独立地址。

### 4. 新正式实例发布和验证

这一步必须在 `prod-next` 支持加入发布脚本后执行，目标是 `current-prod-next` 和：

```text
hailiang-skills-api@prod-next.service
hailiang-skills-workbench@prod-next.service
hailiang-skills-web@prod-next.service
```

新正式库创建后不含业务对象。完成服务健康检查后，先通过业务工作台导入并激活已验收的专家团/专家/Skill 发布包，确认存在默认专家团和锁定依赖。

### 5. BFF 定向路由

这里的“BFF 流量切换”不是停止旧服务，也不是把所有请求一次性改掉，而是由转发层按调用方选择目标：

| 调用方 | 目标服务 | 结果 |
| --- | --- | --- |
| 旧 App | 旧正式 API `8015` | 继续使用旧版本和旧数据库 |
| 新 App/指定灰度租户 | 新正式 API 的独立端口 | 使用新版数据库和新版 Runtime |

确认新版稳定后，可以逐步扩大新版路由范围；旧服务只有在所有旧 App 完成迁移后，才考虑下线。若 BFF 当前只能配置一个固定地址，需要先增加按 App、租户或请求头选择上游的配置，不能直接覆盖旧地址。
