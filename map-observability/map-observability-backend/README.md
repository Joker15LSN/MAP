# MAP 日志分析后端（FastAPI）

后端负责从 MongoDB 查询日志并输出指标分析与关联定位接口。

## 本地运行

```bash
uv sync --dev
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 环境变量

- `MONGO_URI`：必填，Mongo 连接串
- `MONGO_DB`：默认 `map_db_dev`
- `API_PREFIX`：默认 `/api/v1`
- `TIMEZONE`：默认 `Asia/Shanghai`
- `DEFAULT_TZ`：默认 `Asia/Shanghai`
- `CORS_ORIGINS`：默认 `*`
- `INDEX_ENSURE_MODE`：默认 `auto`（`auto|skip|required`）
- `MAX_QUERY_DAYS`：默认 `31`
- `DEFAULT_TIME_RANGE_HOURS`：默认 `24`
- `SLOW_CALL_THRESHOLD_S`：默认 `10`
- `GRAFANA_URL`：可选，关联定位时使用
- `GRAFANA_USER`：可选
- `GRAFANA_PASSWORD`：可选
- `LOKI_DS_UID`：默认 `bex1a2pgx8oowd`

## 关键接口

- `GET /api/v1/overview`
- `GET /api/v1/trends`
- `GET /api/v1/users`
- `GET /api/v1/agents`
- `GET /api/v1/tools`
- `GET /api/v1/requests`
- `GET /api/v1/requests/{request_id}`
- `GET /api/v1/correlation/time-align`
- `GET /api/v1/correlation/rid/{request_id}`
- `GET /api/v1/correlation/errors`

## 时间参数约定

- 后端统一按 UTC 进行查询与聚合计算。
- 常规分析接口（`/overview`、`/trends`、`/users`、`/agents`、`/tools`、`/requests`）的 `start_ts/end_ts` 按 UTC 解析。
- 关联定位接口（`/correlation/time-align`、`/correlation/errors`）使用 `start_local/end_local + tz` 输入，服务端转换为 UTC 与 Loki `ns` 窗口。
- `time-align` 返回 `start_local/end_local/start_utc/end_utc`，用于前端与 Grafana（Asia/Shanghai）对齐展示。
