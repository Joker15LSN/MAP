# ADR-0003: Canonical Event 扩展 `message.delta` — Run 投影的增量内容事实

- 状态：Accepted
- 日期：2026-08-26
- 任务：`TASK P0-CONV-01` / 代码精简计划 Step 4（PR-F）
- 基线：`e781ecd84d4fa54ddf5aa4dd33c9c321b4031f53`（实现工作树在此基础上）
- 规范：`SPEC/contracts/run.md` §3

## Context

ADR-0002 冻结了 Canonical Event 类型前缀（`run./step./attempt./model./tool./
approval./artifact./checkpoint./effect.`）作为跨模块稳定协议。Step 4 把前端
Conversation 路径切换到 Canonical Run：浏览器只订阅 `/api/v1/runs/{id}/events`
并用 `(run_id, seq)` 投影消息内容。旧 conversation SSE 具有 `content_delta`
逐字流式能力；若只保留 `step.completed` 全文事件，前端会失去逐字渲染能力，
能力矩阵（P0-CONV-01）出现行为回退。增量内容不是执行生命周期事实，而是一个
用户可见内容的 Run 投影，因此需要在不改变状态机的前提下新增一个稳定事件类型。

## Decision

1. 新增冻结前缀 `message.`，以及唯一冻结类型 `message.delta`：
   - `data.content: string`（本次增量，非累计；可为空字符串但必须存在）；
   - 事件只描述用户可见消息内容，不改变 Run/Step/Attempt 状态；
   - 与 `step.completed` 的关系：`step.completed.data.content` 仍是全文权威，
     重放投影时全文覆盖增量；`message.delta` 只用于流式实时渲染与增量重放。
2. `EventEnvelope` 的冻结前缀与类型集合同步扩展；未知 `message.*` 类型仍
   在写入前拒绝（`UNKNOWN_EVENT_TYPE`）。
3. 生产投影：BFF `HttpCoreRunStream` 把 legacy core `content_delta` 帧翻译为
   typed `CoreEvent("message.delta", {"content": delta})`；协议知识只存在于该
   adapter。前端 `runProjection` 按 seq 去重后追加增量，收到 `step.completed`
   时以全文覆盖。
4. 状态机、64KiB 分界、SSE 帧格式、idempotency、typed error 均不变。

## Consequences

- `SPEC/contracts/run.md` §3 前缀列表增加 `message.`；contract test 冻结表同步。
- 旧 conversation SSE 的 `content_delta` 与 Run 的 `message.delta` 并存于
  过渡窗口；旧路径退役（Step 4 PR-G）后只保留 Canonical 事件。
- 新增事件不得被 worker/core 用来推断生命周期状态；任何状态推断仍只允许
  `run./step./attempt./effect.` 等既有生命周期事实。
- 兼容性：minor 不变化；未知 `message.*` 未来类型仍 fail-closed，调用方不能
  静默假设未来类型语义。
