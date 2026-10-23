# Skill 脚本沙箱方案：架构决策与使用边界

> 适用范围：Harness、传统 Runtime Skill 与原生工具脚本。本文记录 2026-10-22 采用当前方案的原因，供后续 AI、开发者、运维及业务人员理解和维护。

## 结论

Skill 脚本统一通过独立的 `skill-sandbox-runner` 执行：

```text
API / Harness（裸机或 K8s Pod）
  → RemoteSandboxExecutor（HTTP + 共享 token）
  → 常驻 skill-sandbox-runner
  → 冻结 Skill 包中的 scripts/*.py
```

生产 K8s 使用 Runner Deployment + ClusterIP Service；本地开发和裸机 smoke 测试使用同一 Runner 镜像的 Docker Compose 服务。API 进程、宿主机和 Docker socket 均不是 Skill 脚本的执行位置。

## 为什么选择这个方案

### 需要同时满足的约束

- 业务人员可上传或更新 Skill 脚本，API 不应直接执行不受信任的业务代码。
- 正式环境为 K8s，测试环境和本地常是裸机 API；脚本行为需要尽量一致。
- Skill 包发布后必须冻结：会话执行的是本轮锁定的版本，不能因为后来发布同 ID 的 Skill 而改变结果。
- 多轮对话中可能多次调用脚本，不能每次都重新创建容器、安装依赖。
- 仍需要诊断脚本输入、输出、错误、缓存命中和耗时。

### 被排除的方案

| 方案 | 不采用原因 |
| --- | --- |
| API Pod / 裸机直接 `subprocess.run` | 业务脚本可读取 API 进程环境、接触业务依赖；K8s 与裸机行为不一致。 |
| API 容器挂载 Docker socket，再临时 `docker run` | Docker socket 等同宿主机高权限；每次冷启动开销大，K8s 也不适配。 |
| 每次脚本执行创建一个 K8s Job | 隔离较强，但调度、镜像拉取和 Job 生命周期延迟高，不适合对话实时链路。 |
| 只在 API 内实现“逻辑隔离” | 不能形成可靠的进程、文件系统、网络和凭据边界。 |

## 当前机制

### 冻结包与执行输入

API 每次需要执行脚本时，向 Runner 发送：

- 冻结的 Skill 包归档及 SHA-256 摘要；
- Skill ID、修订/发布版本标识；
- 仅允许的 `scripts/` 下相对 Python 路径；
- 结构化 JSON 输入、超时和 trace ID。

Runner 会校验包摘要、归档大小、路径和静态危险调用，再在临时工作目录中解压并执行。脚本不能通过绝对路径、`..` 路径或任意 shell 命令要求 Runner 执行其它文件。

### 依赖哈希缓存

依赖环境键为：

```text
Runner 镜像标识 + Python 主次版本 + 冻结包摘要 + requirements.txt 摘要
```

缓存未命中时，Runner 创建隔离 venv，并仅从 `HAILIANG_SANDBOX_PYPI_INDEX_URL` 安装固定版本依赖；成功后原子写入缓存。命中时直接复用。

因此以下任一变化都会自动新建环境，而不会污染旧环境：Skill 包内容、`requirements.txt`、Python 版本或 Runner 镜像版本。

`requirements.txt` 只允许固定版本的普通包，例如 `pandas==2.2.3`；不允许 VCS、可编辑安装、本地路径、自带 index URL 或未固定版本。

### 部署拓扑

| 场景 | API | Runner | 配置重点 |
| --- | --- | --- | --- |
| 本地 `./run.sh` | 裸机 Python | Docker Compose | 默认 `127.0.0.1:8091`，本地开发 token。 |
| 裸机 `deploy-smoke.sh` | 裸机 Python | Docker Compose 或外部 Runner | `env.8011.sh` 必须设置私有共享 token。 |
| K8s | API Deployment | Runner Deployment + ClusterIP Service | Secret 共用 token、非 root、只读根文件系统、NetworkPolicy。 |

API 必须设置：

```bash
export HAILIANG_SCRIPT_EXECUTOR=remote
export HAILIANG_REQUIRE_REMOTE_SANDBOX=true
export HAILIANG_SANDBOX_ENDPOINT=http://<runner-address>:8081
export HAILIANG_SANDBOX_SHARED_TOKEN='<private-random-token>'
```

`HAILIANG_REQUIRE_REMOTE_SANDBOX=true` 是防误配置的关键：远端 Runner 缺失时 API 应启动失败或脚本受控失败，而不是静默退回宿主机执行。

