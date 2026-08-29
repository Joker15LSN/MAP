# MAP Core 执行模块（`map_core`）

Core 负责场景选择、Agent 调度、模型/工具调用、Flow 执行和 typed 流输出。它不面向浏览器；
生产由 BFF/Worker 通过受保护的内部网络调用。

当前模块仍保留 legacy 与 AgentScope 双引擎，并直接保存部分 Mongo 运行记录和沙箱 ledger。
目标边界是消费不可变 Runtime Snapshot、返回 typed events/results，而不直接写 Canonical
Run/Event 事实。详见 [`docs/SDD.md`](../docs/SDD.md) 与
[`docs/TDD.md`](../docs/TDD.md#4-core-技术设计)。

## 当前职责

- 全域：`/global_domain/*`；
- 心流：`/flow_domain/*`，ScenarioHub / SkillHub / Flow 策略；
- AgentRuntime：按 `MAP_AGENT_ENGINE` 选择 legacy 或 AgentScope；
- Model/Tool/MCP 调用与统一运行身份传播；
- OpenSandbox client、身份、ledger、fencing 和 crash recovery；
- Mongo 运行记录与可选 OTel trace；
- 健康与受服务身份保护的 sandbox/internal 入口。

`python_exec_tool`、`bash_tool`、本地文件和宿主 stdio MCP 能力已删除或 fail-closed。生产不得
恢复宿主 fallback；见 [`ADR-0001`](../SPEC/adr/ADR-0001-disable-host-execution-capabilities.md)。

## 代码地图

```text
map_core/
├── main.py                  # 组合根、生命周期、router、telemetry
├── routers/                 # HTTP/SSE adapter
├── service/
│   ├── global_domain.py     # 全域编排
│   ├── flow_domain.py       # Flow 图执行
│   ├── agent_runtime.py     # 双引擎选择 seam（过渡中）
│   ├── agent/               # legacy runtime
│   ├── agentscope2/         # AgentScope adapter
│   ├── scenario_hub.py / skill_hub.py
│   └── sandbox_*.py         # OpenSandbox 与调用 ledger
├── schema/                  # 请求、事件和运行数据结构
├── observability/           # OTel 与运行观测
├── utils/
│   ├── model_invocation/    # 单一 typed ModelInvocation（invoke/stream/structured/tool）
│   └── llm_engine.py        # B6 待删兼容薄壳（不再被 production import）
└── tests/
```

## 主要入口

- `POST /global_domain/chat`
- `POST /global_domain/chat/stream/v2`
- `POST /global_domain/chat/stream/v3`
- `POST /flow_domain/chat/v1`
- `POST /flow_domain/chat/stream/v1`
- `GET /health`

这些是当前 Core 协议。浏览器面向的稳定协议由 BFF 拥有；Canonical Event 目标契约见
[`SPEC/contracts/run.md`](../SPEC/contracts/run.md)。

## 运行流概览

全域路径处理输入/附件与上下文、选择 Scenario/Agent、执行模型和工具、汇总并产生流事件。
心流路径从 BFF 获取当前 Flow 配置快照，解析 Scenario，构建执行图，按依赖推进节点并执行
repair/fallback 策略。

当前流事件包含 `start/meta/content_delta/done/error` 及心流 `meta.phase`；BFF 负责把 Core
协议投影为浏览器契约。EOF 不代表成功。

## 本地运行

Python >= 3.13，当前锁定 `agentscope==2.0.4`。

```bash
cd map_core
uv sync --frozen
uv run python -m map_core.main --env dev --host 0.0.0.0 --port 10000
```

数据库、模型和沙箱能力所需配置必须显式注入；缺少高权限能力配置时应 fail-closed。

Compose：

```bash
docker compose up -d algorithm-service
```

生产 override 会移除 Core 宿主机端口。

## 测试

```bash
cd map_core
uv sync --frozen
uv run ruff check .
uv run pytest
```

Agent 引擎、运行身份、OpenSandbox、宿主边界、MCP egress、事件和 trace 均有定向测试。真实
OpenSandbox 环境验收不能仅由 double 代替，见 [`docs/TESTING.md`](../docs/TESTING.md)。

## 关键配置

- `MAP_ENV` / 兼容 `ENV`：环境；
- `POSTGRES_DSN`：当前沙箱 ledger 等关系事实；
- `MONGODB_URI` / `MONGODB_DATABASE`：当前运行记录；
- `MAP_AGENT_ENGINE=legacy|agentscope`：双引擎开关；
- `MAP_LLM_BASE_URL` / `MAP_LLM_MODEL` / `MAP_LLM_API_KEY`：模型；
- `MAP_BFF_API_ORIGIN` / `MAP_FLOW_CONFIG_SNAPSHOT_URL`：当前 Flow 配置来源；
- `MAP_OPENSANDBOX_URL` / `MAP_OPENSANDBOX_API_KEY`：沙箱服务；
- `MAP_SANDBOX_SERVICE_CREDENTIALS` / `MAP_SANDBOX_SERVICE_AUDIENCE`：沙箱入口服务身份；
- `MAP_OTEL_ENABLED` 与 OTLP 配置：可选 trace。

完整变量与优先级见根 [`.env.example`](../.env.example)。AgentScope 单引擎、ModelInvocation、
Runtime Snapshot 和 Event 投影的收敛顺序见
[`TODO/代码精简与可读性改造执行计划.md`](../TODO/代码精简与可读性改造执行计划.md)。

## 相关文档

- [`SPEC/ARCHITECTURE.md`](../SPEC/ARCHITECTURE.md)
- [`SPEC/contracts/run.md`](../SPEC/contracts/run.md)
- [`docs/DEVELOPMENT.md`](../docs/DEVELOPMENT.md)
- [`docs/OPERATIONS.md`](../docs/OPERATIONS.md)
