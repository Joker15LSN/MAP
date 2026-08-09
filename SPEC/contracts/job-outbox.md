# Job / Outbox 契约（FIX-P2-CONTRACT-E2E-01）

> 对应整改：FIX-P0-WORKER-01 与 F-03。

## 1. jobs 状态机

`queued -> running -> succeeded|failed|cancelled`；可重试失败回到 `queued` 并递增 `attempt`。

- 每次 claim `attempt+1`，作为 fencing token；heartbeat/complete/fail 均带 `lease_owner + attempt` 条件，失去 lease 的 worker 无法提交结果或执行新副作用。
- lease 到期（`lease_expires_at < now()`）的 `running` job 可被其他 worker 在带锁事务内单个回收（无批量重置）。
- heartbeat 独立短事务提交，间隔 < lease/3 带 jitter；DB 超时按 lease 丢失处理（fail-closed）。
- handler 通过 `get_current_job_context()` 读取 `lease_ok`（lease_lost/cancel）；SIGTERM 传播为 cancel 并等待安全点。
- 外部副作用 handler 使用稳定 `idempotency_key` 去重，重放不产生第二次外部动作。

## 2. 已注册 job 类型

| job_type | handler | 说明 |
| --- | --- | --- |
| `message_reconcile` | `_message_reconcile_handler` | 把超时 `streaming` 消息标为 `failed/STREAM_INTERRUPTED`（幂等） |

## 3. outbox_events

`id, aggregate_type, aggregate_id, event_type, payload_json, available_at, claimed_at, delivered_at, attempt, last_error`

当前写入方：feedback 撤回（`message_feedback` / `feedback_withdrawn` tombstone）。

## 4. 运维

- worker drain：SIGTERM → 日志 `worker ... stopped gracefully` → 再升级/回滚。
- 运行中 job 清点：`SELECT status, count(*) FROM map_control.jobs GROUP BY status;`，过期 lease 行下个周期被回收。
- 应用回滚：先 drain worker 再回滚镜像；job 数据保留在 PostgreSQL。
