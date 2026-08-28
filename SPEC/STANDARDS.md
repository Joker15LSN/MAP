# MAP 工程规范

## 1. 统一语言与命名

- 产品名统一为 `MAP`（Multi Agent Path）。
- 领域名称以 [`CONTEXT.md`](../CONTEXT.md) 为准；同一概念不新增别名。
- 代码使用能表达职责的具体名称；避免无边界的 `Manager`、`Helper`、`Common`、`Utils`。
- 架构讨论使用 module、interface、seam、adapter、fact owner 和 caller，不用模糊的“中间层”
  掩盖所有权。
- 环境变量以 `MAP_` 或工具规定前缀命名；浏览器构建变量使用 `VITE_`。

## 2. 模块与依赖

- 一个 module 隐藏一组连贯决策，以较小 interface 提供较多能力。
- 依赖从协议 adapter 指向 application module，再指向 repository/client adapter。
- 组合根负责读取配置和装配具体 adapter；业务模块不直接读取散落的环境变量。
- 新 seam 必须有真实的独立变化、第二实现或测试替身需求；禁止单实现样板抽象。
- 共享 package 只承载已被多个消费者真实复用且语义稳定的能力。
- 状态机、重试、身份、错误、序列化和脱敏各自只有一个事实源。

详细设计准则见 [`docs/TDD.md`](../docs/TDD.md)。

## 3. 接口与事件

- 浏览器只访问 BFF；Public `/api/v1` 与 internal `/internal/v1` 分离。
- internal 接口验证服务身份，Public 接口验证 Principal、Workspace 所有权和权限。
- SSE 事件、错误、状态和幂等以对应 [`contracts/`](contracts/) 为准。
- HTTP 错误使用稳定 typed envelope；调用方不依赖厂商异常文本。
- Event/API schema 版本化：未知 major fail-closed，兼容 minor 保留未知字段。
- 大 payload 使用带 hash、大小和类型的 ArtifactRef，不内联绕过限制。
- 前端 DTO 由 Public OpenAPI 生成；禁止手写同义 DTO 长期漂移。

## 4. 状态、并发与副作用

- 状态转移先验证再写入，使用 expected-state 条件更新；终态不可逆。
- Worker 的 heartbeat、完成和失败使用 lease owner + attempt/token fencing。
- 事务只覆盖一个业务不变量，不包围慢网络、模型或工具调用。
- handler 不自行提交；业务写与 fenced complete 由 runner 统一提交。
- 外部副作用使用稳定幂等键；无法证明结果时进入 `uncertain`，禁止盲重试。
- 需要可靠投递时，业务事实与 Outbox Event 同事务登记。

## 5. 数据与迁移

- Schema 变化只通过 Alembic，应用启动不执行 DDL。
- 应用使用非超级角色；Migrator、应用和管理角色分离。
- 持久结构变化采用 expand/migrate/contract，兼容窗口有删除门槛。
- Append-only Event/Audit/Attempt 事实不得 UPDATE/DELETE 改写历史。
- 跨模块不得直接读取对方私有表或文件；通过拥有者 interface 或版本化 projection。
- migration 同时验证 fresh database、存量数据、约束和权限。

## 6. 配置与安全

- `.env.example` 只提供模板，不提供生产可用秘密或口令。
- 生产必需值显式注入并 fail-fast/readiness-fail；不使用开发默认值兜底。
- 地址、账号、路径和凭据不硬编码；内部模块接收验证后的配置对象。
- 生产代码/命令/文件执行只通过 OpenSandbox，无宿主 fallback。
- 日志、事件、错误、trace 和 Evidence 经过敏感数据过滤。
- 依赖例外必须记录 owner、工单、范围和到期时间，并进入 release gate。

## 7. 工具链与容器

- Python 模块使用 `uv sync --frozen`；前端使用 `npm ci`。
- Docker Compose 是本地/验收统一编排入口；生产使用受控 override。
- 核心进程提供 liveness；接流量模块提供 readiness，探针不泄露内部信息。
- Core 生产端口不发布到宿主机；服务间使用容器网络名，不硬编码宿主地址。
- 新依赖需有明确收益并更新 lockfile、审计和镜像构建验证。

## 8. 测试

- 纯规则直接测试真实实现；持久并发与 migration 使用真实 PostgreSQL。
- 跨模块变更验证 producer/consumer 两侧及错误、取消、未知版本和幂等。
- 修复缺陷时先建立可失败回归；删除旧路径前有等价性与使用证据。
- 高风险变更覆盖 kill/restart、lease loss、迟到写、外部结果未知和恢复。
- 合并前按 [`docs/TESTING.md`](../docs/TESTING.md) 运行风险相称的 test/E2E/gate。

## 9. 文档

- 文档分层、事实优先级和状态标记见 [`docs/README.md`](../docs/README.md)。
- 设计变化先更新 contract/ADR，再实现；实现完成后同步 SDD/TDD 状态。
- 端口、路径、命令和版本必须与 manifest、Compose 和代码一致。
- 服务 README 保留局部职责、入口和运行方式；完整协议链接到 `SPEC`，不重复复制。
- 新术语更新 `CONTEXT.md`；Agent 工作方式更新 `AGENTS.md`。

## 10. 完成与删除

- 新路径通过行为、错误、并发和恢复测试后才迁移流量。
- 兼容层必须有 owner、使用证据、删除条件和复核时间；禁止永久双实现。
- 替代完成后同步删除旧代码、配置、测试、文档和无用依赖。
- 完成定义以 [`docs/DEVELOPMENT.md`](../docs/DEVELOPMENT.md#10-完成定义) 为准。
