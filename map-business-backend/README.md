# MAP Backend Service (`map-business-backend`)

`map-business-backend` 是 MAP 的 BFF（Backend For Frontend）层：前端只与本服务通信，本服务再向算法服务转发。

## Service Responsibilities

- 对前端提供统一接口：问答、流式问答、管理配置读写。
- 隔离算法细节：封装 `map_core` 路径与 Header 透传。
- 管理配置托管：维护模型中心、智能体、权限与心流策略配置。
- 运行时配置注入：根据管理配置自动注入 `scene_selection` 与 `dispatch_config`。
- 控制面数据（F-03）：会话/反馈/评测/任务/审计等产品数据的 PostgreSQL 事实源与异步作业框架。

## Code Layout (F-01/F-03)

```text
app/
├── main.py                  # app factory + middleware + router 注册（约 100 行）
├── settings.py              # 环境配置（MAP_CORE_API_ORIGIN / MAP_BFF_STATE_FILE）
├── api/                     # 路由：chat.py / admin_config.py / admin_master.py / admin_assets.py
├── services/                # payload 构建、幂等处理等 use case
├── repositories/            # ConfigRepository protocol（AdminState 文件适配）
├── db/                      # SQLAlchemy 2.x async + Alembic（map_control schema）
│   ├── models/              # workspaces/users/jobs/outbox_events/idempotency_records
│   └── migrations/          # Alembic 迁移
└── workers/                 # 独立进程 `python -m app.workers.main`（job claim/lease/retry）
```

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

集成测试需要真实 PostgreSQL（默认 `127.0.0.1:15432`，即根目录 `docker compose up -d postgres`）：

```bash
docker compose up -d postgres
MAP_CONTROL_TEST_DSN=postgresql+asyncpg://map:map@127.0.0.1:15432/map uv run pytest tests/integration -q
```

## Database Migrations (F-03 / FIX-P1-DEPLOY-01)

```bash
# 升级到 head
MAP_CONTROL_MIGRATION_DSN=postgresql+asyncpg://map:map@127.0.0.1:15432/map uv run alembic -c alembic.ini upgrade head
# 降级一版 / 生成新迁移
uv run alembic -c alembic.ini downgrade -1
uv run alembic -c alembic.ini revision --autogenerate -m "description"
```

迁移使用独立 DSN（`MAP_CONTROL_MIGRATION_DSN`），与业务 DSN（`MAP_CONTROL_DB_DSN`）分离：

- **Compose 全新环境**：`migrate` 一次性服务以 `map_migrator` 角色执行 `alembic upgrade head`；`backend-service`/`worker-service` 在迁移成功后启动。`db/init/01-roles.sql` 在首次初始化时创建 `map_migrator` 角色并配置默认权限（业务角色 `map` 仅 DML）。
- **已有 volume 升级**：`docker-entrypoint-initdb.d` 不会重跑。要么手动创建 `map_migrator` 角色并授权 `map_control` schema，要么把 `MAP_CONTROL_MIGRATION_DSN` 指向有 DDL 权限的角色（例如 `map`）。
- **回滚边界**：迁移均为 expand/contract 风格（本轮只增表/列/索引/seed，不删除已提交结构）。降级一版只回滚该版本新增对象；`4c9e1f2a8b3d` 的 downgrade 删除 default workspace seed（幂等，仅删稳定 UUID+code 匹配行）。生产回滚顺序：先停 worker → 停 backend → 执行 `alembic downgrade -1` → 重启。

## Readiness / Liveness

- `GET /health`（liveness）：进程存活，不依赖下游。
- `GET /ready`（readiness）：数据库可达 + Alembic revision == head + default workspace seed 存在，任一失败返回 503。Compose healthcheck 使用 `/ready`。

## Worker (F-03)

```bash
uv run python -m app.workers.main
```

SIGTERM 停止领取新任务并等待当前 handler 安全点；Compose 中对应 `worker-service`。

## Environment Variables

- `MAP_CORE_API_ORIGIN`：算法服务地址（默认 `http://127.0.0.1:10000`）
- `MAP_BFF_STATE_FILE`：状态文件路径（默认 `/app/data/admin_state.json`）
- `MAP_LLM_API_KEY`：用于下发智能体 `llm_config.api_key`
- `MAP_CONTROL_DB_DSN`：控制面 PostgreSQL DSN（默认 `postgresql+asyncpg://map:map@127.0.0.1:15432/map`，schema `map_control`）
- `MAP_CONTROL_MIGRATION_DSN`：迁移专用 DSN（默认同上）
- `MAP_DEFAULT_WORKSPACE_ID`：稳定默认 workspace UUID（默认 `00000000-0000-0000-0000-000000000001`，业务 code=`default`；与 migration seed 同一值）
- `MAP_AUTH_MODE`：`dev`（仅本地）| `trusted_header` | `oidc`（R3）
- `MAP_ENV`：`dev` | `prod`；`prod` 下禁止 `MAP_AUTH_MODE=dev`
- `MAP_TRUSTED_PROXY_SECRET` / `MAP_TRUSTED_PROXY_REQUIRED`：trusted_header 模式的代理验证
- `MAP_WORKER_ID` / `MAP_WORKER_LEASE_SECONDS` / `MAP_WORKER_POLL_SECONDS`：worker 参数

## Data File

- 默认状态文件：`map-business-backend/app/data/admin_state.json`
- 该文件承载管理端配置快照；生产可替换为外部配置中心。

## Notes

- 新增字段遵循向后兼容，优先“增量扩展”而非替换旧字段。
- 上游异常时，流式接口会输出规范化 `error` 与 `done` 兜底帧。

## References

- 根文档：[`../README.md`](../README.md)
- 算法服务：[`../map_core/README.md`](../map_core/README.md)
