---
name: deploy-test
description: |
  将最新代码部署到测试环境（10.30.6.45:8011/5178）并重启服务。
  当用户提到"部署到测试环境"、"发布到测试"、"更新测试服务器"、"重启测试服务"、
  "deploy to test"、"发到测试"等时触发。
---

# 测试环境部署

将当前分支代码推送到 GitLab 后，同步到测试服务器并重启 API 和前端服务。

## 连接信息

| 项目 | 值 |
|------|-----|
| SSH | `sshpass -p 'Ag.#@S681' ssh -o StrictHostKeyChecking=no -p 56000 hljy@10.30.6.45` |
| 项目路径 | `/home/hljy/Project/for_1031/hailiang-agent` |
| Python venv | `hailiang-skills/.venv-smoke-8011/bin/python` |
| 环境文件 | `hailiang-skills/env.8011.sh` |

## 端口和日志

| 服务 | 端口 | 日志 |
|------|------|------|
| API (uvicorn) | 8011 | `hailiang-skills/smoke-8011.log` |
| 前端 (static) | 5178 | `hailiang-skills/smoke-frontend-5178.log` |

## 部署流程

### 第一步：确保代码已推送到 GitLab

如果本地有未推送的提交，先推送到 GitLab：

```bash
git push gitlab <当前分支名>
```

### 第二步：在测试服务器上拉取代码

服务器的 origin 已配置为 GitLab 凭据，直接 pull 即可：

```bash
sshpass -p 'Ag.#@S681' ssh -o StrictHostKeyChecking=no -p 56000 hljy@10.30.6.45 \
  "cd /home/hljy/Project/for_1031/hailiang-agent && git pull origin cj-10"
```

如果 pull 失败（凭据过期），更新远端 URL：
```bash
# 先更新凭据
sshpass -p 'Ag.#@S681' ssh -o StrictHostKeyChecking=no -p 56000 hljy@10.30.6.45 \
  "cd /home/hljy/Project/for_1031/hailiang-agent && \
   git remote set-url origin 'http://cj:glpat-udF_vCDQ_33oyhnUmYFX@10.30.5.98/eshenya-ai/hailiang-agent.git' && \
   git pull origin cj-10"
```

### 第三步：重启服务

服务器没有 `lsof`/`fuser`/`ss`，需要用 `netstat` 查找旧进程并手动 kill。

```bash
# 1. 查找并杀掉旧进程
sshpass -p 'Ag.#@S681' ssh -o StrictHostKeyChecking=no -p 56000 hljy@10.30.6.45 \
  "kill -9 \$(netstat -tlnp 2>/dev/null | grep -E ':8011|:5178' | awk '{print \$NF}' | sed 's|/.*||') 2>/dev/null; sleep 1; echo 'killed'"

# 2. 确认端口已释放
sshpass -p 'Ag.#@S681' ssh -o StrictHostKeyChecking=no -p 56000 hljy@10.30.6.45 \
  "netstat -tlnp 2>/dev/null | grep -E '8011|5178' || echo 'ports free'"

# 3. 启动 API（8011）
sshpass -p 'Ag.#@S681' ssh -o StrictHostKeyChecking=no -p 56000 hljy@10.30.6.45 \
  "cd /home/hljy/Project/for_1031/hailiang-agent/hailiang-skills && \
   source ./env.8011.sh && \
   nohup .venv-smoke-8011/bin/python -m uvicorn hailiang_skills.api.main:app \
     --host 10.30.6.45 --port 8011 --workers 1 --timeout-keep-alive 15 --no-access-log \
     > smoke-8011.log 2>&1 &"

# 4. 启动前端（5178）
sshpass -p 'Ag.#@S681' ssh -o StrictHostKeyChecking=no -p 56000 hljy@10.30.6.45 \
  "cd /home/hljy/Project/for_1031/hailiang-agent/hailiang-skills && \
   source ./env.8011.sh && \
   nohup .venv-smoke-8011/bin/python scripts/static_frontend_server.py \
     --host 10.30.6.45 --port 5178 --directory frontend/dist \
     < /dev/null > smoke-frontend-5178.log 2>&1 &"
```

### 第四步：验证

验证分三步：健康检查 → CORS 预检 → 前端可达。

