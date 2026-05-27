# MAP Observability Backend (`map-observability-backend`)

FastAPI 服务，提供请求分析、链路关联和错误定位 API。

## Responsibilities

- 从 Mongo 聚合请求/智能体/工具维度数据
- 提供趋势、明细、排行、导出接口
- 提供 RID 关联定位、时间窗口对齐与错误聚类
- 可选集成 Grafana/Loki 进行跨系统日志关联

## API Surface

### Health

- `GET /api/v1/health`

### Analytics

- `GET /api/v1/overview`
- `GET /api/v1/trends`
- `GET /api/v1/users`
- `GET /api/v1/agents`
- `GET /api/v1/tools`
- `GET /api/v1/requests`
- `GET /api/v1/requests/{request_id}`
- `GET /api/v1/requests/export/jsonl`

### Correlation

- `GET /api/v1/correlation/time-align`
- `GET /api/v1/correlation/rid/{request_id}`
- `GET /api/v1/correlation/errors`
- `GET /api/v1/correlation/tool-call`

### Friday

- `GET /api/v1/friday/config`
- `PUT /api/v1/friday/config`
- `POST /api/v1/friday/chat`

## Local Run

```bash
cd map-observability/map-observability-backend
uv sync --dev
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Test

```bash
uv run pytest -q
```

## Environment Variables

### Mongo

- `MONGO_URI`（必填）
- `MONGO_DB`（默认 `map_db_dev`）
- `MONGO_URI_UBDDEV`（可选）
- `MONGO_DB_UBDDEV`（默认 `map_db_dev`）

### API & Runtime

- `API_PREFIX`（默认 `/api/v1`）
- `TIMEZONE`（默认 `Asia/Shanghai`）
- `DEFAULT_TZ`（默认 `Asia/Shanghai`）
- `CORS_ORIGINS`（默认 `*`）
- `INDEX_ENSURE_MODE`（`auto|skip|required`）

### Analytics

- `MAX_QUERY_DAYS`（默认 `31`）
- `DEFAULT_TIME_RANGE_HOURS`（默认 `24`）
- `SLOW_CALL_THRESHOLD_S`（默认 `10`）

### Grafana/Loki (Optional)

- `GRAFANA_URL`
- `GRAFANA_USER`
- `GRAFANA_PASSWORD`
- `LOKI_DS_UID`

## Time Convention

- 分析接口统一按 UTC 聚合。
- 关联接口支持本地时间 + 时区输入，并转换到 UTC 与 Loki 时间窗口。

## References

- 上层观测文档：[`../README.md`](../README.md)
- 根文档：[`../../README.md`](../../README.md)
