# MAP 架构约束

本文只固定跨模块不可违反的约束。系统全景、当前实现与目标差异见
[`docs/SDD.md`](../docs/SDD.md)，实现 seam 和迁移顺序见
[`docs/TDD.md`](../docs/TDD.md)。

## 1. 模块与调用方向

```text
Browser -> Business Frontend -> BFF -> durable command / Worker -> Core
                                                   Core -> OpenSandbox / Model / Tool
Observability Frontend -> Observability Backend -> projections
```

当前 BFF 仍有直连 Core 的 Conversation/Chat 兼容路径。它是“过渡中”，新增执行能力不得只
落在兼容路径。

## 2. 架构不变量

1. 浏览器只访问 BFF；不能直连 Core 或私有数据存储。
2. Public `/api/v1` 与 internal `/internal/v1` 分层；internal 入口必须验证服务身份。
3. Workspace、Principal、资源所有权和权限在 BFF 统一检查。
4. Canonical 执行模型为
   `Conversation -> Run -> Step/Attempt -> Invocation/Approval/Artifact -> Event/Checkpoint`。
5. PostgreSQL 是 Canonical Run 生命周期的 durable truth；持有有效 lease 的 Run worker 是
   生命周期唯一写者。
6. BFF 只原子创建 Run+command、读取和发出取消命令；Core 消费 Runtime Snapshot 并返回
   typed events/results，不直接写 Canonical Run/Event。
7. 命令、代码和文件执行只通过 OpenSandbox；生产没有宿主 fallback。
8. 外部副作用必须使用稳定幂等键、fencing 和结果不确定态。
9. 事件、审计和尝试事实不可被迟到写或回滚覆盖；兼容投影不能成为第二真相。
10. 跨模块协议版本化，未知 major fail-closed；错误和遥测必须脱敏。

Run 细节以 [`contracts/run.md`](contracts/run.md) 为准，身份以
[`contracts/identity.md`](contracts/identity.md) 为准，安全执行以
[`ADR-0001`](adr/ADR-0001-disable-host-execution-capabilities.md) 为准。

## 3. 当前数据角色

- PostgreSQL：Workspace、Conversation、Message、Feedback、Job、Effect、Outbox、配置
  mutation 和审计事实；目标再承载 Run/Event/Checkpoint/Invocation。
- `admin_state.json`：当前管理配置快照，是迁移中的实现，不是目标配置事实模型。
- MongoDB：当前 Core 运行记录和观测查询；目标作为 Canonical Event 的观测投影。
- OTel 后端：trace/span；通过稳定运行标识与业务事实关联，不取代业务状态。

物理数据库可访问不等于模块拥有写权限。所有权矩阵见
[`docs/SDD.md`](../docs/SDD.md#6-当前数据所有权)。

## 4. 部署约束

- Compose 是本地与验收编排基线；生产必须叠加 `docker-compose.prod.yml` 的强约束。
- Migrator 使用独立数据库角色，应用模块不自动执行 DDL。
- Core 的生产端口不发布到宿主机；OpenSandbox 与 internal 接口位于私有网络。
- Python 依赖使用各模块 `uv.lock`，前端使用 lockfile；发布使用冻结安装。
- 新模块、端口、profile、环境变量或数据存储必须同步 SDD、OPERATIONS、Compose、
  `.env.example` 和根 README。

## 5. 架构变化

改变单写者、数据所有权、信任边界、生产执行能力、持久协议或可部署模块时必须新增/修订
ADR。Accepted ADR 索引见 [`adr/README.md`](adr/README.md)。
