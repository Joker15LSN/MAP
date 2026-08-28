# MAP — Multi Agent Path

MAP 是一个面向企业场景的多智能体应用平台。它把业务 UI、身份与控制面、会话与异步任务、算法编排、运行观测拆成独立服务，并通过 PostgreSQL、MongoDB 和可选的 OpenTelemetry 链路形成可配置、可追踪、可恢复的运行闭环。

当前仓库同时保留两类问答入口：

- 兼容入口 `/api/chat*`：继续承载现有全域和心流模式；
- 会话入口 `/api/v1/conversations*`：提供持久化会话、SSE 流式回答、刷新恢复、主动停止、反馈和幂等。

浏览器只访问 BFF，不直接访问算法服务。跨服务契约以 [`SPEC/contracts/`](SPEC/contracts/) 为准。

第一次阅读或准备修改代码时，从 [`docs/README.md`](docs/README.md) 开始；领域术语、系统设计、
技术设计、测试、开发和运维文档均由该页索引。

## 当前实现状态（诚实声明，R-10）

本分支状态为 **P0 止血与契约基础，未完成**，不宣称黄金任务书已完成：

- 已完成：宿主执行止血（python_exec_tool/bash_tool/本地文件读写/stdio MCP
  全部 fail-closed）、仓库凭据清理与统一扫描门禁（scripts/security_scan.py）、
  OpenSandbox 认证 HTTP 客户端、Canonical Run/Event/Artifact 契约
  （状态机 + 事件 envelope + 64KiB/ArtifactRef 校验 + typed error 映射）、
  readiness/Compose/CI 门禁修复、验收证据校验器
  （scripts/validate_acceptance_evidence.py）。
- 未完成（blocked，见 tmp/acceptance/<TASK>/<sha>/ 证据）：
  OpenSandbox Server 部署与真实集成（AC-SEC-12）、OpenViking Server
  集成、durable Run/Checkpoint/worker（P1-RUN-01）、DAG/HITL/PLAN/CTX/SUB、
  OTel 单入口与观测融合、PG 版本化配置、/api/chat* 删除（需流量取证）、
  AgentScope 2.0.6 单引擎收敛与 CLEAN 系列任务。
- 已泄漏凭据的外部吊销（撤销工单）待 security owner 出具后 AC-SEC-01
  才可转为 pass；当前为 blocked（见
  security/INCIDENT-2026-08-13-hardcoded-credentials.md）。

## 核心能力

- 多智能体执行：场景识别、业务智能体调度、工具调用、结果聚合，以及 ScenarioHub/SkillHub 心流编排。
- 会话式问答：会话与消息持久化、增量 SSE、终态状态机、request-id 重放和 `Idempotency-Key`。
- 控制面治理：模型、智能体、MCP、Skill、权限、场景包和心流策略统一通过 BFF 管理。
- 安全边界：可信代理身份、服务身份、workspace/user 所有权隔离、集中授权和标准错误 envelope。
- 异步执行：独立 worker、短事务 heartbeat、lease/attempt fencing、崩溃回收和副作用 ledger。
- 不可抵赖审计：所有管理写操作经过 mutation 编排，并写入 append-only hash chain。
- 端到端观测：Mongo 请求/智能体/工具记录，独立观测前后端，以及可选的 BFF → map_core 分布式追踪。
- 可复现质量门禁：冻结依赖、后端 Ruff/pytest、前端 test/build、bundle、依赖审计和 Compose 跨服务 E2E。

## 系统架构（当前过渡实现）

```mermaid
flowchart LR
    User["用户 / 企业代理"] --> Web["业务前端<br/>React + Vite"]
    Web --> BFF["业务 BFF<br/>FastAPI"]

    BFF --> Core["map_core<br/>多智能体执行"]
    BFF --> State["AdminState<br/>配置快照文件"]
    BFF --> PG[("PostgreSQL<br/>map_control")]

    Worker["异步 Worker"] --> PG
    Worker --> Core
    Migrator["一次性 Migrator"] --> PG

    Core --> Mongo[("MongoDB<br/>运行记录")]
    Core --> LLM["LLM / MCP / 外部工具"]
    Core --> PG

    ObsWeb["观测前端"] --> ObsAPI["观测后端"]
    ObsAPI --> Mongo

    BFF -. "OTLP（可选）" .-> Collector["OTel Collector"]
    Core -. "OTLP（可选）" .-> Collector
    Collector --> Jaeger["Jaeger"]
```

