# 项目容器化部署：运维支持需求清单

> 用途：把本清单发给运维/平台团队，确认部署平台能力、网络和存储条件，并据此安排后端工程化改造。  
> 项目：`hailiang-skills`（FastAPI 后端、对话 Runtime、业务工作台、前端、Harness 任务 Worker、动态 Skill 脚本）。  
> 日期：2026-10-19。本文依据当前工作区代码整理；不包含真实线上环境、资源配额或凭据核验结果。

## 发给运维的需求说明

我们准备把 hailiang-skills 项目改造成 Docker 镜像，并通过 Kubernetes 部署。项目不是单一无状态 HTTP API：它包含 SSE 对话接口、动态 Skill 脚本执行、Harness 长任务、PostgreSQL 持久化、Redis 协调、前端静态资源、加密审计和运行时文件目录。请协助确认并提供下述平台能力、参数和接入规范。代码改造将根据确认结果实施；当前仓库没有可直接用于生产的应用 Dockerfile/K8s 模板。

### 一、请运维提供或确认的基础信息

| 需求 | 请提供的信息/支持 | 用途 |
|---|---|---|
| K8s 环境 | 集群版本、目标 namespace、可用节点架构（amd64/arm64）、Pod 安全策略、默认 ServiceAccount 规则 | 确定镜像平台、资源权限和清单 API 版本 |
| 镜像仓库 | Registry 地址、项目/命名空间、镜像命名规则、推送权限、拉取凭据 Secret 名称、镜像保留策略 | CI 构建、发布、集群拉取镜像 |
| 基础镜像和 Python 包源 | 批准的 Python 3.11 Debian/glibc 系基础镜像地址与版本；pip/npm 内网源、证书和访问方式 | 科学计算与密码学依赖需要目标 Linux wheel；复现旧 Linux 安装经验 |
| CI/CD 规范 | 现有流水线如何接收 `BUILD_NUMBER`、`APP_NAME` 等变量，模板渲染工具、校验方式、发布审批入口、回退机制 | 与运维现有流程对接；本项目暂不假设 Jenkins/GitLab 等具体产品 |
| 集群入口 | Ingress/Gateway 类型、域名、TLS 证书归属、BFF/网关路由、健康检查要求 | 对外提供 API/前端访问并正确转发 SSE |
| 资源基线 | namespace CPU/内存配额、Pod 数上限、存储配额、HPA 可用指标、调度限制 | 先给建议范围或测试环境配额，再用容量测试修订 |
| 观测平台 | 容器日志采集方式、字段规范、保留天数、查询入口；Prometheus/OTel Collector 地址和认证 | 统一查看请求、运行任务、错误、链路和指标 |
| 安全要求 | Pod UID/GID、只读根文件系统要求、Linux capabilities、出网控制、Secret 管理和轮换方式 | 定义可运行容器的安全上下文和外部服务访问策略 |

请同时确认运维示例模板变量的语义，尤其 `DOCKERHUB_NAMESPACE` 是否同时是 K8s namespace，还是只用于镜像仓库路径。建议沿用已有参数名：`REGISTRY`、`DOCKERHUB_NAMESPACE`、`APP_NAME`、`APP_CONFIG`、`BUILD_NUMBER`、`APP_REPLICAS`、`APP_PVC_NAME`、`REQUEST_CPU`、`REQUEST_MEMORY`、`LIMITS_CPU`、`LIMITS_MEMORY`；如现场含义不同，请拆成明确参数，避免同一个变量同时表达仓库和 namespace。

### 二、数据服务和网络访问

当前生产样例和代码涉及 PostgreSQL、Redis、模型 API、阿里云内容审核及可选 OTel。请运维确认平台已有服务还是需要申请新实例，并提供容器网络可访问的 DNS 地址、端口、TLS/认证要求和连接限额。

