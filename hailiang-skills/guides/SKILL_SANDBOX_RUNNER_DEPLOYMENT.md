# Skill Sandbox Runner 部署说明

Skill 脚本不应在 API Pod、宿主机或 Docker socket 中执行。生产和测试集群均通过独立的 `skill-sandbox-runner` Service 执行冻结 Skill 包中的 Python 脚本。

## 构建与部署

CI 除 API 镜像外，还要构建仓库根目录的 `Dockerfile.sandbox-runner`：

```text
${REGISTRY}/${DOCKERHUB_NAMESPACE}/${APP_NAME}-sandbox:${BUILD_NUMBER}
```

部署 `deploy/sandbox-runner.yaml` 前，CI/运维平台需要注入：

```text
APP_NAME
DOCKERHUB_NAMESPACE
REGISTRY
BUILD_NUMBER
SANDBOX_REPLICAS=2
SANDBOX_WORKERS_PER_POD=2
SANDBOX_QUEUE_TIMEOUT_SECONDS=5
SANDBOX_REQUEST_CPU=250m
SANDBOX_REQUEST_MEMORY=512Mi
SANDBOX_LIMIT_CPU=1
SANDBOX_LIMIT_MEMORY=1Gi
SANDBOX_PYPI_INDEX_URL=https://<internal-pypi>/simple
SANDBOX_PYPI_CIDR=<internal-pypi-cidr>
```

创建同名 Secret `${APP_NAME}-sandbox`，其中的 `shared-token` 必须是随机值。API Deployment 和 Runner Deployment 都从该 Secret 读取同一 token；不要将 token 写进 ConfigMap、`.env.example` 或仓库。

API ConfigMap/Secret 还必须加入 [sandbox-api.env.example](../../deploy/sandbox-api.env.example) 的变量。其中：

```text
HAILIANG_SCRIPT_EXECUTOR=remote
HAILIANG_REQUIRE_REMOTE_SANDBOX=true
HAILIANG_SANDBOX_ENDPOINT=http://${APP_NAME}-sandbox:8081
```

`HAILIANG_REQUIRE_REMOTE_SANDBOX=true` 会在 API 启动期校验远端执行器配置，防止配置漏掉后静默回退到 API Pod 内执行。

## 网络与依赖

Runner 的 NetworkPolicy 默认拒绝网络，仅允许 API Pod 入站、DNS 和 `SANDBOX_PYPI_CIDR:443` 出站。请将该 CIDR 配置为内部 PyPI/制品代理的固定出口；不要放通公网。

业务人员上传的 `scripts/requirements.txt` 必须使用固定版本，例如 `pandas==2.2.3`。Runner 拒绝 VCS、可编辑、本地路径、自带索引地址和未固定版本。依赖缓存键由 Runner 镜像、Python 版本、冻结 Skill 包和 requirements 内容共同决定；同一键命中缓存，不同发布内容不会相互污染。

## 本地验收

`./run.sh` 会通过 `docker-compose.local.yml` 启动 `skill-sandbox-runner`，默认地址为 `http://127.0.0.1:8091`。本地 `env.local.sh` 保持：

```bash
export HAILIANG_SCRIPT_EXECUTOR=remote
export HAILIANG_SANDBOX_ENDPOINT=http://127.0.0.1:8091
export HAILIANG_SANDBOX_SHARED_TOKEN=local-dev-sandbox-token
```

本地 API 是裸机进程，但脚本必须由容器内 Runner 执行，才能作为生产一致性验收。仅在显式设置 `HAILIANG_SCRIPT_EXECUTOR=legacy` 时保留旧执行路径，且只能用于开发诊断，不能用于测试或生产验收。

本地 Compose 与裸机 smoke 部署默认使用清华 PyPI 镜像：`https://pypi.tuna.tsinghua.edu.cn/simple/`。该地址于 2026-10-22 验证可访问（HTTP 200）；如网络策略或镜像稳定性要求不同，可通过 `HAILIANG_SANDBOX_PYPI_INDEX_URL` 覆盖。K8s 生产环境仍应使用运维提供的内网 PyPI/制品代理，并保持 NetworkPolicy 仅放通该内网地址。

## 裸机测试服务器

`./deploy-smoke.sh --env ./env.8010.sh ...` 同样启动裸机 API，但会默认启动或复用 Docker 中的 `skill-sandbox-runner`。测试服务器私有环境文件必须包含：

```bash
export HAILIANG_SCRIPT_EXECUTOR=remote
export HAILIANG_REQUIRE_REMOTE_SANDBOX=true
export HAILIANG_SANDBOX_ENDPOINT=http://127.0.0.1:8091
export HAILIANG_SANDBOX_SHARED_TOKEN='<随机且仅存于私有环境文件的值>'
export HAILIANG_SANDBOX_PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
```

如 Runner 由运维另行部署，可设置 `HAILIANG_SANDBOX_MANAGE_RUNNER=0`；部署脚本仍会在启动 API 前检查该 endpoint 的 `/healthz`。不要将 `HAILIANG_SCRIPT_EXECUTOR` 改为 `legacy` 来绕过 Runner：smoke 脚本会明确拒绝这种配置。
