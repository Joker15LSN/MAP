# `map-tree-core`

业务前端与观测前端共享的 React 调用树呈现模块。它只负责把规范化的请求、Agent、Tool 与
模型调用数据渲染为树，不拥有会话状态、数据获取、身份或观测查询策略。

## Public interface

入口为 `src/index.ts`，主要实现为 `src/RequestCallTree.tsx`。包通过 file dependency 被两个
前端直接消费，peer dependencies 由消费方提供。

## 变更规则

- 只加入两个前端都需要、语义稳定的呈现能力；业务专属行为留在消费方。
- Props 变化需要同时迁移业务前端和观测前端。
- 不在此模块请求 BFF、观测 API 或读取环境变量。
- 修改后同时运行两个前端的 test/build 和根 bundle 检查。

测试选择见 [`docs/TESTING.md`](../../docs/TESTING.md)，共享模块原则见
[`SPEC/STANDARDS.md`](../../SPEC/STANDARDS.md#2-模块与依赖)。
