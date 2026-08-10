# 会话/流式/幂等契约（FIX-P2-CONTRACT-E2E-01）

> 对应整改：FIX-P1-CONV-01。

## 1. 数据模型

- `conversations(id, workspace_id, owner_user_id, mode(global|flow), title, status, created_at, updated_at, last_message_at, version)`；所有权 = `(workspace_id, owner_user_id)`。
- `messages(id, conversation_id, workspace_id, role, status(pending|streaming|completed|failed|stopped|suspended), content, request_id, task_id, decision_json, config_snapshot_id, stream_error, error_message, fallback_used, created_at, updated_at, completed_at, version)`。
- assistant 消息 `request_id` 唯一（partial index）；同一 `(workspace, owner, conversation, request_id)` 重放不创建第二条。

## 2. API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/conversations` | 创建（支持 `Idempotency-Key`；同键同 body 重放返回原会话，不同 body 409 `IDEMPOTENCY_CONFLICT`） |
| GET | `/api/v1/conversations` | 当前用户会话列表 |
| GET | `/api/v1/conversations/{id}` | 详情 + 消息（刷新恢复） |
| POST | `/api/v1/conversations/{id}/messages:stream` | 流式（SSE） |
| POST | `/api/v1/messages/{id}:stop` | 停止（abort + 条件终态更新） |

## 3. SSE 事件集（冻结）

`start` / `meta` / `content_delta` / `done` / `error`。`message.started` 等非冻结事件不得再出现；message IDs 在 `start` 事件携带。

- `start`：`{conversation_id, message_id, user_message_id}`
- `content_delta`：`{content}`（增量）
- `done`：`{message_id?, content, status(completed|failed|stopped), task_id?, replayed?}`
- `error`：`{error, code?, fallback?}`

## 4. 状态机

```
pending -> streaming -> completed | failed | stopped
```

- 只有合法 `done` 可进入 `completed`；EOF 无 done → `failed` + `stream_error=STREAM_EOF_WITHOUT_DONE`。
- 解析错误 → `STREAM_PARSE_ERROR`；非法 UTF-8 → `STREAM_DECODE_ERROR`；core error → `STREAM_CORE_ERROR`（`fallback_used` 独立持久化，不覆盖错误事实）；客户端 abort/stop → `STREAM_ABORTED`；reconciler → `STREAM_INTERRUPTED`。
- 终态写为条件更新（`WHERE status='streaming'`）：stop 与 done 竞态只允许一个终态。

### 4.1 stop 与 StreamRegistry 生命周期（R2-P1-01）

- 每条流式消息在创建上游流**之前**注册进 `StreamRegistry`，注册态覆盖上游 pump、chunk 消费、parser close、finalize 全程，仅在最外层 `finally` 注销；任何退出路径（done/error/stop/disconnect）不得泄漏注册项，流结束后 `active_count` 恒为 0。
- registry 条目 = abort event + 上游消费 task 句柄。`POST /messages/{id}:stop` 是**主动取消**：置 abort event 并 cancel 上游消费 task，core 的 HTTP response 在 generator finally 中立即关闭，stop 后 core 不再产生工具调用等副作用；不是等到下一个 chunk 才察觉。
- 消费端每个 chunk 与 abort 信号竞态（`asyncio.wait` FIRST_COMPLETED）：core hang、无新 chunk 时 stop 也能在超时内完成取消。
- stop 响应携带 `abort` 字段（registry 命中即 true），并记录结构化日志（registry_hit/db_finalized/prior_status）。
- registry 是进程内的：多实例部署需按 message_id 同实例路由（sticky）或替换为共享取消通道；stop 端点的条件终态写在任一情况下都保持正确。
- stop/done 竞态：终态唯一，且败方（losing side）不得继续执行上游消费。

## 5. 幂等与并发

- 并发相同 `request_id`：唯一约束冲突被捕获 → 安全 re-query → 只返回一对消息；不暴露 500、不留孤儿 user message。
- 跨用户/跨会话使用他人 `request_id`：404，绝不返回内容。

## 6. Reconciler

- worker job `message_reconcile`：`streaming` 且 `updated_at` 超阈值（默认 300s）→ `failed` + `STREAM_INTERRUPTED`；条件更新幂等。

## 7. 前端

- feature flag `VITE_MAP_CONVERSATIONS_ENABLED=true` 启用新会话视图（创建/刷新恢复/停止/反馈）；默认保持旧 `/api/chat*` 路径。