## 性能特征

在本机用真实 `guess-profession/scripts/pick_profession.py` 测得：

| 场景 | 调用端总耗时 | Runner 内部总耗时 | 缓存 |
| --- | ---: | ---: | --- |
| 新冻结包首次执行（无第三方依赖） | 约 3.58 秒 | 约 3.44 秒 | 未命中 |
| 同一冻结包第二次执行 | 约 193 ms | 约 181 ms | 命中 |
| 已有镜像的 Runner 重启到健康就绪 | 约 2.0 秒 | 不适用 | 容器启动 |

首次执行包含包传输、摘要校验、解压、venv 创建和脚本子进程启动。若 Skill 有第三方依赖，首次还包含 pip 下载/安装，耗时取决于依赖体积和镜像网络；后续同一环境键命中缓存后不再安装。

首次构建 Runner 镜像属于部署成本，不属于每轮脚本调用。Runner 是常驻服务；不会为每一次对话脚本调用创建容器。

## 优势

- **隔离更清晰**：业务脚本不在 API Pod、宿主机或拥有 Docker socket 的环境中运行。
- **环境一致**：本地、裸机 smoke 与 K8s 使用同一 HTTP 协议和 Runner 镜像。
- **版本正确性**：以冻结 Skill 包和摘要执行，避免同 ID 新发布版本影响进行中的会话。
- **性能可接受**：常驻 Runner 与哈希缓存避免“每轮拉镜像、建容器、装依赖”。
- **可追溯**：执行 ID、环境键、缓存命中、退出码和 stdout/stderr 摘要可进入诊断轨迹。
- **可演进**：未来可以独立扩展 Runner 副本数、并发上限、资源限制和队列策略，不必扩容 API 来承载脚本。

## 劣势与已知边界

- **首次调用较慢**：新冻结包或依赖变化会触发 venv/安装；业务侧不应把首次调用当作毫秒级操作。
- **增加运维对象**：本地/测试要有 Docker Runner，K8s 要维护 Service、Secret、NetworkPolicy、资源限制与内部 PyPI。
- **缓存不跨 K8s Pod**：当前 K8s 每个 Runner Pod 使用 `emptyDir`，扩容或调度到新 Pod 后会重新准备环境；这是用隔离性换共享缓存复杂度的取舍。
- **Python 依赖受限**：未固定、私有但无内网源、需要编译工具链或需要系统库的依赖，可能安装失败；应在发布校验和镜像基线中提前处理。
- **并非任意代码执行平台**：网络默认拒绝，禁止 shell/任意路径/危险模块；需要外部网络、数据库或平台密钥的业务不应直接写成脚本。
- **当前耗时粒度有限**：Runner 已记录总 `duration_ms` 与缓存命中，但尚未分别上报“解压、依赖准备、脚本本体、清理”各阶段耗时；需要细粒度性能归因时应补充 phase timings。

## 后续 AI / 开发者操作准则

1. 不要恢复 API 内 `subprocess.run`、本机 Python 或 Docker socket 作为生产/测试脚本回退路径。
2. 新增脚本调用点时，必须复用 `SandboxExecutor` / `execute_remote_skill_script`，不能自行发 shell 命令。
3. 只执行冻结包中 `scripts/` 下的 Python 文件；脚本输入由服务端组装，模型不能覆写用户、会话、孩子等受控上下文。
4. 新增依赖前确认其可被固定版本安装，并能通过配置的 PyPI 源获取；不要在 `requirements.txt` 写 `-e`、Git URL、本地路径或自定义 index。
5. 诊断“脚本慢”时，先区分 Runner 未就绪、缓存未命中、依赖安装、脚本自身慢、队列等待和最终回复未呈现，不能只看模型耗时。
6. 测试服务器私有配置使用独立 `env.8011.sh`；不得提交 token、数据库地址、API key 或其他私有环境文件。
7. K8s 生产环境优先使用公司内网 PyPI/制品代理；清华源仅是本地与裸机 smoke 的默认镜像，不应作为生产 NetworkPolicy 的公网放通目标。

## 关联文档

- [Skill Sandbox Runner 部署说明](SKILL_SANDBOX_RUNNER_DEPLOYMENT.md)
- [脚本使用规范](SKILL_SCRIPT_USAGE.md)
- `Dockerfile.sandbox-runner`
- `deploy/sandbox-runner.yaml`
