# MAP 规范与决策（SPEC）

`SPEC` 保存跨模块可以依赖的权威契约、工程规则和架构决策。系统说明和开发/运维指南在
[`docs/`](../docs/README.md)，统一领域术语在 [`CONTEXT.md`](../CONTEXT.md)。

## 文档地图

### 权威契约

| 契约 | 范围 |
| --- | --- |
| [`contracts/run.md`](contracts/run.md) | Canonical Run/Event/Artifact、状态机、版本、错误与幂等 |
| [`contracts/identity.md`](contracts/identity.md) | 认证、可信代理、权限、服务身份和错误 envelope |
| [`contracts/conversation.md`](contracts/conversation.md) | 当前 Conversation/Message、SSE、停止、幂等和恢复 |
| [`contracts/job-outbox.md`](contracts/job-outbox.md) | Job lease/fencing、Effect、Outbox 与 Worker 运维语义 |
| [`contracts/audit.md`](contracts/audit.md) | 配置 mutation、append-only hash chain、JSON Patch 和脱敏 |
| [`contracts/feedback.md`](contracts/feedback.md) | Feedback 当前事实、API、权限和迁移 |

### 架构规则与决策

- [`ARCHITECTURE.md`](ARCHITECTURE.md)：不可违反的系统边界，以及当前/目标架构关系；
- [`STANDARDS.md`](STANDARDS.md)：模块、接口、配置、数据、测试和文档规则；
- [`adr/README.md`](adr/README.md)：Accepted ADR 索引和新增规则。

### 说明性文档

- [`docs/SDD.md`](../docs/SDD.md)：系统上下文、当前链路、数据所有权和目标模型；
- [`docs/TDD.md`](../docs/TDD.md)：仓库实现、module/interface/seam/adapter 与迁移设计；
- [`docs/TESTING.md`](../docs/TESTING.md)：测试层级、命令和故障矩阵；
- [`docs/OPERATIONS.md`](../docs/OPERATIONS.md)：部署、升级、恢复和分诊。

## 事实与状态

- contract 描述跨模块规范性承诺；ADR 描述已接受方向；
- 源码、迁移、Compose 和测试描述当前实现；
- `TODO/` 描述工作顺序，不自动代表行为已存在；
- 若 contract 已接受但实现尚未完成，SDD/TDD 必须标为“目标”或“过渡中”。

发现冲突时记录并修复文档漂移，不通过降低 contract 要求来掩盖未完成实现，也不把目标
行为写进当前操作指南。

## 必须更新 SPEC 的变化

- 新增/删除模块，或改变数据所有权、单写者与调用方向；
- 新增跨模块 API/Event，或改变请求、响应、状态和错误语义；
- 改变身份、权限、服务身份、执行能力、凭据或副作用策略；
- 改变持久事实、迁移、恢复、幂等、lease/fencing；
- 改变运行链路中的兼容窗口或版本策略。

## 修改规则

1. 协议或方向变化先修改 contract/ADR，再实现并添加两侧验证。
2. 端口、路径、服务名和命令必须与 Compose、manifest 和代码一致。
3. Event/API 变化必须说明版本、未知版本、错误、取消与兼容策略。
4. Accepted ADR 的方向变化用新 ADR supersede，不重写历史理由。
5. 不在多个 README 复制完整协议；局部文档链接到本目录的唯一事实源。

## 推荐阅读顺序

1. [`docs/README.md`](../docs/README.md)
2. [`CONTEXT.md`](../CONTEXT.md)
3. [`ARCHITECTURE.md`](ARCHITECTURE.md)
4. 与任务相关的 contract 和 ADR
5. [`docs/TDD.md`](../docs/TDD.md) 与对应服务 README