| 服务 | 项目用途 | 运维需提供/确认 |
|---|---|---|
| PostgreSQL | 会话/用户档案、事实、Harness 运行与任务状态、事件审计、工作台版本和发布数据 | 测试与生产库、账号最小权限、TLS、最大连接数、备份/PITR、监控告警、维护窗口；应用 Pod 不应直接拥有建库权限 |
| Redis | SSE 并发协调、取消/代际状态、QPS 限流和 Harness Worker 唤醒消息 | 地址/DB 或隔离方式、认证/TLS、连接上限、持久化/淘汰策略、故障告警；Redis wakeup 是通知机制，任务状态仍以数据库为准 |
| 大模型服务 | DashScope 兼容 Chat Completions 与 Embedding API | 两类 endpoint 的 HTTPS 出网白名单、DNS/代理要求、密钥注入方式、供应商额度分配及 429 告警联系人 |
| 内容安全服务 | 阿里云内容审核 API；另有本地词库 | 访问域名/区域、AK/SK Secret 管理；没有云服务凭据时应用会降级到本地词库，业务需知晓精度差异 |
| OTel/Prometheus | 指标与链路 | OTel Collector 地址/协议，Prometheus `/metrics` 抓取策略与网络策略 |
| Skill 依赖安装所需出网 | 当前 Docker 沙箱按 Skill `scripts/requirements.txt` 安装依赖 | 决定执行架构后确认镜像仓库、Python 包源及每个获批 Skill 的外部域名。若生产脚本默认禁网，须由平台网络规则落实 |

所有密码、API Key、审计加密密钥和安全管理 Token 通过 Kubernetes Secret 或企业 Secret 管理系统注入。请提供 Secret 创建/轮换方式；不要把值写入 Git、ConfigMap、镜像层或流水线明文日志。

需要单独保管并恢复的密钥至少包括 `DASHSCOPE_API_KEY`、阿里云审核 AK/SK、`HAILIANG_AUDIT_ENCRYPTION_KEY`、`HAILIANG_SECURITY_QUARANTINE_KEY`、`HAILIANG_SECURITY_ADMIN_TOKEN` 及 PostgreSQL/Redis 凭据。审计密钥轮换需保留历史密钥解密能力；隔离证据密钥与审计密钥分开管理。

### 三、持久化卷和日志采集支持

请按用途确认是否提供独立 PVC、访问模式（RWO/RWX）、StorageClass、快照/备份、容量告警和 UID/GID 1000 写权限。项目目录里有些“logs”实际会保存用户档案/事实、会话快照或 SSE 记录，必须先确认关闭配置和迁移情况，再定义清理策略。

建议把目录拆开挂载，以下路径只是待评审目标：

| 容器目录 | 建议数据类型 | 存储性质 |
|---|---|---|
| `/opt/app/logs` | 程序结构化日志或必要的本地 JSONL | 最好以 stdout/stderr 为主；如平台要求文件采集，则按 Pod 分目录，避免共享文件轮转冲突 |
| `/data/state` | 安全隔离证据、工作台 SQLite 回退数据、物化业务包等 | 先确定哪些是权威数据、哪些能由 PostgreSQL 重建；不要整体当缓存 |
| `/data/runtime` | Runtime 会话/记忆文件、Skill 沙箱依赖缓存、workspace 等 | 按内容拆分；会话/产物与缓存采用不同清理和备份策略 |
| `/data/tasks`（若采用独立执行服务） | 任务输入、输出和短期产物 | 需确认 Pod 间访问方式及清理 TTL；共享访问也要按任务隔离目录 |

当前 `HAILIANG_STATE_DIR` 默认到项目 `runtime/`；`HAILIANG_RUNTIME_DIR` 在 Runtime 配置中单独解析；`HAILIANG_LOG_DIR` 默认项目 `logs/`。部署时应显式给三者绝对路径。安全隔离证据还可通过 `HAILIANG_SECURITY_QUARANTINE_DIR` 指定。

生产启用 PostgreSQL 并不自动证明本地文件都已废弃。请和后端一起确认：是否有现存 file/SQLite 数据、Harness 是否运行在本地 SQLite 回退、会话/记忆的真实读写位置、安全隔离目录、SSE 录制是否开启。多副本部署前必须保证权威状态放在共享数据库/服务，或提供可证明的共享持久化方案。

