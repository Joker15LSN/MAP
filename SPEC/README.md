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