```bash
# 1. API 健康检查
sshpass -p 'Ag.#@S681' ssh -o StrictHostKeyChecking=no -p 56000 hljy@10.30.6.45 \
  "curl -s --max-time 5 http://10.30.6.45:8011/health"

# 2. CORS 预检（关键！缺少这步前端会 "Failed to fetch"）
#    浏览器发送的 Origin 不带尾部斜杠，env 里 HAILIANG_CORS_ORIGINS 也不能带
sshpass -p 'Ag.#@S681' ssh -o StrictHostKeyChecking=no -p 56000 hljy@10.30.6.45 '
  ORIGIN=$(grep HAILIANG_CORS_ORIGINS /home/hljy/Project/for_1031/hailiang-agent/hailiang-skills/env.8011.sh | sed "s/.*=//; s|/$||")
  RESULT=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 -X OPTIONS \
    http://10.30.6.45:8011/api/v3/sessions/chat/stream \
    -H "Origin: $ORIGIN" \
    -H "Access-Control-Request-Method: POST" \
    -H "Access-Control-Request-Headers: Content-Type")
  if [ "$RESULT" = "200" ]; then
    echo "CORS OK (origin: $ORIGIN)"
  else
    echo "CORS FAILED: HTTP $RESULT — check HAILIANG_CORS_ORIGINS for trailing slash"
  fi'

# 3. 前端可用性
sshpass -p 'Ag.#@S681' ssh -o StrictHostKeyChecking=no -p 56000 hljy@10.30.6.45 \
  "curl -s -o /dev/null -w '%{http_code}' http://10.30.6.45:5178/"
```

### 快捷完整部署（仅重启，不重复 pull）

如果代码已经 pull 过，只需要重启：

```bash
SSH_CMD="sshpass -p 'Ag.#@S681' ssh -o StrictHostKeyChecking=no -p 56000 hljy@10.30.6.45"
PROJ="/home/hljy/Project/for_1031/hailiang-agent/hailiang-skills"

# kill old
$SSH_CMD "kill -9 \$(netstat -tlnp 2>/dev/null | grep -E ':8011|:5178' | awk '{print \$NF}' | sed 's|/.*||') 2>/dev/null; sleep 1"

# start API
$SSH_CMD "cd $PROJ && source ./env.8011.sh && nohup .venv-smoke-8011/bin/python -m uvicorn hailiang_skills.api.main:app --host 10.30.6.45 --port 8011 --workers 1 --timeout-keep-alive 15 --no-access-log > smoke-8011.log 2>&1 &"

# start frontend
$SSH_CMD "cd $PROJ && source ./env.8011.sh && nohup .venv-smoke-8011/bin/python scripts/static_frontend_server.py --host 10.30.6.45 --port 5178 --directory frontend/dist < /dev/null > smoke-frontend-5178.log 2>&1 &"

# verify
sleep 5
$SSH_CMD "echo '=== API ===' && curl -s --max-time 5 http://10.30.6.45:8011/health | python3 -m json.tool 2>/dev/null || echo 'API not ready'"
$SSH_CMD "echo '=== CORS ===' && ORIGIN=\$(grep HAILIANG_CORS_ORIGINS $PROJ/env.8011.sh | sed 's/.*=//; s|/\$||') && RESULT=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X OPTIONS http://10.30.6.45:8011/api/v3/sessions/chat/stream -H \"Origin: \$ORIGIN\" -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: Content-Type') && [ \"\$RESULT\" = '200' ] && echo \"CORS OK\" || echo \"CORS FAILED: HTTP \$RESULT\""
$SSH_CMD "echo '=== Frontend ===' && curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://10.30.6.45:5178/"
```

## 注意事项

- 第三步启动服务使用 `source ./env.8011.sh` 加载环境变量，确保使用正确的数据库和配置
- API 启动需要约 10 秒（含 Skill 加载），health check 返回后等待几秒再验证
- 前端使用 `static_frontend_server.py` 而非 vite dev server
- 服务器缺少 `lsof`/`fuser`/`ss`，进程查找用 `netstat -tlnp`
- 如果 pull 时出现 `credential-` 错误，是 harmless warning，不影响功能
- **CORS 尾部斜杠陷阱**：`HAILIANG_CORS_ORIGINS` 不能带尾部斜杠。浏览器发 `Origin: http://10.30.6.45:5178`（无斜杠），如果 env 里配了 `http://10.30.6.45:5178/`（有斜杠）则不匹配，前端所有请求报 `Failed to fetch`。部署后第四步会自动检测。