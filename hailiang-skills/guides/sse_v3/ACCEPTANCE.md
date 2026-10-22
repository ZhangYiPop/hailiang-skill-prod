# SSE v3 实施与验收记录

初始记录日期：2026-10-19。后续产品切换已将 `HAILIANG_SSE_V3` / `VITE_HAILIANG_SSE_V3` 设为本地与 8011 模板默认开启；SSE v2 接口仅保留给历史调用方。

## 已交付

- 公开实体事务投影、有序 outbox、按身份/会话/档案隔离的游标与快照、旧运行快照引导。
- v3 启动幂等、独立回答/取消、实体更新与 Unicode 正文增量；本地聊天和调试台共用恢复订阅。
- 显式 finalize_response 后真实流式调用；片段检查、最终风控撤回、取消/失效 token 拒绝、重启新 revision。
- 协议、状态表、Schema、完整多专家/多轮追问/部分回答/跳过样例、BFF 本地参考及分片测试。
- PostgreSQL 0010 迁移和 SQLite 建表；修正历史 0001 按当前 metadata 提前创建后续 Harness 表导致空库升级失败的问题。

## 验证

- SQLite + SSE v2 + Harness/API/追问/资产 + v3/BFF 定向回归：148 passed，1 个依赖弃用警告。
- PostgreSQL Harness/v3/上下文/执行协议：72 passed；补充旧运行引导、回滚与迁移降升后的 v3 专项：10 passed。
- PostgreSQL 在独立 harness_test 数据库验证从空库升级到 head，并验证 0010 → 0009 → 0010；没有对业务数据库执行迁移。
- 前端 reducer 与表单：9 passed；TypeScript 检查通过。完整生成事件在前端重放后与后端快照逐实体相等。
- 模型流式客户端专项：5 passed。

## 未通过的发布条件

扩大运行 test_runtime_bridge.py 后，旧 Skill 路由/元数据及旧主路由断言存在多项失败，并伴随既有 sandbox 后台退出日志。代表性失败：subject_advisor.routing.scene_name 实际为空，断言要求“选科参谋”；该测试、skill_registry、skill_loader、main_planner 和 runtime_skills 资产与 HEAD 均无修改。本轮未完成全部旧桥接失败的归因，也未为通过旧断言修改业务路由。因此不能宣称“全量回归通过”，保持 v3 默认关闭。

真实供应商模型与实际业务 BFF 灰度回放未完成。自动测试使用模型替身，但调用真实 runtime、持久化和流式客户端接口；不能替代真实模型指令遵循与首字延迟验收。BFF 交付范围是文档、可运行参考和模拟联调，不含生产登录态改造。

## 成本测量

measurement.json 是同一虚构多专家场景的本地模拟对照，记录调用次数及 elapsed_ms。v3 显式最终生成比基线多 1 次主 agent 调用；样例使用固定模拟正文生成延迟，不代表供应商 token、真实模型时延或质量收益。生成脚本可重跑，不应将其耗时当作生产性能承诺。

## 操作

在原项目 hailiang-skills 内设置两个开关后执行 ./run.sh，启动脚本会运行 Alembic。新建对话验证；关闭两个开关回退，保留 public 表和运行记录。迁移降级仅用于隔离测试，已有业务数据环境回退入口时不要删除表。

## 2026-10-20：公开有限流与问题组收敛

- 对外 SSE 契约固定为完整快照帧：`state`、`suspended`、`timeout`、`done` 与 `ping`；`done` 是唯一对话记录落库信号。
- 追问增加持久化 `input_groups` 和 `input_queue` 投影：首题立即活动，后续问题按组排队，组内一次只展示一张卡片。
- 相同事实身份且约束兼容的跨专家问题共享 binding；部分重叠表单会复用已承接字段的回答或跳过状态。
- 更新公开协议、BFF 接入和 cURL 手册；细粒度 outbox 样例保留作内部诊断，不再描述为浏览器/BFF wire 流。
- 定向验证：Harness/SSE v3/执行协议 51 项通过，前端 TypeScript 检查通过。该数字只代表本次定向回归，不替代上文的历史全量基线和真实供应商/BFF 灰度验收。
