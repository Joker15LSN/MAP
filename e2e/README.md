# Compose 跨服务 E2E（R2-P1-05）

一条命令在**随机 Compose project / 全新 volumes** 上完成真实跨服务验收并自动清理：

```bash
python3 e2e/run_e2e.py
```

## 拓扑

| 组件 | 真实性 |
| --- | --- |
| PostgreSQL 16 / MongoDB 7 | 真实容器，全新 named volumes |
| map_core（algorithm-service） | 真实容器，运行真实管线 |
| BFF（backend-service）/ outbox worker | 真实容器 |
| OTel Collector + Jaeger | 真实容器（otel profile） |
| **fake-llm** | **唯一的 fake**：OpenAI 兼容的确定性 LLM，位于 LLM 边界（`MAP_LLM_BASE_URL` + admin model-center 均指向它），不替换任何被验收服务 |

浏览器侧以**真实 HTTP/SSE**（runner 直接打 BFF 发布端口）发起，禁止 ASGI in-process
transport；BFF 级的快速回归（ASGI + FakeCore）已按整改要求改名为 BFF integration，
位于 `map-business-backend/tests/integration/test_bff_minimal_flow.py`。

## 场景

1. model-center 重定向：通过**真实 admin 变更 API** 把 model-center 大模型指向
   fake-llm。BFF 会把 model-center 行（base_url/model）内嵌进每个 core 请求
   （`route_llm_config` / `summary_llm_config` / 各 agent `llm_config`），
   仅设置 `MAP_LLM_BASE_URL` 环境变量**不够**，必须同时改 admin state；
   该写入本身也进入审计链；
2. happy path：真实 SSE 流式对话 → PG 终态 `completed`；
3. 会话幂等重放（同 Idempotency-Key 返回同一 conversation）；
4. 重复 request_id → `done.replayed=true`；
5. 流中 stop/abort：fake LLM 慢速吐字 → `:stop` → registry 命中、终态 `stopped`；
6. core 重启恢复：`docker restart` algorithm-service 后新请求仍成功；
7. feedback + withdraw → 墓碑行 + outbox 事件原子持久（按 SPEC 契约 outbox 目前只有
   写入方，无 relay，故校验持久化而非投递）；
8. 管理配置写 → 审计事件 + `/api/v1/admin/audit-events/verify` 链校验通过；
   并以 backend/worker 实际使用的 app 角色尝试篡改审计表，必须被拒绝（append-only）；
9. 真实 worker：造一条超期 `streaming` 消息 + 入队 `message_reconcile` job →
   worker 实际 claim（lease/fence）并完成对账 → 消息 `failed/STREAM_INTERRUPTED`，
   且终态消息不受影响（幂等）。

## ID 一致性

runner 生成 `traceparent` 并携带 `X-Request-ID / X-Session-ID / X-Workspace-ID`：

- PostgreSQL：`messages.request_id` 与请求一致，终态正确；
- MongoDB：`request_records` 按 `request_id` 命中，且 `trace_id` / `session_id` 一致；
- OTel：Jaeger 中同一 `trace_id` 同时包含 `map-business-backend` 与 `map-core` 的 span。

## 报告

结束时输出 JSON 报告（服务版本、request IDs、PG/Mongo 计数、最终状态、审计链结果、
fake LLM 调用统计），落盘至 `e2e/tmp/report-<project>.json` 并打印到控制台。
退出码 0 = 全部通过；无论成败都会执行 `docker compose down -v --remove-orphans`。

## 隔离保证

- 随机 project 名 → 全新 named volumes，绝不依赖本机 15432/27017 已有数据；
- 容器名加 `map-e2e-<rand>` 前缀、发布端口全部动态选取，与开发栈并行不冲突；
- BFF 管理状态写入 `e2e/tmp/<project>/data`，不触碰开发栈的 `app/data`。