### 服务与端口

| Compose 服务 | 代码目录 | 职责 | 默认宿主机入口 |
| --- | --- | --- | --- |
| `frontend-service` | `map-business-frontend/` | 问答工作台与管理配置 UI | `http://localhost:5174` |
| `backend-service` | `map-business-backend/` | BFF、身份、会话、反馈、管理配置、审计 | `http://localhost:18080` |
| `worker-service` | `map-business-backend/app/workers/` | job claim、lease、reconcile 和副作用执行 | 无公开端口 |
| `migrate` | `map-business-backend/app/db/migrations/` | 使用独立角色执行 Alembic 升级 | 一次性服务 |
| `algorithm-service` | `map_core/` | 场景识别、多智能体/工具/心流执行 | `http://localhost:10000` |
| `observability-frontend-service` | `map-observability/map-observability-frontend/` | 请求、智能体、工具和关联诊断 UI | `http://localhost:15152` |
| `observability-backend-service` | `map-observability/map-observability-backend/` | Mongo 分析与关联查询 API | `http://localhost:15151/api/v1` |
| `postgres` | — | 控制面、会话、任务、反馈和审计事实 | `localhost:15432` |
| `mongo` | — | map_core 运行记录与观测数据 | `localhost:27017` |
| `otel-collector` | `otel/` | 可选 OTLP 接收与转发 | `4317` / `4318` |
| `jaeger` | — | 可选分布式追踪查询 | `http://localhost:16686` |

## 关键运行链路

### 会话与流式回答

1. 前端向 BFF 创建会话，或加载已有会话进行刷新恢复。
2. BFF 校验 workspace/user 所有权，在 PostgreSQL 创建 user/assistant 消息事实。
3. BFF 调用 map_core，并将上游 SSE 解析为冻结事件集：`start`、`meta`、`content_delta`、`done`、`error`。
4. 只有合法 `done` 可以把消息置为 `completed`；EOF、解析错误、非法 UTF-8、上游错误和中断会保存为明确失败事实。
5. `POST /api/v1/messages/{id}:stop` 同时触发上游取消和条件终态更新；stop/done 竞态只允许一个终态。
6. 异常遗留的 `streaming` 消息由 worker 的 `message_reconcile` job 收敛为 `failed/STREAM_INTERRUPTED`。

### 管理配置与审计

管理配置的当前快照保存在 `map-business-backend/app/data/admin_state.json`，但写入不是简单覆盖文件：

1. `ConfigMutationService` 计算 expected/target hash 和脱敏 JSON Patch；
2. PostgreSQL 先持久化 pending mutation；
3. 通过临时文件、fsync 和原子 rename 更新快照；
4. 随后以短事务追加 applied/failed/rejected 审计事件并终结 mutation；
5. 审计事件形成 append-only SHA-256 hash chain，应用角色只有 SELECT/INSERT 权限；
6. BFF 启动时 reconciler 处理崩溃遗留 mutation，未知状态不会猜测为成功。

### Worker 与外部副作用

- 每次 job claim 都递增 `attempt`，`lease_owner + attempt` 是 heartbeat/complete/fail 的 fencing 条件。
- heartbeat 使用独立短事务；数据库异常按 lease 丢失处理，旧 worker 不能提交迟到结果。
- handler 业务写和 fenced complete 共用事务；SIGTERM 会停止领取新任务并向运行中 handler 传播 cancel。
- 外部副作用必须使用稳定 `idempotency_key`，并经过 effect ledger 与 provider 事实对账；未知结果进入 `uncertain`，不会盲目重发。

详细状态机见 [`SPEC/contracts/job-outbox.md`](SPEC/contracts/job-outbox.md)。

## 数据所有权

| 数据 | 事实源 | 主要写入方 | 主要读取方 |
| --- | --- | --- | --- |
| workspace、会话、消息、反馈、job、outbox、mutation、审计链 | PostgreSQL `map_control` schema | BFF、worker、migrator | BFF、worker、运维工具 |
| 管理配置当前快照 | `admin_state.json` | BFF `ConfigMutationService` | BFF、map_core 运行时快照消费者 |
| request/agent/tool/LLM 运行记录 | MongoDB `map_db_dev` | map_core | 观测后端、E2E 交叉验证 |
| trace/span | OTel Collector → Jaeger（可选） | BFF、map_core | Jaeger、E2E |

