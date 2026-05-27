# MAP Observability (`map-observability`)

MAP 观测系统用于分析多智能体请求链路，覆盖请求、智能体、工具三个层级。

## Components

- `map-observability-backend`：FastAPI 分析后端
- `map-observability-frontend`：React 可视化前端

## What It Solves

- 请求级分析：成功率、耗时、Token 消耗、错误分布
- 执行级分析：智能体调用链、工具调用频次与失败原因
- 关联定位：请求 RID 与日志窗口对齐、错误聚类、工具调用追踪

## Data Sources

观测后端主要消费算法服务写入 Mongo 的三类集合：

- `request_records`
- `agent_executions`
- `tool_call_records`

## Quick Start

### Docker

```bash
docker compose up -d observability-backend-service observability-frontend-service
```

- 前端：`http://localhost:15152`
- 后端：`http://localhost:15151/api/v1`

### Local Development

后端：

```bash
cd map-observability/map-observability-backend
uv sync --dev
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

前端：

```bash
cd map-observability/map-observability-frontend
npm ci
npm run dev
```

## Key APIs

- 分析看板：`/api/v1/overview`、`/api/v1/trends`
- 实体分析：`/api/v1/users`、`/api/v1/agents`、`/api/v1/tools`
- 请求检索：`/api/v1/requests`、`/api/v1/requests/{request_id}`
- 关联定位：`/api/v1/correlation/*`

## Time Semantics

- 常规分析接口：按 UTC 查询与聚合。
- 关联定位接口：支持 `start_local/end_local + tz` 输入。
- 前端默认展示 `Asia/Shanghai`，并保留 UTC 对照。

## References

- 观测后端：[`map-observability-backend/README.md`](map-observability-backend/README.md)
- 观测前端：[`map-observability-frontend/README.md`](map-observability-frontend/README.md)
- 根文档：[`../README.md`](../README.md)
