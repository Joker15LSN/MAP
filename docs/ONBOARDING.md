# MAP 开发者入门

这份指南帮助新成员在不先读完整仓库的情况下建立可靠心智模型。预计 60–90 分钟完成
首次阅读与本地验证。

## 1. 先建立三个认识

1. MAP 不是单一 Agent 服务，而是业务 UI、BFF/Worker、Core 执行和观测模块组成的系统。
2. 当前代码是过渡态：Conversation 与 `/api/chat*` 并存，legacy/AgentScope 双引擎并存，
   Canonical Run 契约已确定但持久 Run worker 尚未落地。
3. `TODO` 描述目标和顺序，不代表已实现；判断当前行为要回到源码、迁移和测试。

统一术语先读 [`CONTEXT.md`](../CONTEXT.md)，系统全景读 [`SDD.md`](SDD.md)。

## 2. 30 分钟阅读路线

按顺序阅读：

1. 根 [`README.md`](../README.md) 的“当前实现状态”“系统架构”“快速开始”；
2. [`docs/SDD.md`](SDD.md) 的当前关键链路与目标执行模型；
3. [`SPEC/README.md`](../SPEC/README.md) 的文档层级；
4. [`ADR-0001`](../SPEC/adr/ADR-0001-disable-host-execution-capabilities.md) 和
   [`ADR-0002`](../SPEC/adr/ADR-0002-canonical-run-event-artifact-contract.md)；
5. 与任务相关的 [`SPEC/contracts/`](../SPEC/contracts/)；
6. [`docs/TDD.md`](TDD.md) 中对应模块；
7. 对应服务 README。