日志请至少采集 stdout/stderr、应用请求日志、Harness/脚本任务事件和容器退出信息。建议保留 `request_id/trace_id/session_id/run_id/user_id/profile_id`、Pod 名、应用版本、环境、耗时、状态码等字段；不要采集密钥、完整对话或未脱敏脚本输入。请确认敏感字段屏蔽、访问授权、保留期限和告警渠道。现有 `deploy/logrotate/` 是宿主机 JSONL 示例，不能自动视为 K8s 日志策略。

### 四、项目容器和 Kubernetes 工作负载建议

请平台方确认 Service、Ingress、探针、优雅终止及资源管理规则。以下是建议讨论的部署单元，不表示当前仓库已具备对应镜像或清单：

| 工作负载 | 说明 | 需要确认 |
|---|---|---|
| API/Runtime Deployment | 对外 FastAPI，提供 API v1/v2/v3、workbench 和 deployment 路由；长连接 SSE | Service/Ingress 路由边界、起始副本、Pod 资源、每 Pod Uvicorn Worker 数；工作台管理路由是否另加鉴权或单独暴露 |
| Harness Worker Deployment | `python -m hailiang_skills.harness.worker`，从 PostgreSQL 领取/推进长任务，Redis 仅做唤醒提示 | 明确 API 是否关闭内嵌 Worker（`HAILIANG_HARNESS_EMBEDDED_WORKER=false`）并运行独立 Worker；Worker 副本/并发/租约参数由后端压测和确认。不可让每个 API Pod 都无意启动完整内嵌 Worker |
| 前端静态资源 | `frontend/` 用 npm 构建，产物通过静态服务器或平台 CDN/Nginx 发布 | 是否由运维统一托管；API base URL、工作台 base URL、SPA fallback 和 CORS 路由 |
| 数据库迁移 Job | 发布版本中包含 Alembic migrations | 由流水线使用独立身份/Job 执行；应用 Pod 启动时不得每副本并发迁移 |
| 清理/维护任务 | 任务产物、临时 workspace、过期日志/审计等按策略清理 | 先由后端逐项定义数据归属、保留期和删除条件；不直接删除 PVC 全目录 |

API 已提供 `/health/live` 和 `/health/ready`。存活探针用 HTTP GET `/health/live`；就绪探针用 HTTP GET `/health/ready`。后者检查 PostgreSQL/Redis、审计初始化、模型限流器、Harness 存储和专家 Runtime；冷启动时间需现场测量，避免 readiness 过早或永不就绪。`/health` 会返回更多运行诊断字段，不建议直接公开给外网。

SSE 要求 Ingress/Gateway 关闭响应缓冲，读超时大于项目允许的最长流时长，支持客户端断连传播；请提供相关注解/参数和网关配置示例。滚动升级需给当前连接和任务留排空时间，配套 preStop/停止接单逻辑；单纯增大 `terminationGracePeriodSeconds` 不会自动排空 SSE/Harness 任务。

### 五、Skill 脚本执行需运维与后端共同评审

当前共享 Runtime 核心通过 Docker 沙箱执行 Skill 脚本，并可能在沙箱内安装各 Skill 的 `scripts/requirements.txt`。如果 API 变成普通 K8s 容器，这段逻辑不会自动转成容器内临时目录执行；它可能仍需要 Docker daemon、镜像和宿主机可见挂载路径。请运维明确集群是否允许业务 Pod 访问专用 Docker 执行环境，以及安全审查要求。不要默认挂载宿主 Docker socket。

可评审的方向：

1. 接入专用脚本执行环境，保留逐任务容器沙箱行为。需要运维提供受控 Docker/执行平台接口、镜像仓库、挂载或文件传输约定、额度和安全边界。
2. 开发独立常驻 Worker，在容器内用子进程执行脚本。需要后端实现任务 API/队列、限制、文件隔离、超时取消、幂等、崩溃恢复和依赖版本管理；运维提供独立 Deployment、资源配额、存储和出口网络规则。子进程/临时目录不等同恶意代码安全沙箱。

