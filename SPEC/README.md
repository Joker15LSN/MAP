# MAP Specification (`SPEC`)

`SPEC` 目录用于维护 MAP 的架构与工程规范文档，是跨服务协作的设计基线。

## Document Map

- [`ARCHITECTURE.md`](ARCHITECTURE.md)
  - 四层系统架构
  - 服务边界与调用关系
  - 数据依赖与部署约束
- [`STANDARDS.md`](STANDARDS.md)
  - 命名规范
  - 接口规范
  - 配置规范
  - 容器与文档规范
- [`contracts/`](contracts/)（R1 整改后新增的权威契约）
  - [`identity.md`](contracts/identity.md)：认证模式、可信代理身份、权限矩阵、服务身份、错误 envelope
  - [`conversation.md`](contracts/conversation.md)：会话/消息模型、冻结 SSE 事件集、状态机、幂等
  - [`feedback.md`](contracts/feedback.md)：反馈当前事实模型、API、脱敏、迁移
  - [`audit.md`](contracts/audit.md)：config_audit_events/config_mutations、写流程、hash chain、JSON Patch
  - [`job-outbox.md`](contracts/job-outbox.md)：job 状态机、lease/fencing、outbox、worker 运维

## When To Update

以下场景必须同步更新 `SPEC`：

- 新增服务或调整服务边界
- 新增跨服务 API，或修改请求/响应契约
- 运行链路（全域/心流）发生关键流程变化
- 配置体系与部署方式发生变化

## Change Rules

1. 先更新设计文档，再提交实现代码。
2. 文档中的端口、路径、服务名必须与 `docker-compose.yml` 和代码一致。
3. 任何与可观测性相关的字段变更，必须同时说明事件语义与兼容策略。

## Recommended Reading Order

1. `ARCHITECTURE.md`
2. `STANDARDS.md`
3. 各服务 README（根目录 README 提供索引）