PostgreSQL 使用三个角色分权：

- `map_admin`：仅用于首次初始化和维护，不供应用服务使用；
- `map_migrator`：仅由一次性 `migrate` 服务执行 DDL；
- `map`：BFF、worker 和算法服务使用的非超级应用角色。

生产环境必须覆盖 Compose 中所有本地默认密码。

## 快速开始

### 前置条件

- Docker Engine 或 Docker Desktop
- Docker Compose v2
- 建议至少 8 GB 可用内存

只有在直接运行服务或执行本地测试时才需要 Node.js、Python 和 [`uv`](https://docs.astral.sh/uv/)。

### 1. 准备环境变量

```bash
cp .env.example .env
```

至少填写 OpenAI-compatible 模型配置：

```dotenv
MAP_LLM_BASE_URL=https://api.deepseek.com
MAP_LLM_MODEL=deepseek-v4-flash
MAP_LLM_API_KEY=replace-me
```

`.env` 只用于本地，不应提交。生产环境还必须设置安全的 PostgreSQL 密码，并使用 `trusted_header` 身份模式；不要沿用 `dev` 身份或仓库默认凭证。

### 2. 启动完整开发栈

```bash
docker compose up -d --build
```

首次启动顺序由 Compose 管理：PostgreSQL/MongoDB 健康 → `migrate` 升级到 Alembic head → BFF/worker → 两个前端。

检查状态：

```bash
docker compose ps
docker compose logs -f backend-service worker-service algorithm-service
```

### 3. 验证健康状态

```bash
curl -fsS http://localhost:18080/health
curl -fsS http://localhost:18080/ready
curl -fsS http://localhost:10000/health
curl -fsS http://localhost:15151/api/v1/health
```

- BFF `/health` 只表示进程存活；
- BFF `/ready` 同时检查数据库、Alembic revision 和默认 workspace seed；
- `/health` 与 `/ready` 是无身份基础设施探针，不返回 principal 数据。

OpenAPI：

- BFF：`http://localhost:18080/docs`
- map_core：`http://localhost:10000/docs`
- 观测后端：`http://localhost:15151/docs`

### 4. 停止服务

```bash
docker compose down
```

需要同时删除本地 PostgreSQL/MongoDB 数据和前端依赖卷时：

```bash
docker compose down -v
```

`down -v` 会删除本地数据，执行前应确认不需要保留。

## 启用分布式追踪

基础 Compose 默认不启用 OpenTelemetry。使用 overlay 同时启用 BFF、map_core、Collector 和 Jaeger：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.otel.yml \
  --profile otel \
  up -d --build
```

默认协议是 `http/protobuf`，对应 `http://otel-collector:4318`。切换为 gRPC 时，协议和端点必须成对设置：

```dotenv
MAP_OTEL_PROTOCOL=grpc
MAP_OTEL_ENDPOINT=http://otel-collector:4317
```

`OTEL_SDK_DISABLED=true` 是最高优先级 kill switch。仓库自带 Collector 只配置 traces pipeline；map_core 日志通过 span events 进入链路，不应在该 profile 下打开 native OTLP log export。

## API 边界

### 浏览器面向的 BFF API

| 类别 | 主要入口 |
| --- | --- |
| 会话 | `POST/GET /api/v1/conversations`、`GET /api/v1/conversations/{id}` |
| 流式与停止 | `POST /api/v1/conversations/{id}/messages:stream`、`POST /api/v1/messages/{id}:stop` |
| 反馈 | `/api/v1/messages/{id}/feedback`、会话反馈摘要、管理反馈查询 |
| 审计 | `GET /api/v1/admin/audit-events`、`GET /api/v1/admin/audit-events/verify` |
| 管理配置 | `/api/admin/*` |
| 兼容问答 | `/api/chat`、`/api/chat/stream/v2`、`/api/chat/flow/v1`、`/api/chat/stream/flow/v1` |

`/api/v1` 使用统一错误 envelope：

```json
{
  "code": "FORBIDDEN",
  "message": "platform_admin role required",
  "details": null,
  "request_id": "..."
}
```

旧 `/api/*` 兼容路径仍返回 `{"detail": ...}`。前端不得直接调用 `/global_domain/*` 或 `/flow_domain/*`，这些是 BFF 与 map_core 之间的内部边界。

### map_core 内部入口

- 全域：`/global_domain/chat`、`/global_domain/chat/stream/v2`；
- 心流：`/flow_domain/chat/v1`、`/flow_domain/chat/stream/v1`；
- master pipeline：`/master_pipeline/*`；
- 健康检查：`/health`、`/status`。

map_core 会把请求、智能体、工具和 LLM 运行记录写入 MongoDB，观测服务通过相同 `request_id`/`trace_id` 关联查询。

## 身份与权限

### 用户身份模式

| `MAP_AUTH_MODE` | 用途 | 行为 |
| --- | --- | --- |
| `dev` | 本地开发 | 固定 `local-admin/platform_admin`；`MAP_ENV=prod` 时拒绝启动 |
| `trusted_header` | 企业代理接入 | 必须配置 proxy secret 且保持 required=true，否则 fail-closed |
| `oidc` | 预留 | 当前未实现，返回 501 `NOT_IMPLEMENTED` |

`trusted_header` 模式只信任通过 `X-Trusted-Proxy-Secret` 验证后的 `X-UserId`、roles 和 workspace。跨 workspace 或跨用户资源统一返回 404，避免泄漏资源是否存在。

### 服务身份

`/internal/v1/*` 只接受 `ServicePrincipal`。服务 token 必须在 `MAP_SERVICE_CREDENTIALS` 中绑定 `key_id`、`service_name`、`audience` 和 scopes；用户身份与服务身份不能互相替代。

完整契约见 [`SPEC/contracts/identity.md`](SPEC/contracts/identity.md)。

## 配置索引

完整模板和优先级说明见 [`.env.example`](.env.example)。常用配置：

| 类别 | 变量 |
| --- | --- |
| 端口 | `MAP_FRONTEND_PORT`、`MAP_BFF_PORT`、`MAP_ALGO_PORT`、`MAP_OBS_FRONTEND_PORT`、`MAP_OBS_BACKEND_PORT` |
| LLM | `MAP_LLM_BASE_URL`、`MAP_LLM_MODEL`、`MAP_LLM_API_KEY` |
| BFF 数据 | `MAP_CONTROL_DB_DSN`、`MAP_CONTROL_MIGRATION_DSN`、`MAP_DEFAULT_WORKSPACE_ID` |
| 身份 | `MAP_AUTH_MODE`、`MAP_ENV`、`MAP_TRUSTED_PROXY_SECRET`、`MAP_SERVICE_CREDENTIALS` |
| Worker | `MAP_WORKER_ID`、`MAP_WORKER_LEASE_SECONDS`、`MAP_WORKER_POLL_SECONDS` |
| OTel | `MAP_OTEL_ENABLED`、`MAP_OTEL_PROTOCOL`、`MAP_OTEL_ENDPOINT`、`MAP_OTEL_SAMPLING_RATIO`、`OTEL_SDK_DISABLED` |
| 算法引擎 | `MAP_AGENT_ENGINE=legacy|agentscope` |

新会话 UI 由 `VITE_MAP_CONVERSATIONS_ENABLED=true` 开启，默认关闭并使用旧 `/api/chat*`。直接运行前端时可把它写入 `map-business-frontend/.env.local`；当前基础 Compose 没有透传该变量，如需在容器内启用，应在 `frontend-service.environment` 中显式设置后重建前端服务。

## 本地开发

### 基础设施与迁移

```bash
docker compose up -d postgres mongo

cd map-business-backend
uv sync --frozen
MAP_CONTROL_MIGRATION_DSN=postgresql+asyncpg://map_migrator:map-migrator-local@127.0.0.1:15432/map \
  uv run alembic upgrade head
```

### BFF 与 Worker

```bash
cd map-business-backend
MAP_CONTROL_DB_DSN=postgresql+asyncpg://map:map@127.0.0.1:15432/map \
MAP_BFF_STATE_FILE="$PWD/app/data/admin_state.json" \
  uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 18080
```

另一个终端：

```bash
cd map-business-backend
MAP_CONTROL_DB_DSN=postgresql+asyncpg://map:map@127.0.0.1:15432/map \
MAP_BFF_STATE_FILE="$PWD/app/data/admin_state.json" \
  uv run python -m app.workers.main
```

### map_core

```bash
cd map_core
uv sync --frozen
# P0-SEC-01: 连接串不再有仓库默认值，必须显式注入（生产口令强要求）。
ENV=dev \
POSTGRES_DSN=postgresql://map:<local-dev-password>@127.0.0.1:15432/map \
MONGODB_URI='mongodb://map:<local-dev-password>@127.0.0.1:27017/?authSource=admin' \
MONGODB_DATABASE=map_db_dev \
  uv run python -m map_core.main --host 0.0.0.0 --port 10000
```

### 前端与观测服务

```bash
# 业务前端
cd map-business-frontend
npm ci
npm run dev

# 观测后端
cd map-observability/map-observability-backend
uv sync --frozen
MONGO_URI='mongodb://map:<local-dev-password>@127.0.0.1:27017/?authSource=admin' \
MONGO_DB=map_db_dev \
  uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 观测前端
cd map-observability/map-observability-frontend
npm ci
VITE_DEV_PROXY_TARGET=http://localhost:8000 npm run dev
```

## 测试与发布门禁

### 完整 release gate

```bash
# 日常开发：未设置 baseline 时 committed-range 步骤会明确记录 skipped
bash scripts/release_gate.sh

# 发布候选：baseline 必填，并检查 worktree 与 baseline..HEAD
RELEASE_GATE_FINAL=1 \
GATE_BASELINE_SHA=<approved-base-commit> \
  bash scripts/release_gate.sh
```

Gate 覆盖：

- browser E2E fail-closed self-test；
- worktree 与 committed-range whitespace check；
- Compose 配置；
- 三个 Python 服务的 frozen sync、Ruff 和 pytest；
- 两个前端的 clean install、test 和 build；
- bundle size；
- 三个 Python 服务 pip-audit 与两个前端 npm audit。

结果和每步日志位于 `tmp/gate-logs/`，机器可读汇总为 `tmp/gate-logs/gate-summary.json`。

### Compose 跨服务 E2E

```bash
# PR 稳定子集：真实浏览器 + 身份边界
MAP_E2E_FINAL=1 python3 e2e/run_e2e.py --suite pr

# 完整故障矩阵：重启、PG 中断、worker kill/lease takeover 等
MAP_E2E_FINAL=1 python3 e2e/run_e2e.py --suite full
```

E2E 只在 LLM 边界使用确定性的 fake，其余 PostgreSQL、MongoDB、BFF、worker、map_core、浏览器、Collector 和 Jaeger 都是真实服务。每轮使用随机 Compose project 和全新 volumes，结束后自动清理。报告位于 `e2e/tmp/report-*.json`。

更多说明见 [`e2e/README.md`](e2e/README.md)。

## 运维要点

- Liveness/readiness：用 `/health` 判断进程，用 `/ready` 判断 BFF 是否可接流量。
- 迁移：Compose 中只能由 `migrate`/`map_migrator` 执行；已有 volume 不会重跑 `docker-entrypoint-initdb.d`。
- Worker 升级：先向 worker 发送 SIGTERM 并等待安全停止，再升级 BFF/worker 镜像。
- 多 BFF 实例：`StreamRegistry` 是进程内取消注册表；需要按 message_id 粘性路由，或替换成共享取消通道。
- 审计验证：调用 `GET /api/v1/admin/audit-events/verify`；链损坏时不要绕过检查或直接修改 append-only 表。
- 依赖例外：当前登记为空；新增例外必须同时更新 [`SECURITY_EXCEPTIONS.md`](SECURITY_EXCEPTIONS.md) 和 `security/dependency_exceptions.json`，并包含 owner、工单和到期时间。

## 当前明确边界

- `MAP_AUTH_MODE=oidc` 尚未实施，会显式返回 501。
- feedback 转 evaluation case 依赖尚未实现的 R1-EVAL 事实源，默认 `MAP_EVAL_CONVERT_ENABLED=false` 并返回 501，不会创建伪 case。
- outbox 当前提供事务性写入和任务事实，未实现通用外部 relay。
- 新 conversation UI 默认关闭；后端 v1 API 和 E2E 已可用。
- 基础 Compose 默认关闭 OTel；需要 overlay 才会启动 Collector/Jaeger 并启用 exporter。

## 仓库结构

```text
MAP/
├── README.md
├── AGENTS.md                         # Agent 工作指针与完成标准
├── CONTEXT.md                        # 领域统一语言
├── docker-compose.yml               # 基础开发栈
├── docker-compose.otel.yml          # OTel/Jaeger overlay
├── .env.example                     # 配置模板与优先级
├── docs/                             # SDD/TDD、开发、测试、运维与入门
├── SPEC/
│   ├── ARCHITECTURE.md
│   ├── STANDARDS.md
│   ├── contracts/                   # 跨服务权威契约
│   └── adr/                         # 架构决策记录
├── map-business-frontend/           # 业务 React/Vite 前端
├── map-business-backend/            # FastAPI BFF + worker + Alembic
├── map_core/                         # 多智能体算法服务
├── map-observability/
│   ├── map-observability-backend/   # Mongo 分析 API
│   └── map-observability-frontend/  # 观测 React/Vite 前端
├── packages/map-tree-core/          # 两个前端共享的调用树组件
├── e2e/                             # 独立 Compose E2E runner
├── scripts/                         # release、bundle、依赖与证据工具
├── security/                        # 机器可读安全例外
└── otel/                            # Collector 配置
```

## 文档索引

- 文档入口与事实优先级：[`docs/README.md`](docs/README.md)
- 领域术语：[`CONTEXT.md`](CONTEXT.md)
- 系统设计（SDD）：[`docs/SDD.md`](docs/SDD.md)
- 技术设计（TDD）：[`docs/TDD.md`](docs/TDD.md)
- 开发指南：[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)
- 测试策略：[`docs/TESTING.md`](docs/TESTING.md)
- 运维手册：[`docs/OPERATIONS.md`](docs/OPERATIONS.md)
- 新成员入门：[`docs/ONBOARDING.md`](docs/ONBOARDING.md)
- 规范与 ADR：[`SPEC/README.md`](SPEC/README.md)
- Canonical Run/Event/Artifact：[`SPEC/contracts/run.md`](SPEC/contracts/run.md)
- 身份与权限：[`SPEC/contracts/identity.md`](SPEC/contracts/identity.md)
- 会话、SSE 与幂等：[`SPEC/contracts/conversation.md`](SPEC/contracts/conversation.md)
- 反馈：[`SPEC/contracts/feedback.md`](SPEC/contracts/feedback.md)
- 审计链：[`SPEC/contracts/audit.md`](SPEC/contracts/audit.md)
- Worker、effect 与 outbox：[`SPEC/contracts/job-outbox.md`](SPEC/contracts/job-outbox.md)
- BFF：[`map-business-backend/README.md`](map-business-backend/README.md)
- 算法服务：[`map_core/README.md`](map_core/README.md)
- 业务前端：[`map-business-frontend/README.md`](map-business-frontend/README.md)
- 观测系统：[`map-observability/README.md`](map-observability/README.md)
- Compose E2E：[`e2e/README.md`](e2e/README.md)
- 供应链安全例外：[`SECURITY_EXCEPTIONS.md`](SECURITY_EXCEPTIONS.md)
- 代码精简计划：[`TODO/代码精简与可读性改造执行计划.md`](TODO/代码精简与可读性改造执行计划.md)

## 变更不变量

提交架构或跨服务变更时，至少保持以下约束：

1. 前端只访问 BFF，不能绕过身份/权限层直连 map_core；
2. `/api/v1` 契约变化必须同步 OpenAPI snapshot 和 `SPEC/contracts/`；
3. 所有管理写继续通过 `ConfigMutationService` 和审计链；
4. worker handler 不自行 commit，外部副作用继续使用稳定 key 和 effect guard；
5. 新服务、端口、环境变量或数据所有权变化必须同步 Compose、`.env.example` 和 README；
6. 合并前运行与风险相称的 release gate 和 E2E，不用旧 artifact 覆盖新失败。
