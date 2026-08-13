# ADR-0002: Canonical Run/Event/Artifact 契约 — 单写者与 BFF→worker→core 边界

- 状态：Accepted
- 日期：2026-08-13
- 任务：`TASK P0-CONTRACT-01`（依赖：无）
- 规范：`SPEC/contracts/run.md`（本文档的 normative 实现）
- 基线：`e019059c2c8499454ecddc9eb63655aeadb0bd90`

## Context

当前对话执行路径是 BFF 直接转发 `/api/chat*` 到 core 的同步/流式调用，
浏览器与 core 之间没有 durable 执行模型；消息级状态存在 PG，但 Run/Step/
Event/Checkpoint 不存在。黄金任务书边界第 2 条要求
`Conversation -> Run -> Step/Attempt -> Invocation/Approval/Artifact ->
Event/Checkpoint` 成为唯一执行模型，PG 是 durable truth。

## Decision

1. **单写者规则**：Run 生命周期由唯一持有 lease 的 Run worker 写入；BFF
   只做事务内创建（Run+command）与读取/取消命令；core 只消费 BFF 下发的
   snapshot 并返回 typed events/results，不直接写 PG。任何违反单写者的
   提交以条件更新（`WHERE status=<expected>`）+ lease fencing 拒绝。
2. **状态机是唯一转移真相**：`app/runtime/state_machine.py` 定义
   Run/Step/Effect/ModelInvocation/Evidence 五张 frozen 转移表
   （与 `TODO/acceptance-profile.yaml` `canonical_states` 一致）。非法转移
   在写入前 fail-closed（`STATE_TRANSITION_VIOLATION`）；终态不可再转移。
3. **版本化事件 envelope**：`event.v1`（major）+ 内部 minor 字段；未知
   major/schema 在写入前拒绝（`UNKNOWN_EVENT_VERSION`），minor 增量 forward
   compatible（未知字段保留）。事件按 `(run_id, seq)` 严格递增且唯一。
4. **64KiB 分界**：≤64KiB payload 内联；>64KiB 只允许 ArtifactRef
   （`artifact_id/sha256/size_bytes/content_type/presigned_ttl`），
   `ARTIFACT_PAYLOAD_TOO_LARGE` 拒绝超限内联。
5. **idempotency**：Run 创建使用 `Idempotency-Key`（同 key+同 body 重放返回
   同一 Run；同 key+异 body → 409 `IDEMPOTENCY_CONFLICT`）。
6. **typed errors**：公共 `/api/v1` 错误使用 `{code, message, details,
   request_id}` envelope；SSE 使用 `error` 事件（`code` 字段）；legacy
   projection 仅映射已登记语义（见 run.md §7）。
7. **OpenAPI 分层**：public OpenAPI（浏览器消费的 `/api/v1`）与 internal
   OpenAPI（service identity 保护的 `/internal/v1`）分离；前端 DTO 由
   OpenAPI 生成，禁止手写同义 DTO（P1-CLEAN-BUILD-01 执行）。

## Consequences

- PG 落表（runs/steps/events/checkpoints/model_invocations 等）在
  P1-RUN-01 实施；本 ADR 先行固定状态机与 envelope 契约，避免执行实现
  时反推契约。
- 存量 `/api/chat*` 路径在 P1-API-01 按流量证据退役；新路径一律
  `/api/v1/runs*`。
- core 侧 Run 执行适配（durable 事件写回 BFF）依赖 P1-RUN-01 的 worker
  与 outbox；在此之前本契约只定义边界，不改变现有消息级行为。