如果这些内容发生冲突，按 [`docs/README.md`](README.md#文档分层与事实优先级) 处理并记录
文档漂移，不要默默选择自己偏好的版本。

## 3. 目录地图

```text
MAP/
├── map-business-frontend/       # 业务 React UI
├── map-business-backend/        # BFF、Worker、Alembic、PostgreSQL 模型
├── map_core/                    # 多智能体、模型、工具、沙箱执行
├── map-observability/           # 观测查询 API 与 UI
├── packages/                    # 真实跨模块共享能力
├── SPEC/                        # 权威契约、规则和 ADR
├── docs/                        # SDD/TDD/开发/测试/运维说明
├── e2e/                         # 隔离的 Compose 跨服务 E2E
├── scripts/                     # release、安全、依赖和 Evidence 工具
├── TODO/                        # 未完成工作的计划与验收任务书
├── security/                    # 安全事件和机器可读例外
└── docker-compose*.yml          # 开发、生产约束和 OTel 编排
```

`tmp/`、`e2e/tmp/`、缓存、虚拟环境、`node_modules/` 和前端 `dist/` 是生成内容，不是设计
事实源。

## 4. 跟一遍业务请求

### 兼容 Chat 路径

1. 前端：`map-business-frontend/src/features/chat/ChatView.tsx`
2. controller：`features/chat/useChatController.ts`
3. HTTP/SSE client：`src/api/client.ts`、`src/api/sse.ts`
4. BFF 路由：`map-business-backend/app/api/chat.py`
5. Core client：`map-business-backend/app/core_client.py`
6. Core 路由：`map_core/map_core/routers/global_domain_router.py` 或 `flow_domain_router.py`
7. 执行：`map_core/map_core/service/global_domain.py`、`flow_domain.py`、`agent_runtime.py`

这是退役路径。理解它用于迁移和回归，不应把新功能只加在这里。

### Conversation 路径

1. UI：`map-business-frontend/src/features/conversation/ConversationView.tsx`
2. client/controller：`conversationApi.ts`、`useConversationController.ts`
3. BFF 路由：`map-business-backend/app/api/conversations.py`
4. application module：`app/services/conversation_service.py`
5. repository/model：`app/repositories/conversations.py`、`app/db/models/conversation.py`
6. 中断恢复：`app/services/message_reconciler.py` 与 Worker

精确行为见 [`SPEC/contracts/conversation.md`](../SPEC/contracts/conversation.md)。目标是再从
Message 生命周期迁移到 Canonical Run，而不是永久保留两套执行模型。

## 5. 跟一遍控制面写入

1. 管理 UI：`map-business-frontend/src/features/admin/`
2. BFF 管理路由：`map-business-backend/app/api/admin_*.py`
3. 变更编排：`app/services/config_mutation.py`
4. 配置 adapter：`app/repositories/config.py`
5. mutation/audit 模型与迁移：`app/db/models/`、`app/db/migrations/versions/`
6. Core 快照消费者：`map_core/map_core/service/flow_config_provider.py`

关键点：配置快照文件只是当前值；mutation 与 append-only audit 才描述一次写入的结果与
证据。不要从 Router 直接改文件。

## 6. 跟一遍 Worker 与副作用

1. 进程入口：`map-business-backend/app/workers/main.py`
2. claim/lease/fencing：`app/workers/job_runner.py`
3. Job adapter：`app/repositories/jobs.py`
4. Job/Effect/Outbox 模型：`app/db/models/job.py`、`effect.py`、`outbox.py`
5. 契约：[`SPEC/contracts/job-outbox.md`](../SPEC/contracts/job-outbox.md)

阅读时特别观察：数据库时间、短事务 heartbeat、attempt fencing、SIGTERM、外部结果
`uncertain`。这些不是可以为“简洁”删除的样板代码。

## 7. 跟一遍 Agent 与沙箱

1. Core 组合根：`map_core/map_core/main.py`
2. 引擎选择 seam：`map_core/map_core/service/agent_runtime.py`
3. legacy 实现：`map_core/map_core/service/agent/`
4. AgentScope adapter：`map_core/map_core/service/agentscope2/`
5. 模型调用：`map_core/map_core/utils/model_invocation/`（typed 入口）；`utils/llm_engine.py` 为 B6 待删兼容壳
6. OpenSandbox：`service/opensandbox_client.py`、`sandbox_tools.py`、`sandbox_ledger.py`
7. 安全能力：`service/agent/disabled_capabilities.py`、`service/mcp_egress.py`

目标收敛顺序见 [`TODO/代码精简与可读性改造执行计划.md`](../TODO/代码精简与可读性改造执行计划.md)。

## 8. 跟一遍观测查询

1. Core 运行记录：`map_core/map_core/service/state_store.py` 与 `observability/`
2. 观测 API：`map-observability/map-observability-backend/app/main.py`
3. 查询模块：观测后端 `app/services/requests.py`、`llm_calls.py`、`correlation_service.py`
4. 观测 UI：`map-observability/map-observability-frontend/src/pages/`
5. 共享树：`packages/map-tree-core/src/RequestCallTree.tsx`

Mongo 与 OTel 当前并存。不要假设两者已经由 Canonical Event 完全统一。

## 9. 第一次本地验证

从根目录：

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
curl -fsS http://localhost:18080/ready
```

填入 `.env` 所需的本地秘密，但不要提交。更完整的运行、profile 与停止方式见
[`DEVELOPMENT.md`](DEVELOPMENT.md) 和 [`OPERATIONS.md`](OPERATIONS.md)。

如果只修改一个模块，按 [`TESTING.md`](TESTING.md#3-快速本地验证) 运行 frozen sync、lint、
test/build。不要为了通过本地测试改用未冻结依赖。

## 10. 当前高风险热点

- `conversation_service.py`：持久状态、流、取消和错误协调集中；
- `job_runner.py`：并发正确性高，改动必须覆盖 lease/fencing/故障；
- `config_mutation.py`：文件与数据库跨资源一致性；
- `llm_engine.py`：B6 待删兼容壳，provider/重试/观测已收敛到 `model_invocation`；
- legacy / AgentScope 双引擎：事件与工具策略重复；
- BFF Effect guard / Core sandbox ledger：相邻但未统一的副作用生命周期；
- Conversation、Chat 与目标 Run：三种语义容易被误当成同一概念。

修改这些区域前先读 TDD 和对应 contract，并先跑定向测试建立基线。

## 11. 适合的第一个任务

优先选择边界清楚且不改变持久协议的任务，例如：

- 修正文档与实现漂移并加链接校验；
- 为纯状态机、事件校验或前端 parser 补边界测试；
- 删除经引用扫描和测试证明不可达的私有 helper；
- 在不改变行为的前提下，把重复错误映射收敛到已有事实源；
- 给现有 adapter 补失败/超时/脱敏测试。

不建议把数据库迁移、Worker fencing、沙箱执行或引擎删除作为第一个独立任务。

## 12. 获取方向时需要回答的问题

- 这是当前行为、目标契约，还是兼容路径？
- 哪个模块拥有该事实和状态机？
- 哪个测试能在变更前固定行为？
- 失败、取消、重试和未知结果如何解释？
- 迁移完成后具体删除哪些代码和文档？

Agent 执行规则见 [`AGENTS.md`](../AGENTS.md)。
