# Frontend Service

MAP 前端服务，负责前台问答与后台管理页面展示。

## 技术栈

- React + TypeScript + Vite
- `@agentscope-ai/design`
- 共享问答树：`packages/map-tree-core`

## 职责边界

- 提供前台问答 UI（历史、对话、思考过程、问答溯源）。
- 提供后台配置 UI（模型、智能体、权限、运营等页签）。
- 仅调用 `backend-service`，不直连 `algorithm-service`。

## 本地开发

```bash
cd map-business-frontend
npm ci
npm run dev
```

默认端口：`5174`

## 容器化运行

由仓库根目录统一编排：

```bash
docker compose up -d frontend-service
```

## 关键环境变量

- `VITE_MAP_BFF_API_ORIGIN`：BFF 地址，默认 `http://localhost:18080`
