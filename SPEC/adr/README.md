# MAP 架构决策记录（ADR）

ADR 记录影响多个模块、难以轻易逆转或改变信任/数据所有权的决策。协议的完整字段与状态表
放在 `SPEC/contracts/`，ADR 只说明背景、决策、替代方案取舍和后果。

## 决策索引

| ADR | 状态 | 决策 |
| --- | --- | --- |
| [`ADR-0001`](ADR-0001-disable-host-execution-capabilities.md) | Accepted | 删除/禁用宿主执行能力，生产执行只允许 OpenSandbox |
| [`ADR-0002`](ADR-0002-canonical-run-event-artifact-contract.md) | Accepted | Canonical Run/Event/Artifact、单写者与 BFF→Worker→Core 边界 |
| [`ADR-0003`](ADR-0003-message-delta-run-projection.md) | Accepted | Canonical Event 扩展 `message.delta`：Run 投影的增量内容事实 |
| [`ADR-0004`](ADR-0004-runtime-snapshot-pinning.md) | Accepted | PG 单行 AdminState + 不可变 Runtime Snapshot + Run 固定 id/digest + core fixed-id 读取 |
| [`ADR-0005`](ADR-0005-core-typed-execution-events.md) | Accepted | core typed execution events + service-identity NDJSON 流 + Mongo telemetry 停写/retention |

## 何时新增 ADR

- 新增或改变可部署模块及同步依赖；
- 改变领域事实、单写者或数据库所有权；
- 改变身份、信任、凭据、沙箱或外部副作用策略；
- 选择新的持久协议、事件版本或恢复模型；
- 推翻或显著修订 Accepted ADR。

小范围实现细节写入 `docs/TDD.md` 或代码；单纯工作清单写入 `TODO/`。

## 状态与修改规则

状态使用 Proposed、Accepted、Deprecated、Superseded。Accepted ADR 不直接重写其历史决策；
方向变化时新增 ADR，并在旧文档标注 `Superseded by ADR-xxxx`。实现尚未完成时，ADR 仍可为
Accepted，但 SDD/TDD 必须明确标为“目标”或“过渡中”。
