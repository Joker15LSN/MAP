# ADR-0005：Core typed Execution Event 与 Mongo telemetry 退役

- 状态：Accepted
- 日期：2026-08-30
- 决策者：MAP 开发负责人（Step 8 PR-K 实现后记录）
- 相关：ADR-0002（Canonical Run/Event 边界）、ADR-0004（Runtime Snapshot）

## 背景

core 曾用 `state_store.py` 的 dict 事件 + Mongo 四 collection 双写执行事实与
诊断：caller 逐层传递 `state_store/state_id/base_state`，`fire_and_forget`/
queue/Webhook 与 OTel 并存，Mongo 不可用时 core 启动直接失败。BFF Run worker
只能解析 legacy SSE 帧再投影为 canonical RunEvent。

## 决策

1. **core 只产 typed `CoreExecutionEvent`**：单一 `ExecutionEventEmitter.emit
   (type, data)` 入口；`RunContext`（run_id/workspace_id/attempt/request_id/
   session_id）由请求边界 contextvar 注入，caller 不再逐层传 store/state_id，
   不再手写 `record_event(dict)`。事件类型映射到 BFF canonical 前缀
   （`step.*/message.delta/tool.invocation_*/model.invocation_*/checkpoint.written/
   effect.*`）。
2. **core→BFF 只走 service-identity NDJSON stream**：
   `POST /internal/v1/runs/{run_id}/attempts/{attempt}/events`，scope
   `runs.execute`；每行一个 typed event（per-run 单调 seq），末行
   `stream.terminal`。BFF `TypedCoreRunStream` 映射为 canonical RunEvent；
   旧 `HttpCoreRunStream` 保留为 legacy adapter 直到 PR-G 排空。
3. **Mongo telemetry 抽象退役**：`state_store.py`/Mongo handler/Webhook/
   EventDispatcher/`fire_and_forget`/`record_agent_call` 与四个 telemetry
   collection 配置物理删除；core 的 Mongo 连接只服务 `agent_session_memories`
   业务数据，无配置时启动不再失败。
4. **OTel 是唯一诊断投影**：`ExecutionEventEmitter` 的 internal
   `OtelEventProjector` 把事件元数据写入当前 span；不写 data 全量，不把
   durable event 与 OTel handler 混成 generic handler。
5. **旧 collection 只停写、不提前 drop**：按
   `TODO/retention/mongo_state_store_retirement.md` 完成 export/digest/
   restore 演练与双签后才 drop；P1-OBS-01 切换 observability 读取前保留
   collection 供查询。

## 后果

- Run 正确性只依赖 PG canonical RunEvent；core 无 Mongo 也可启动执行。
- 普通 agent/model/tool 代码不再反向依赖 Mongo module；诊断仅 OTel。
- 旧 collection 的 drop 是独立运维动作，有 manifest、对账与双签。
- legacy chat 排空（PR-G）前旧 SSE adapter 继续存在，但不写 Mongo。
