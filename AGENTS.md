# MAP Agent 指令

## 先读什么

开始任何任务前先读 [`docs/README.md`](docs/README.md)，再按任务选择：

- 术语：[`CONTEXT.md`](CONTEXT.md)
- 系统/所有权：[`docs/SDD.md`](docs/SDD.md)
- 模块/迁移：[`docs/TDD.md`](docs/TDD.md)
- 权威协议：[`SPEC/contracts/`](SPEC/contracts/)
- 已接受决策：[`SPEC/adr/`](SPEC/adr/)
- 测试：[`docs/TESTING.md`](docs/TESTING.md)
- 开发与完成标准：[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)
- 部署/恢复：[`docs/OPERATIONS.md`](docs/OPERATIONS.md)
- 精简顺序：[`TODO/代码精简与可读性改造执行计划.md`](TODO/代码精简与可读性改造执行计划.md)

`TODO` 是计划，不代表已实现。当前行为以源码、Compose、迁移和测试为准；跨模块承诺以
contract/ADR 为准。发现冲突时记录漂移，不要静默改写事实。

## 必守不变量

1. 浏览器只访问 BFF；internal 路由必须使用服务身份。
2. Workspace/Principal 所有权检查不得绕过。
3. 状态转移只有一个事实源；终态不得被迟到写覆盖。
4. Worker 写入遵守 lease/attempt fencing；外部副作用使用稳定幂等键并保留 `uncertain`。
5. 管理写入继续经过 `ConfigMutationService` 和 append-only 审计链。
6. 生产命令、代码和文件执行只通过 OpenSandbox；缺配置时 fail-closed。
7. 不提交秘密、`.env`、缓存、构建产物或 `tmp/` Evidence。
8. 不把目标架构写成已完成，不把兼容路径作为新增功能默认落点。

## 工作顺序

1. 定位任务对应的当前实现、contract/ADR、测试和计划项。
2. 明确目标、非目标、拥有该决策的模块和删除对象。
3. 修改前运行最小基线或补回归测试。
4. 保持改动局部；优先复用已有规则模块，拒绝无真实替换需求的抽象。
5. 逐个迁移调用者；替代路径验证后删除旧代码、旧配置、旧测试和旧文档。
6. 按 [`docs/TESTING.md`](docs/TESTING.md) 选择风险相称的验证。
7. 同步 contract/ADR、SDD/TDD、README 或运维文档，并检查链接与命令。
8. 检查工作树，只交付预期差异和可追溯验证结果。

## 完成标准

任务未满足以下条件时不要标为完成：行为与契约一致；正常/错误/并发/恢复路径按风险验证；
旧路径已删除或有证据化退役门槛；文档状态同步；未解决风险明确；工作树无意外内容。
