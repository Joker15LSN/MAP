# MAP Algorithm Service (`map_core`)

`map_core` 是 MAP 的算法执行层，负责场景识别、多智能体调度、工具调用、心流编排与结果汇总。

## Service Responsibilities

- 统一算法入口：
  - 全域链路：`/global_domain/*`
  - 心流链路：`/flow_domain/*`
- 智能体执行：场景识别、业务智能体调用、工具编排、总结生成。
- 心流治理：ScenarioHub + SkillHub + FlowPolicy（含 fallback、预算、修复策略）。
- 运行记录：请求/智能体/工具事件写入 Mongo，支撑观测平台。

## Key Interfaces

### Global Domain

- `POST /global_domain/chat`
- `POST /global_domain/chat/stream/v2`
- `POST /global_domain/chat/stream/v3`

### Flow Domain

- `POST /flow_domain/chat/v1`
- `POST /flow_domain/chat/stream/v1`

### SSE Event Model

- 通用事件：`start` / `meta` / `content_delta` / `done` / `error`
- 心流关键 phase（`meta.phase`）：
  - `flow_mode_initialized`
  - `flow_policy_hit`
  - `scenario_resolved`
  - `flow_graph_built`
  - `flow_node_started`
  - `flow_node_result`
  - `flow_repair_applied`

## Execution Flow

### 全域模式

1. 请求预处理（历史、附件、上下文）。
2. 场景识别与智能体选择。
3. 子智能体工具调用与结果聚合。
4. 汇总智能体生成最终响应。
5. 写入 `request.start/request.end` 与执行事件。

### 心流模式

1. 从 BFF 拉取心流运行时快照（可缓存）。
2. ScenarioResolver 命中场景并构建执行图。
3. SkillHub 按 `agent/scenario/user/tenant` 计算授权工具。
4. FlowOrchestrator 按节点依赖顺序执行。
5. 节点失败或不确定时按策略执行 repair。
6. 不可执行时按 `fallback_to_global` 决定回退或硬失败。

## Module Layout

```text
map_core/
├── map_core/main.py
├── map_core/routers/
│   ├── global_domain_router.py
│   └── flow_domain_router.py
├── map_core/service/
│   ├── global_domain.py
│   ├── flow_domain.py
│   ├── scenario_hub.py
│   ├── skill_hub.py
│   ├── flow_config_provider.py
│   └── agent/
├── map_core/schema/
└── tests/
```

## Local Development

```bash
cd map_core
uv sync --dev
uv run python -m map_core.main --host 0.0.0.0 --port 10000
```

## Docker Run

```bash
docker compose up -d algorithm-service
```

## Test

```bash
cd map_core
uv run pytest -q
```

## Environment Variables

### Runtime Basics

- `ENV`：`dev/test/pre/prod`
- `POSTGRES_DSN`：PostgreSQL 连接串
- `MONGODB_URI`：MongoDB 连接串
- `MONGODB_DATABASE`：MongoDB 数据库名

### LLM

- `MAP_LLM_BASE_URL`
- `MAP_LLM_MODEL`
- `MAP_LLM_API_KEY`
- `MAP_LLM_TEMPERATURE`
- `MAP_AGENT_LLM_TEMPERATURE`
- `MAP_SCENE_SELECTOR_LLM_TEMPERATURE`
- `MAP_SUMMARIZATION_LLM_TEMPERATURE`

### Flow Config Source

- `MAP_BFF_API_ORIGIN`：BFF 地址
- `MAP_FLOW_CONFIG_SNAPSHOT_URL`：默认 `${MAP_BFF_API_ORIGIN}/api/admin/flow-runtime-snapshot`
- `MAP_FLOW_CONFIG_CACHE_TTL_S`：快照缓存秒数
- `MAP_FLOW_CONFIG_FETCH_ENABLED`：是否启用远端拉取

## Operational Notes

- 同步接口（`/chat`）会消费完整事件流并返回最终文本。
- 为保证观测链路完整，`request.end` 在响应 `done` 之前落库。
- 请求日志默认对密钥字段脱敏（`api_key/token/authorization`）。

## References

- 系统架构：[`../SPEC/ARCHITECTURE.md`](../SPEC/ARCHITECTURE.md)
- 工程规范：[`../SPEC/STANDARDS.md`](../SPEC/STANDARDS.md)