选择方案前，先确认 Skill 的信任边界、动态依赖清单、是否需要 OCR/GPU/外网、典型/最大 CPU/内存/时长。若使用固定基础镜像，应规定可用依赖版本并给 Skill 开发人员文档；新依赖走镜像版本发布，不能让生产任务无限制运行 `pip install`。如仍允许每个 Skill 自带依赖，需评估包源可用性、安装供应链和任务启动耗时。

### 六、配置映射和安全要求

运维模板使用 `LOG_DIR`、`APP_NAME`、`PROFILE` 等变量；项目主要读取 `HAILIANG_*`，且当前后端没有统一自动读取挂载在 `/opt/.env` 的代码。请后端与运维约定：ConfigMap/Secret 以环境变量注入，或明确实现 dotenv 入口；不要只挂载文件就假设变量自动生效。

至少需要按环境设置以下非密钥参数：

- `HAILIANG_DEPLOY_ENV`：项目允许 `test`、`prod` 及 `test-xxx`/`prod-xxx`，没有 `dev`；若平台用 `PROFILE=dev`，需明确映射为 `test-dev`。
- `HAILIANG_RELEASE_VERSION`、`HAILIANG_NODE_NAME`：分别注入镜像版本和 Pod 名。
- `HAILIANG_STORAGE_BACKEND=postgres`、`HAILIANG_DATABASE_URL`、`HAILIANG_REDIS_URL`、`HAILIANG_REDIS_KEY_PREFIX`。
- `HAILIANG_STATE_DIR`、`HAILIANG_RUNTIME_DIR`、`HAILIANG_LOG_DIR`、`HAILIANG_SECURITY_QUARANTINE_DIR`。
- `HAILIANG_HARNESS_NEW_SESSIONS`、`HAILIANG_HARNESS_EMBEDDED_WORKER`：根据灰度和工作负载拓扑显式设置。
- `HAILIANG_MAX_SSE_CONNECTIONS`、`HAILIANG_MAX_SSE_PER_USER`、`HAILIANG_STREAM_WORKERS`、`UVICORN_WORKERS`：先以压测结果设值；当前代码默认 SSE 100、每用户 3，生产 env 样例 Stream Workers 50、Uvicorn Workers 2。
- `HAILIANG_LLM_RATE_LIMIT_QPS`、`HAILIANG_LLM_RATE_LIMIT_CAPACITY`：现有为 QPS 限流，不是 TPM 计费控制。
- `HAILIANG_BUSINESS_CONFIG_SOURCE`：确认数据库发布快照和文件系统基线策略。
- `HAILIANG_AUDIT_RAW_CONTENT_ENABLED=false`、SSE 录制策略、审计密钥 ID、审核 region/endpoint、OTel endpoint。

模型配额需单独确认。曾提供的供应商总额为 15000 RPM、1200000 TPM；此前讨论过项目暂按 50% 分配并以 80% 准入（6000 RPM、480000 TPM），这只是待确认预算。代码当前的 45 QPS 参数不能实现 TPM 预留/结算。请模型平台确认本项目可用额度、其他应用共享预算和限流口径，再由后端设计全局跨副本 RPM/TPM 限额。

### 七、镜像构建与流水线交付

请运维确认镜像构建平台是否可从仓库根目录构建，并纳入 `agent_skill_runtime_core/` 共享核心。镜像需包含：主 Python 包、配置和词库、`runtime_skills/`、`runtime_agents/`、`runtime_agent_teams/`、必要模板资源及 Alembic migrations。另需构建前端资源；是否同镜像由平台路由方式决定。

依赖声明在 `pyproject.toml`，Python 版本要求 `>=3.11`，现有脚本按 Python 3.11 和旧 Linux wheel 兼容经验安装。当前 requirements 使用若干范围版本，`constraints/linux-legacy-ms-agent.txt` 是兼容约束，不是完整锁文件。请确认目标架构和基础镜像后，由后端生成可复现的 Linux 依赖锁并验证关键二进制扩展；失败应阻止镜像发布，不应静默降级依赖。

