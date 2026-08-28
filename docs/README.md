# MAP 文档中心

本目录是 MAP 的说明性文档入口。权威协议、架构决策和工程规则仍位于
[`SPEC/`](../SPEC/README.md)；部署命令的最短入口仍是根目录
[`README.md`](../README.md)。

## 从哪里开始

| 读者 / 任务 | 首选文档 | 接着阅读 |
| --- | --- | --- |
| 第一次接触仓库 | [`ONBOARDING.md`](ONBOARDING.md) | [`CONTEXT.md`](../CONTEXT.md)、[`SDD.md`](SDD.md) |
| 理解系统与服务关系 | [`SDD.md`](SDD.md) | [`SPEC/ARCHITECTURE.md`](../SPEC/ARCHITECTURE.md)、ADR |
| 修改模块或跨服务流程 | [`TDD.md`](TDD.md) | 对应 [`SPEC/contracts/`](../SPEC/contracts/) |
| 本地开发 | [`DEVELOPMENT.md`](DEVELOPMENT.md) | 对应服务 README |
| 选择与执行测试 | [`TESTING.md`](TESTING.md) | [`e2e/README.md`](../e2e/README.md) |
| 部署、巡检、故障处置 | [`OPERATIONS.md`](OPERATIONS.md) | 根 README 的运维要点 |
| Agent 执行仓库任务 | [`AGENTS.md`](../AGENTS.md) | 本页和任务对应的权威文档 |
| 代码精简改造 | [`代码精简与可读性改造执行计划.md`](../TODO/代码精简与可读性改造执行计划.md) | `TDD.md` 的迁移约束 |

> 本仓库中的 **TDD** 指 Technical Design Document（技术设计文档）。测试设计与
> 测试执行规则单独维护在 [`TESTING.md`](TESTING.md)，避免缩写歧义。

## 文档分层与事实优先级

不同文档回答不同问题，不能用一类文档替代另一类：

1. **当前实现事实**：源码、依赖清单、Compose、数据库迁移和可重复测试结果。
2. **规范性契约**：[`SPEC/contracts/`](../SPEC/contracts/)；定义跨模块、跨服务可依赖的协议。
3. **已接受决策**：[`SPEC/adr/`](../SPEC/adr/)；说明为何选择某个方向及其后果。
4. **系统与技术说明**：本目录中的 SDD/TDD；解释当前结构、目标结构与迁移关系。
5. **执行计划**：[`TODO/`](../TODO/)；决定工作顺序，不自动代表功能已经实现。

发现冲突时，不要让说明性文档覆盖实现事实，也不要因为当前实现尚未完成就忽略
已接受契约。应记录差异，并把实现状态标为“过渡中”或“目标”。

## 状态标记

设计文档统一使用以下标记：

- **已实现**：当前代码和测试已提供该行为。
- **过渡中**：新旧路径并存，或实现只满足目标契约的一部分。
- **目标**：已由契约、ADR 或计划确定，但尚未完成。
- **退役**：不得用于新增实现；删除条件见计划或 ADR。

任何“已实现”声明都应能追溯到源码、迁移或测试；任何“目标”声明都应链接到契约、
ADR 或执行计划。

## 文档维护规则

| 变更类型 | 必须同步检查 |
| --- | --- |
| 新增或删除可部署模块、端口、数据存储 | SDD、OPERATIONS、Compose、`.env.example`、根 README |
| 修改跨服务请求、事件、状态或错误 | 对应 contract、TDD、契约测试、OpenAPI 快照 |
| 修改信任边界、凭据或执行能力 | ADR、安全文档、SDD、OPERATIONS、回归测试 |
| 修改数据库事实或迁移顺序 | contract、TDD、OPERATIONS、迁移和恢复测试 |
| 修改本地命令、工具链或版本要求 | DEVELOPMENT、TESTING、服务 README、CI/release gate |
| 引入新的领域术语 | [`CONTEXT.md`](../CONTEXT.md)，并在代码与契约中采用同一名称 |
| 改变 Agent 的工作方式 | [`AGENTS.md`](../AGENTS.md)，只保留指针和可验证约束 |

文档中的命令必须可从所注明的目录执行；链接、端口和路径必须在提交前验证。不要在
多个文档复制长命令清单或完整协议，首选链接到唯一事实源。
