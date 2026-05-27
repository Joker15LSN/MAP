# MAP Observability Frontend (`map-observability-frontend`)

MAP 观测前端，提供多智能体链路可视化与排障操作界面。

## Features

- 总览分析：请求量、成功率、平均耗时、Token 消耗
- 维度分析：按用户、智能体、工具查看趋势与分布
- 请求检索：按时间窗口、RID、状态过滤并查看详情
- 关联定位：RID 追踪、时间对齐、错误聚类、工具调用关联

## Tech Stack

- React + TypeScript + Vite
- Ant Design + `@ant-design/plots`
- `@agentscope-ai/design`
- 与主前端共享 `map-tree-core`

## Local Development

```bash
cd map-observability/map-observability-frontend
npm ci
npm run dev
```

## Build

```bash
npm run build
```

## Preview

```bash
npm run preview
```

## Test

```bash
npm run test
```

## Environment Variables

- `VITE_API_BASE_URL`（默认 `/api/v1`）
  - 同域部署：保持 `/api/v1`
  - 跨域部署：设置完整后端地址，例如 `https://your-domain/api/v1`

## Time Display Convention

- 默认展示时区：`Asia/Shanghai`
- 对关键时间同时展示本地时间与 UTC，便于跨系统排障
- 常规查询参数由前端转换为 UTC ISO 后发送

## References

- 观测后端：[`../map-observability-backend/README.md`](../map-observability-backend/README.md)
- 观测系统总览：[`../README.md`](../README.md)
