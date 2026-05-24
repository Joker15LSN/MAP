# Observability Service

MAP 观测服务，负责日志分析、请求追踪、链路关联与诊断。

## 子模块

- `map-observability-backend`：FastAPI 分析后端
- `map-observability-frontend`：React 可视化前端

## 职责边界

- 聚合请求/智能体/工具维度指标
- 提供请求详情、错误聚类、关联检索
- 与业务链路解耦，独立部署与演进

## 本地开发

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

## 容器化运行

```bash
docker compose up -d observability-backend-service observability-frontend-service
```

## 关键环境变量

- `MONGO_URI` / `MONGO_DB`
- `API_PREFIX`（默认 `/api/v1`）
- `TIMEZONE` / `DEFAULT_TZ`

## 深入文档

- 历史详细部署与功能文档：`docs/OBSERVABILITY_DETAILED.md`
