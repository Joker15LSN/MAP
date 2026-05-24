# Algorithm Service

MAP 算法服务，负责多智能体调度、工具调用与汇总生成。

## 职责边界

- 请求接入：`/global_domain/chat`、`/global_domain/chat/stream/v2|v3`
- 流程执行：场景识别 -> 子智能体调度 -> 工具调用 -> 汇总输出
- 元数据输出：流式 `meta` 事件，支撑前端问答树展示
- 状态记录：请求、智能体、工具事件写入 Mongo

## 本地开发

```bash
cd map_core
uv sync --dev
uv run python -m map_core.main --host 0.0.0.0 --port 10000
```

## 容器化运行

```bash
docker compose up -d algorithm-service
```

## 关键环境变量

- `ENV`：运行环境（`dev/test/pre/prod`）
- `POSTGRES_DSN`：PostgreSQL 连接串
- `MONGODB_URI`：Mongo 连接串
- `MONGODB_DATABASE`：Mongo 数据库名

## 相关文档

- 总体架构：`../SPEC/ARCHITECTURE.md`
- 工程规范：`../SPEC/STANDARDS.md`
