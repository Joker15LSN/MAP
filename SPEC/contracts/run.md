# Canonical Run/Event/Artifact 契约（P0-CONTRACT-01）

> 状态：Normative。ADR：[ADR-0002](../adr/ADR-0002-canonical-run-event-artifact-contract.md)。
> 状态枚举与阈值以 `TODO/acceptance-profile.yaml` 为唯一事实源；本文件是
> 状态机与 envelope 的 normative 说明。

## 1. 唯一执行模型

```
Conversation -> Run -> Step/Attempt -> Invocation/Approval/Artifact
                                    -> Event/Checkpoint（PG durable truth）
```

- **BFF**：事务创建 Run + command（`create_run`）；读、取消命令、SSE 重放。
- **Run worker**：持有 lease 的唯一执行写者；写 Step/Attempt/Event/
  Checkpoint/Invocation；执行期外任何 writer 提交被 fencing 拒绝。
- **core**：只读 BFF 下发的 config snapshot，返回 typed events/results，
  不直连 PG。
- 事件按 `(run_id, seq)` 严格递增唯一（DB unique 约束）；SSE 至少一次投递，
  客户端按 `(run_id, seq)` 幂等去重，终态恰好一次渲染。

## 2. 状态机（唯一转移真相）

实现：`map-business-backend/app/runtime/state_machine.py`。
非法转移写入前 fail-closed（`STATE_TRANSITION_VIOLATION`）；终态无出边。

### 2.1 Run

```
queued -> running | cancelling | cancelled | timed_out
running -> paused | completed | failed | cancelling | timed_out
paused -> running | cancelling
cancelling -> cancelled
终态: completed | failed | cancelled | timed_out
```

- cancel 命令只允许从 `queued|running|paused` 提交（条件更新），
  cancel/done/timeout 竞态收敛到唯一终态；五方竞态（cancel/done/timeout/
  retry/reconcile）不允许产生表外状态。

### 2.2 Step

```
pending -> ready | skipped | cancelled
ready -> running | skipped | cancelled
running -> waiting_approval | succeeded | failed | cancelled
waiting_approval -> running | failed | cancelled
终态: succeeded | failed | skipped | cancelled
```

### 2.3 Effect

```
planned -> approval_required | executing | cancelled
approval_required -> approved | cancelled
approved -> executing | cancelled
executing -> succeeded | failed | uncertain | cancelled
uncertain -> reconciling
reconciling -> succeeded | failed
终态: succeeded | failed | cancelled
```

### 2.4 ModelInvocation

```
planned -> sent | failed
sent -> succeeded | failed | unknown
unknown -> reconciled
终态: succeeded | failed | reconciled
```

### 2.5 Evidence

```
not-run -> running
running -> pass | fail | blocked | not-applicable-approved
终态: pass | fail | blocked | not-applicable-approved
```

## 3. 版本化事件 envelope

```json
{
  "schema_version": 1,
  "schema_minor": 0,
  "event_id": "<uuid>",
  "run_id": "<uuid>",
  "seq": 42,
  "type": "run.started",
  "occurred_at": "2026-08-13T12:00:00Z",
  "workspace_id": "<uuid>",
  "data": {}
}
```

- `schema_version`（major）未知 → 写入前拒绝 `UNKNOWN_EVENT_VERSION`；
  minor 增量 forward compatible（未知字段保留、不丢弃）。
- 冻结事件类型前缀：`run.` / `step.` / `attempt.` / `model.` /
  `tool.` / `approval.` / `artifact.` / `checkpoint.` / `effect.`。
- 未知 `type`（已知前缀内未定义）→ 写入前拒绝 `UNKNOWN_EVENT_TYPE`。
- SSE 帧：`id: <seq>`、`event: <type>`、`data: <envelope-json>`；客户端
  `Last-Event-ID`/`after_seq` 重连续传；断线不产生第二 Run。

## 4. 64KiB 分界与 ArtifactRef

- `inline_payload_max_bytes = 65536`（acceptance-profile）。任何 Event/
  message 的 payload ≤64KiB 内联；超限内联 → `ARTIFACT_PAYLOAD_TOO_LARGE`。
- 超限数据只经 ArtifactRef 传递：

```json
{
  "artifact_id": "<uuid>",
  "workspace_id": "<uuid>",
  "sha256": "<64hex>",
  "size_bytes": 123456,
  "content_type": "application/octet-stream",
  "policy_labels": ["internal"],
  "created_at": "2026-08-13T12:00:00Z",
  "expires_at": "2026-08-13T12:05:00Z"
}
```

- presigned URL TTL 300s（`presigned_url_ttl_seconds`）；原始对象存私有
  object store，PG 只存 manifest 元数据。

## 5. idempotency

- `POST /api/v1/runs` 携带 `Idempotency-Key`：
  同 `(workspace_id, principal_id, key)` + 同 body hash → 返回同一 Run
  （200/201 重放）；同 key + 异 body hash → 409 `IDEMPOTENCY_CONFLICT`。
- 一次用户操作最多一个 Run；断流不自动重跑。

## 6. OpenAPI 分层

- **public** `/api/v1/*`：浏览器消费；runs/conversations 读、SSE、cancel、
  approval 决定。
- **internal** `/internal/v1/*`：service identity 保护；run 创建/事件写回/
  checkpoint、snapshot 读取。
- 前端 DTO 由 public OpenAPI 生成（P1-CLEAN-BUILD-01），禁止手写同义类型。

## 7. typed errors

HTTP（public）与 SSE 共用 code 语义；legacy projection 只映射已登记项：

| code | HTTP | SSE | legacy projection |
| ---- | ---- | --- | ----------------- |
| `STATE_TRANSITION_VIOLATION` | 409 | `error` | `stopped`（仅 message 级） |
| `IDEMPOTENCY_CONFLICT` | 409 | — | — |
| `RUN_NOT_FOUND` / 跨 workspace | 404 | — | — |
| `UNKNOWN_EVENT_VERSION` / `UNKNOWN_EVENT_TYPE` | 400 | — | — |
| `ARTIFACT_PAYLOAD_TOO_LARGE` | 413 | `error` | — |
| `CAPABILITY_DISABLED` | 409 | `error` | 工具结果 `error` 字段 |
| `RUN_TERMINAL_STATE` | 409 | — | — |
| `EVENT_STALE_SEQ` | 409 | — | — |
| `AUTHENTICATION_REQUIRED` | 401 | — | — |
| `FORBIDDEN` | 403 | — | — |

## 8. 契约测试（AC-CONTRACT-01/03/04/05/07/08）

- 五张转移表：合法转移、非法转移、终态出边=0 全覆盖（pytest 参数化）。
- envelope：major 未知 fail-closed；minor 增量保真。
- 64KiB 边界：65535/65536/65537 三档（≤内联、=内联、>拒绝）。
- idempotency：同 key 同 body 重放 / 异 body 409（AC-CONTRACT-04）。
- typed error 映射表恒定（HTTP/SSE/legacy projection）。
