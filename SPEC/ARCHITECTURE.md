# MAP 架构设计

## 1. 目标

MAP（Multi Agent Path）采用“前端 - 业务后端 - 算法 - 观测”分层架构，目标是：

- 前端只面对业务接口，不直接依赖算法服务。
- 算法服务专注多智能体调度与推理执行。
- 观测服务独立沉淀运行日志、追踪链路和诊断能力。
- 所有服务通过 Docker Compose 统一编排，降低本地联调复杂度。

## 2. 四层架构

```text
[frontend-service]
        |
        v
[backend-service (BFF)]  ---> [observability-service]
        |
        v
[algorithm-service]
        |
        v
[PostgreSQL / MongoDB / 外部工具能力]
```

## 3. 服务职责

### frontend-service
- 提供前台问答与后台配置 UI。
- 管理本地会话状态、问答树展示、页面交互。
- 所有业务请求都发往 BFF（`backend-service`）。

### backend-service
- 作为 BFF 层，统一对外暴露前端需要的业务接口。
- 向下游转发到算法服务（同步/流式）。
- 管理侧配置（模型、智能体、权限、词库等）在此层维护。
- 提供心流运行时配置快照（`/api/admin/flow-runtime-snapshot`）供算法服务按需拉取并缓存。

### algorithm-service
- 处理场景识别、子智能体调度、工具调用与结果汇总。
- 输出 SSE 元数据，支撑前端问答树实时构建。
- 不直接暴露给浏览器前端。
- 同时提供全域链路（`/global_domain/*`）与心流链路（`/flow_domain/*`）。

### observability-service
- 聚合请求、智能体、工具维度的日志与指标。
- 提供链路追踪、错误聚类、趋势分析和诊断入口。
- 与主业务链路解耦，可单独扩缩与迭代。

## 4. 数据与依赖

- PostgreSQL：算法服务所需关系型数据。
- MongoDB：状态记录、观测分析主存储。
- `packages/map-tree-core`：前台与观测端共用的问答树构建能力。

## 5. 部署约束

1. Python 服务容器内统一使用 `uv`。
2. 服务之间通过容器名互访，不使用宿主机地址硬编码。
3. 新增服务必须在顶层 `docker-compose.yml` 注册，并补充 README。
