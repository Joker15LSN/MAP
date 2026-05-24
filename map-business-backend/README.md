# Backend Service (BFF)

MAP 业务后端服务（BFF），位于前端与算法服务之间。

## 职责边界

- 对前端提供统一业务 API（问答、流式问答、管理配置读写）。
- 向下游转发到 `algorithm-service`，隔离算法层细节。
- 托管管理配置状态文件（默认 `/app/data/admin_state.json`）。

## 本地开发

```bash
cd map-business-backend
uv sync --dev
uv run uvicorn app.main:app --host 0.0.0.0 --port 18080
```

## 容器化运行

```bash
docker compose up -d backend-service
```

## 关键环境变量

- `MAP_CORE_API_ORIGIN`：算法服务地址，默认 `http://127.0.0.1:10000`
- `MAP_BFF_STATE_FILE`：管理状态文件路径，默认 `/app/data/admin_state.json`
