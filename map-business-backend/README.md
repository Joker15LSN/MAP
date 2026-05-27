# MAP Backend Service (`map-business-backend`)

`map-business-backend` 是 MAP 的 BFF（Backend For Frontend）层：前端只与本服务通信，本服务再向算法服务转发。

## Service Responsibilities

- 对前端提供统一接口：问答、流式问答、管理配置读写。
- 隔离算法细节：封装 `map_core` 路径与 Header 透传。
- 管理配置托管：维护模型中心、智能体、权限与心流策略配置。
- 运行时配置注入：根据管理配置自动注入 `scene_selection` 与 `dispatch_config`。

## API Overview

### Chat API

- `POST /api/chat`
- `POST /api/chat/stream/v2`
- `POST /api/chat/flow/v1`
- `POST /api/chat/stream/flow/v1`

### Admin API (Core)

- `GET /api/admin/full-config`
- `GET /api/admin/summary`
- `GET /api/admin/flow-runtime-snapshot`

### Admin API (Flow)

- `GET/PUT /api/admin/flow-policy`
- `GET/PUT /api/admin/scenario-packs`
- `GET/PUT /api/admin/flow-skill-descriptors`

### Admin API (Model & Agent)

- `GET/PUT /api/admin/model-center`
- `GET/PUT /api/admin/master-agent`
- `GET /api/admin/business-agents`
- `POST /api/admin/business-agents`
- `PUT /api/admin/business-agents/{agent_code}`

## Runtime Injection Logic

当请求未显式携带以下字段时，BFF 会根据管理配置自动补齐：

- `scene_selection.enabled_agent_codes`
- `dispatch_config.scene_agent_configs`

这保证了“管理端改配置 -> 新请求立即生效”的一致性。

## Local Development

```bash
cd map-business-backend
uv sync --dev
uv run uvicorn app.main:app --host 0.0.0.0 --port 18080
```

## Docker Run

```bash
docker compose up -d backend-service
```

## Test

```bash
cd map-business-backend
uv run pytest -q
```

## Environment Variables

- `MAP_CORE_API_ORIGIN`：算法服务地址（默认 `http://127.0.0.1:10000`）
- `MAP_BFF_STATE_FILE`：状态文件路径（默认 `/app/data/admin_state.json`）
- `MAP_LLM_API_KEY`：用于下发智能体 `llm_config.api_key`

## Data File

- 默认状态文件：`map-business-backend/app/data/admin_state.json`
- 该文件承载管理端配置快照；生产可替换为外部配置中心。

## Notes

- 新增字段遵循向后兼容，优先“增量扩展”而非替换旧字段。
- 上游异常时，流式接口会输出规范化 `error` 与 `done` 兜底帧。

## References

- 根文档：[`../README.md`](../README.md)
- 算法服务：[`../map_core/README.md`](../map_core/README.md)
