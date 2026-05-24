# MAP 日志分析前端（React + Spark-Design）

前端用于日志检索、指标可视化、请求追踪和 Grafana/Mongo 关联定位。

## 功能

- 总览 / 用户 / Agent&Tool / 请求检索
- 关联定位页面（时间对齐、RID 追踪、错误聚类）
- 明暗主题切换（暗色模式可读性已适配）

## 本地运行

```bash
npm install
npm run dev
```

## 构建

```bash
npm run build
```

## 环境变量

- `VITE_API_BASE_URL`：默认 `/api/v1`
  - 同域部署：保持 `/api/v1`
  - 跨域部署：设置为 `https://api.your-domain.com/api/v1`

## 时间展示约定

- 页面默认展示时区：`Asia/Shanghai`（UTC+8）。
- 时间字段统一主显示东八区，次显示 UTC（用于排障与日志比对）。
- DatePicker 选择本地时间后，前端会转换为 UTC ISO 发送到常规分析接口。
- 关联定位接口使用 `start_local/end_local + tz`，并在页面展示本地时间与 UTC 双口径。
