# Core Execution Event 契约（P1-CLEAN-STATE-01）

> 状态：Normative。ADR：[ADR-0005](../adr/ADR-0005-core-typed-execution-events.md)。
> 实现：`map_core/map_core/service/execution_event.py`；
> BFF 映射：`map-business-backend/app/runs/typed_core_stream.py`。

## 1. Event 形状

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "run_id": "uuid",
  "attempt": 1,
  "seq": 1,
  "type": "step.started",
  "occurred_at": "2026-08-30T00:00:00Z",
  "workspace_id": "uuid|null",
  "request_id": "str|null",
  "trace_id": "hex|null",
  "span_id": "hex|null",
  "data": {}
}
```

- `seq` per-run 从 1 单调递增；`stream.terminal` 必须是流最后一行。
- `data` inline ≤65536 字节且 canonical JSON 可序列化（NaN/bytes/集合拒绝）。
- `stream.terminal` 的 `data` 必含 `status ∈ {completed, failed}`，
  failed 时含 `error_code/error_message`。

## 2. type 与 BFF canonical 映射

| core type | BFF RunEvent |
| --- | --- |
| `step.started` | `step.started` |
| `step.completed` | `step.completed` |
| `step.failed` | `step.failed` |
| `message.delta` | `message.delta` |
| `tool.invocation_created` | `tool.invocation_created` |
| `tool.invocation_completed` | `tool.invocation_completed` |
| `tool.invocation_failed` | `tool.invocation_failed` |
| `model.invocation_*` | 同名 |
| `checkpoint.written` | `checkpoint.written` |
| `effect.*` | 同名 |
| `stream.terminal` | 映射为 `CoreOutcome(completed|failed)` |

未知 type/坏 JSON/EOF 无 terminal 由 BFF 投影为 `CoreError`（attempt retry）。

## 3. 传输

```
POST /internal/v1/runs/{run_id}/attempts/{attempt}/events
Authorization: Bearer <service-token>   # scope runs.execute
X-Workspace-ID / X-Request-ID 一致性校验
body: GlobalDomainChatSchema
response: application/x-ndjson
```

- 401 missing/expired/坏 token；403 audience/scope 错；400 path/header/body 不一致。
- 执行异常仍是 200 NDJSON，terminal `failed`；transport 错误是 HTTP/连接层失败。

## 4. RunContext 与诊断

- 请求边界设置 `RunContext`；caller 使用 `ExecutionEventEmitter.current()`，
  不得再传递 `state_store/state_id/base_state`。
- `OtelEventProjector` 把事件元数据写入当前 span（不写全量 data）；
  旧 Mongo telemetry 已停写，collection 的 drop 受 retention runbook 约束。
