# TODO

## 分享会话后继续追问（Fork 会话）

- [ ] 在 `POST /api/v1/sessions/chat/stream` 请求体增加可选顶层字段 `share_session_id`。
- [ ] 约定 Fork 触发条件：`share_session_id` 有值且传入的 `session_id` 不存在时，创建接收方的新会话并在首轮消息前完成上下文 Fork；未传该字段时保持现有创建/恢复会话逻辑不变。
- [ ] 若携带 `share_session_id` 的 `session_id` 已存在，返回 `409 SHARE_FORK_SESSION_ALREADY_EXISTS`；若新会话 ID 等于源会话 ID，同样拒绝。
- [ ] `share_session_id` 必须指向一份不可变、版本化的分享快照，而不是只引用可继续变化的源会话；校验快照存在、未过期、未撤销，且当前接收方拥有访问权限。
- [ ] 转发端在用户点击“继续对话”时生成新的 `session_id`，并在首个 chat 请求中传递：接收方 `user_id`、`profile_id`、`student_name`、新的 `session_id` 与 `share_session_id`。
- [ ] Fork 时创建的会话归属接收方账号；写入来源元数据，例如 `source_share_session_id`、`source_session_id`、`snapshot_version`、`forked_at`，用于审计和排障。
- [ ] Fork 快照只复制：对话摘要、关键历史消息、已经确认的关键事实、来源 Skill/讨论主题；不复制源会话 ID、运行中的 SSE run、互动表单状态、Skill 中间状态或分享者的 shared Facts。
- [ ] 当前 PostgreSQL `advisor_sessions.payload` 已保存 `messages`、`skill_states`、`interaction_state`、Facts、候选路径及会话元数据。Fork 应从不可变分享快照读取这些字段，而不是依赖仍可能变化或被删除的源会话。
- [ ] Fork 后保留源会话的 `interaction_state.active_skill`，使接收方从分享时所在的 Skill 继续追问；不要按普通新会话流程强制重置为 `general_chat`。
- [ ] Fork `skill_states` 时仅复制可恢复的业务进度（当前专题、已确认问卷答案、已收集信息、对话记忆/摘要）；深拷贝后清理并重新生成临时运行数据，如 `session_id`、SSE `run_id`、流锁、回调、消息互动 ID、未提交表单及其他一次性状态。
- [ ] 复制的历史消息只用于历史回显与模型理解；其中未完成的 assistant 输出、交互表单/按钮状态不得在接收方会话中继续复用，应按新会话状态重新生成。
- [ ] 将分享上下文单独保存为 `share_context`（例如放在 `session_meta` / runtime memory），并标记 `source=shared_conversation`、`pending_confirmation=true`；不要直接写入接收方长期 `profile_facts` 或 `shared_facts`。
- [ ] 组装模型上下文时明确分区：`当前用户自身档案`、`分享对话参考信息（未经当前用户确认）`、`当前用户本轮问题`。优先级：当前用户本轮明确陈述 > 当前用户自身 Facts > 分享快照事实 > 分享历史建议。
- [ ] 提供“采用到我的档案”交互：接收方显式确认某些分享事实后，才按字段写入其 profile/shared Facts；接收方的新陈述只更新接收方 Fork 会话，不影响分享方原会话。
- [ ] 接收方默认先只读查看分享内容；点击“继续对话”后才创建 Fork 会话，保证分享方原会话及其历史记录始终独立保留。
- [ ] 增加测试覆盖：正常 Fork、普通 chat 不受影响、会话 ID 重复、分享无权限/过期/撤销、源会话删除后快照仍可 Fork、接收方新 Facts 不会写回分享方。
