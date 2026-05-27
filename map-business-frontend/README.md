# MAP Frontend Service (`map-business-frontend`)

MAP 业务前端，包含“问答工作台 + 管理配置台”两大界面。

## Functional Scope

- 问答工作台：
  - 全域模式与心流模式切换
  - 流式回答展示
  - Trace / Source 信息展示
  - 模式内会话历史隔离
- 管理配置台：
  - 模型中心
  - 智能体配置
  - 权限与策略配置
  - 心流策略、场景包、技能描述配置

## Frontend Architecture

- 技术栈：React + TypeScript + Vite
- UI：`@agentscope-ai/design` + Ant Design
- 共享能力：`packages/map-tree-core`
- 数据边界：仅调用 BFF（`map-business-backend`），不直连算法服务

## Runtime API Routing

- 全域模式 -> `POST /api/chat/stream/v2`
- 心流模式 -> `POST /api/chat/stream/flow/v1`
- 管理端配置 -> `GET/PUT/POST /api/admin/*`

## Local Development

```bash
cd map-business-frontend
npm ci
npm run dev
```

默认地址：`http://localhost:5174`

## Build

```bash
npm run build
```

## Preview

```bash
npm run preview
```

## Environment Variables

- `VITE_MAP_BFF_API_ORIGIN`：BFF 地址（默认 `http://localhost:18080`）

## Engineering Notes

- 新增页面时保持“前端只走 BFF”的边界。
- 心流模式默认策略应来自管理端快照，而非前端硬编码。
- 任何模式切换功能必须保证历史会话按模式隔离。

## References

- 根文档：[`../README.md`](../README.md)
- BFF 服务：[`../map-business-backend/README.md`](../map-business-backend/README.md)
