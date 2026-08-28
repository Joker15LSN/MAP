# MAP 业务前端（`map-business-frontend`）

React/TypeScript/Vite 业务 UI，提供问答与管理配置。浏览器只访问 BFF，不直连 Core、数据库
或 internal 接口。

## 当前功能与迁移状态

- `features/chat/`：全域/心流兼容问答，调用 `/api/chat*`；处于退役轨道；
- `features/conversation/`：持久 Conversation、刷新恢复、停止和 Feedback，调用 `/api/v1`；
- `features/admin/`：模型、Agent、权限、场景、Skill 和 Flow 管理；
- `api/sse.ts`：共享 SSE 分帧与错误解析；
- `packages/map-tree-core`：与观测前端共享调用树呈现。

Conversation UI 由 `VITE_MAP_CONVERSATIONS_ENABLED=true` 启用，当前默认关闭。目标是在
Canonical Run 落地并完成等价验证后，迁移全部流量并删除 Chat controller/reducer，而不是
长期维护双状态。

## 代码地图

```text
src/
├── main.tsx
├── app/                    # Shell 与视图装配
├── api/                    # BFF client、DTO、SSE parser
├── features/chat/          # 兼容问答
├── features/conversation/  # 新 Conversation UI
├── features/admin/         # 管理配置
└── test/                   # MSW 与测试设置
```

## 本地运行

```bash
cd map-business-frontend
npm ci
npm run dev
```

默认地址：`http://localhost:5174`。开发代理/跨域 BFF 地址由
`VITE_MAP_BFF_API_ORIGIN` 配置。

## 测试与构建

```bash
npm test
npm run build
```

修改 `packages/map-tree-core` 时还必须运行观测前端 test/build 和根 bundle 检查。完整策略见
[`docs/TESTING.md`](../docs/TESTING.md)。

## 前端约束

- 新业务接口只加在 BFF Public API，不绕过身份与 Workspace 所有权；
- SSE 的 EOF、`error` 和唯一终态由统一 parser/controller 处理；
- 目标 `/api/v1` DTO 由 Public OpenAPI 生成，不手写同义类型；
- 策略与权限来自 BFF/Runtime Snapshot，不在 UI 复制；
- Chat/Conversation 兼容逻辑必须有迁移和删除条件。

## 相关文档

- [`SPEC/contracts/conversation.md`](../SPEC/contracts/conversation.md)
- [`docs/TDD.md`](../docs/TDD.md#5-前端技术设计)
- [`docs/DEVELOPMENT.md`](../docs/DEVELOPMENT.md)
- [`map-business-backend/README.md`](../map-business-backend/README.md)