流水线建议保留运维原参数：开发/测试用 `SNAPSHOT-${BUILD_NUMBER}`，生产用 `${BUILD_NUMBER}`；保留 APP/镜像/资源参数。渲染 K8s 模板后校验无 `${...}` 残留、YAML 可解析、Secret 引用存在、镜像 tag 与 BUILD_NUMBER 一致、PVC/namespace/资源参数齐备，再提交 API Server。部署步骤拆分构建、镜像扫描、模板渲染/校验、迁移 Job、滚动发布、探针检查、回退。

镜像入口应以前台方式运行，容器监听 `0.0.0.0:8080`（如平台端口约定为 8080）；配置、日志路径与探针保持一致；生产 UID/GID 1000。请运维提供期望的 requests/limits 起始范围以及 startup/liveness/readiness 参数模板。无实测前不在本文指定 CPU/内存数值或宣称 2 副本/200 并发已满足业务峰值。

### 八、请运维回复的确认项

请优先回复下面内容，未确定的标“待定”并注明负责人/时间：

1. K8s 集群版本、namespace、节点架构及 Python 基础镜像地址。
2. 镜像 Registry/namespace、拉取 Secret、CI/CD 参数名和模板渲染工具。
3. 测试/生产数据库与 Redis 是否由平台提供；TLS、连接数、备份、监控及网络 DNS。
4. Secret 管理流程；API/模型/审核服务的出网白名单。
5. PVC StorageClass、RWO/RWX 支持、容量和备份；日志采集和保留策略。
6. Ingress SSE 配置（缓冲、读超时、断连处理）与公网/内网域名、TLS、BFF 路由。
7. Pod UID/GID、资源 requests/limits、滚动升级和优雅终止规范。
8. Docker 沙箱是否允许接入；如不允许，请安排运维与后端评审替代执行架构。
9. 是否接受 API 与 Harness Worker 分开部署；建议由后端先确认内嵌 Worker 设置与容量模型。
10. 数据库迁移 Job、回滚和灾备演练的流水线责任人。

## 项目当前实现依据（便于运维/后端快速核对）

| 主题 | 文件 |
|---|---|
| Python 依赖 | `hailiang-skills/pyproject.toml`、`hailiang-skills/constraints/linux-legacy-ms-agent.txt` |
| API 路由、探针和 Harness Worker 启动 | `hailiang-skills/src/hailiang_skills/api/main.py`、`api/routes/harness.py` |
| 独立 Harness Worker 入口 | `hailiang-skills/src/hailiang_skills/harness/worker.py`、`hailiang-skills/deploy/systemd/hailiang-skills-harness@.service` |
| PostgreSQL/Redis | `hailiang-skills/src/hailiang_skills/storage/factory.py`、`storage/database.py`、`core/concurrency.py`、`core/rate_limit.py` |
| Harness 任务持久化和 Redis 唤醒 | `hailiang-skills/src/hailiang_skills/harness/store.py`、`harness/wakeup.py`、`harness/worker.py` |
| 日志/目录/遥测 | `hailiang-skills/src/hailiang_skills/core/deployment.py`、`core/request_logging.py`、`core/session_logging.py`、`core/telemetry.py` |
| Skill 沙箱 | 仓库根 `agent_skill_runtime_core/agent_skill_runtime_core/core.py`、`hailiang-skills/src/hailiang_skills/runtime_bridge/ms_agent_adapter.py` |
| 当前部署配置样例 | `hailiang-skills/deploy/env/prod.env.example`、`prod-next.env.example`、`deploy/systemd/`、`docker-compose.yml` |
| 前端构建及配置 | `hailiang-skills/frontend/`、`hailiang-skills/deploy/bin/build-frontend.sh`、`deploy/systemd/hailiang-skills-web@.service` |
| 数据库迁移 | `hailiang-skills/alembic.ini`、`hailiang-skills/migrations/versions/`（当前工作区含 0001–0010 迁移文件；发布前核对数据库实际 revision） |
