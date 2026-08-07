# SSE 模型错误码速查

> 字段含义、终态处理、HTTP 建流前错误和前端展示规则以
> [SSE_RESPONSE_CONTRACT.md](SSE_RESPONSE_CONTRACT.md#5-风控与错误) 为准。

当 SSE 连接已建立后，模型相关错误通过 v2 状态快照的 `error` 返回。所有错误均包含
`code`、面向用户的 `message`、脱敏后的 `upstream_detail`、`retryable` 与 `terminal`。
其中 `terminal=true` 表示本轮以 `status=failed` 结束；`terminal=false` 表示辅助调用失败，正文可继续完成。

| 错误码 | 含义 | retryable |
| --- | --- | --- |
| `MODEL_UNAVAILABLE` | 模型服务未配置或暂不可用 | 是 |
| `MODEL_TIMEOUT` | 模型响应超时 | 是 |
| `MODEL_AUTHENTICATION_FAILED` | 模型服务认证失败 | 否 |
| `MODEL_RATE_LIMITED` | 模型服务限流 | 是 |
| `MODEL_INVALID_RESPONSE` | 模型返回内容无法解析 | 是 |
| `MODEL_STREAM_INTERRUPTED` | 模型流连接中断 | 是 |
| `MODEL_UPSTREAM_ERROR` | 模型上游服务错误 | 是 |
| `MODEL_RUNTIME_ERROR` | 未分类模型运行错误 | 是 |

请求校验、身份冲突和重复 run 等发生在 SSE 建连前，继续使用 HTTP `4xx/429`，不包装为流帧。
