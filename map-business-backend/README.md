# MAP BFF 与 Worker（`map-business-backend`）

本模块是浏览器唯一业务入口，并提供独立 Worker 与数据库迁移。它拥有身份、Workspace
权限、Conversation/Message、Feedback、管理配置、Job/Effect 和审计事实；对 Core 的直接
Chat/Conversation 调用是当前过渡实现。

系统边界见 [`docs/SDD.md`](../docs/SDD.md)，实现与迁移设计见
[`docs/TDD.md`](../docs/TDD.md#3-bff-技术设计)。

## 当前职责

- Public `/api/v1`：Conversation、Message、Feedback、身份与标准错误；
- 兼容 `/api/chat*`：全域/心流问答代理，处于退役轨道；
- 管理入口：模型、Agent、权限、场景包、Skill 和 Flow 策略；
- `ConfigMutationService`：配置快照变更、mutation 对账与 append-only 审计；
- Worker：Job claim/lease/fencing、message reconcile 和 Effect 防重；
- Internal `/internal/v1`：服务身份保护的模块间入口；
- Alembic：`map_control` schema 与角色/约束迁移。

Canonical Run/Event/Checkpoint 的规则模块已经存在，但持久 Run worker 尚未实现；目标契约见
[`SPEC/contracts/run.md`](../SPEC/contracts/run.md)。

## 代码地图

```text
app/
├── main.py                 # 应用组合根、生命周期、middleware、router
├── settings.py             # BFF 进程配置
├── api/                    # Public、internal、兼容和管理协议 adapter
├── core/                   # 身份、服务身份、权限和脱敏
├── services/               # Conversation、配置 mutation、审计、SSE 等用例
├── repositories/           # PostgreSQL / 配置 adapter
├── runtime/                # Canonical 状态机、event envelope、错误映射
├── db/
│   ├── models/             # 当前持久事实
│   └── migrations/         # Alembic revisions
└── workers/                # 独立 Worker 进程与 lease/fencing runner
```

## 协议入口

- Conversation：`/api/v1/conversations*`、`/api/v1/messages/{id}:stop`
- Feedback：`/api/v1/messages/{id}/feedback`
- Audit：`/api/v1/admin/audit-events*`
- Internal：`/internal/v1/*`
- 兼容 Chat：`/api/chat*`
- 管理配置：`/api/admin/*`

字段、状态和错误以 [`SPEC/contracts/`](../SPEC/contracts/) 与运行时 OpenAPI (`/docs`) 为准，
不以本列表替代契约。

## 本地运行

Python >= 3.11。先从根目录准备 `.env` 并启动 PostgreSQL/Core；直接运行 BFF 时必须显式
设置 `MAP_CONTROL_DB_DSN`。

```bash
cd map-business-backend
uv sync --frozen
MAP_CONTROL_DB_DSN='postgresql+asyncpg://map:<local-password>@127.0.0.1:15432/map' \
  uv run uvicorn app.main:app --host 0.0.0.0 --port 18080
```

Compose：

```bash
docker compose up -d backend-service worker-service
```

## 测试

```bash
cd map-business-backend
uv sync --frozen
uv run ruff check .
uv run pytest
```

集成测试需要真实 PostgreSQL：

```bash
docker compose up -d postgres
MAP_CONTROL_TEST_DSN='postgresql+asyncpg://map:<local-password>@127.0.0.1:15432/map' \
  uv run pytest tests/integration
```

完整选择规则见 [`docs/TESTING.md`](../docs/TESTING.md)。

## 数据库迁移

迁移和应用使用不同 DSN。Compose 的 `migrate` 一次性模块以 `map_migrator` 角色执行 DDL；
BFF/Worker 使用非超级应用角色。

```bash
cd map-business-backend
MAP_CONTROL_MIGRATION_DSN='postgresql+asyncpg://map_migrator:<local-password>@127.0.0.1:15432/map' \
  uv run alembic -c alembic.ini upgrade head

uv run alembic -c alembic.ini revision --autogenerate -m "description"
```

已有 volume 不会重跑 `docker-entrypoint-initdb.d`。缺失 migrator 角色时，应由数据库管理员按
`db/init/01-roles.sql` 建立受限角色，不能把应用角色临时提升为长期 DDL 管理者。升级、回滚
和恢复顺序见 [`docs/OPERATIONS.md`](../docs/OPERATIONS.md)。

## 健康检查

- `GET /health`：liveness，只证明进程存活；
- `GET /ready`：检查 DSN、数据库、Alembic head 和默认 Workspace seed；失败返回 503。

Compose 使用 `/ready` 决定 BFF 是否可接流量。

## Worker 不变量

- 每次 claim 增加 `attempt`；heartbeat/complete/fail 受 `lease_owner + attempt` fencing；
- heartbeat 使用独立短事务；数据库错误按 lease 丢失处理；
- handler 通过 job context 检查 `lease_ok`/`cancel`，不自行 commit；
- 业务写与 fenced complete 共用 runner 事务；
- 外部副作用使用稳定 `idempotency_key` 和 dispatch token；结果未知进入 `uncertain`；
- SIGTERM 停止领取并传播取消，强制终止后由 lease expiry/reconcile 接管。

精确状态与运维语义见 [`SPEC/contracts/job-outbox.md`](../SPEC/contracts/job-outbox.md)。

## 关键配置

- `MAP_CONTROL_DB_DSN`：应用数据库 DSN；无代码默认值，缺失时 fail-fast/readiness-fail；
- `MAP_CONTROL_MIGRATION_DSN`：迁移 DSN；仅 migrator 使用；
- `MAP_CORE_API_ORIGIN`：Core 地址；
- `MAP_BFF_STATE_FILE`：当前配置快照文件；
- `MAP_AUTH_MODE`：`dev | trusted_header | oidc`；`oidc` 当前未实现，生产禁止 `dev`；
- `MAP_TRUSTED_PROXY_SECRET` / `MAP_TRUSTED_PROXY_REQUIRED`：可信代理模式；
- `MAP_SERVICE_CREDENTIALS` / `MAP_SERVICE_AUDIENCE`：internal 服务身份；
- `MAP_WORKER_ID` / `MAP_WORKER_LEASE_SECONDS` / `MAP_WORKER_POLL_SECONDS`：Worker；
- `MAP_CORS_ORIGINS` / `MAP_CORS_ALLOW_CREDENTIALS`：CORS。

完整变量与本地示例见根 [`.env.example`](../.env.example)。

## 配置快照的当前边界

`app/data/admin_state.json` 是当前管理配置快照。写入必须经过 `ConfigMutationService`，并与
PostgreSQL mutation/audit 对账；不能直接覆盖文件。版本化配置与不可变 Runtime Snapshot
是目标设计，尚未实现，见代码精简计划 Phase 6。

## 相关文档

- [`SPEC/contracts/identity.md`](../SPEC/contracts/identity.md)
- [`SPEC/contracts/conversation.md`](../SPEC/contracts/conversation.md)
- [`SPEC/contracts/audit.md`](../SPEC/contracts/audit.md)
- [`docs/DEVELOPMENT.md`](../docs/DEVELOPMENT.md)
- [`docs/OPERATIONS.md`](../docs/OPERATIONS.md)
