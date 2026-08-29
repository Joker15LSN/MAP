# MAP 技术设计文档（TDD）

- 文档类型：Technical Design Document
- 状态：Living
- 最后核对：2026-08-24
- 适用范围：仓库组织、模块接口、并发与持久化策略、兼容与迁移设计

测试策略不在本文展开，见 [`TESTING.md`](TESTING.md)。系统级上下文、数据所有权和
目标架构见 [`SDD.md`](SDD.md)。

## 1. 技术设计原则

本文使用以下统一词汇：

- **模块（module）**：隐藏设计决策、对外提供连贯接口的实现单元；
- **接口（interface）**：调用方需要理解的承诺总和，不只是一组函数签名；
- **seam**：允许实现被替换或被独立测试的接缝；
- **adapter**：把外部协议或存储细节翻译为内部接口的实现；
- **深度**：模块隐藏的复杂度与接口复杂度之比；
- **leverage**：一个能力被复用或承载变化的程度；
- **locality**：完成一次典型修改所需理解和触碰的范围。

设计优先级是正确性、可恢复性和安全，其次才是减少代码量。精简的目标是删除无证据的
抽象、重复策略和兼容分支，让关键策略只出现一次，而不是把不同职责挤进同一个文件。

## 2. 仓库组成与组合根

| 模块 | 组合根 | 主要内部区域 |
| --- | --- | --- |
| BFF | `map-business-backend/app/main.py` | `api/`、`services/`、`repositories/`、`runtime/`、`db/` |
| Worker | `map-business-backend/app/workers/main.py` | `workers/job_runner.py`、repositories、effect / reconcile handler |
| Core | `map_core/map_core/main.py` | `routers/`、`service/`、`service/agent/`、`service/agentscope2/`、`utils/` |
| 业务前端 | `map-business-frontend/src/main.tsx` | `app/`、`features/chat/`、`features/conversation/`、`features/admin/`、`api/` |
| 观测后端 | `map-observability/map-observability-backend/app/main.py` | `routers/`、`services/`、`repositories/`、`core/` |
| 观测前端 | `map-observability/map-observability-frontend/src/` | 页面、请求 client、共享调用树视图 |

组合根负责装配具体 adapter、进程生命周期和配置，不应承载业务规则。跨区域导入应沿
`router -> application module -> repository/client adapter` 方向流动；数据库会话、HTTP client
和环境变量解析不能扩散进领域规则。

## 3. BFF 技术设计

### 3.1 HTTP 与身份入口

`app/main.py` 创建 FastAPI 应用、生命周期、遥测和路由。`app/api/deps.py` 与 `app/core/`
提供用户身份、服务身份、权限和脱敏能力。

接口分层：

- 浏览器协议：`/api/v1`，使用用户身份与统一错误 envelope；
- 内部协议：`/internal/v1`，只接受服务身份；
- 兼容协议：`/api/chat*` 与既有管理路径，处于退役轨道。

Router 只做协议解析、身份/权限调用、application module 调用和响应投影。它不应直接编码
状态机、重试、存储提交或上游事件容错规则。

### 3.2 Conversation 模块（PR-F：Run-backed turn 已切换，legacy proxy 待删）

核心路径：

- `app/turns/`：`TurnApplication`（start_turn/stop_turn/get_turn_projection）
  与单事务 PG store；Run-backed turn 的唯一 BFF 入口；
- `app/api/conversations.py`：`POST /conversations/{id}/turns`、旧 proxy 薄
  兼容与 stop 适配（legacy 无 run 消息保留旧终态写，待 PR-G 删除）；
- `app/services/conversation_service.py`、`stream_registry.py`、
  `message_reconciler.py`：legacy 停用保留，待流量排空后删除；
- `app/repositories/conversations.py`、`app/db/models/conversation.py`：
  消息/会话持久模型（messages.run_id 已加，PR-F）。

前端默认 ConversationView，协议知识收敛于 `api/runApi.ts` 与
`features/conversation/runProjection.ts`；旧 `features/chat` 停用保留。
删除前必须保持 [`conversation.md`](../SPEC/contracts/conversation.md)
规定的 SSE、幂等和终态语义，并满足 PR-G 四证据（HAR/OpenAPI/路由/bundle）。

### 3.3 Job / Worker 模块（当前基础）

`app/workers/job_runner.py`、`app/repositories/jobs.py` 与 Job/Effect 数据模型共同提供：

- claim 与数据库时间驱动的 lease；
- `lease_owner + attempt` fencing；
- 独立短事务 heartbeat；
- handler 写入与 fenced complete 的事务原子性；
- Effect dispatch token、幂等键和 `uncertain` 结果。

这是一组高价值不变量。精简时可以统一命名和提取策略，但不得把 fencing、取消、结果不
确定态简化掉。目标 Run worker 应复用这些已验证机制，不再新建第二套租约协议。

### 3.4 配置与审计模块（当前过渡）

`app/services/config_mutation.py` 编排快照变更、mutation 事实和审计链；
`app/repositories/config.py` 定义了基础读取/更新 seam，而当前编排还依赖文件 adapter 的
附加能力。这意味着公开协议和真实调用关系不一致。

收敛方向：

1. 先按“管理配置事实”“原子快照写入”“审计 append”拆清内部职责；
2. 由组合根装配具体 adapter；
3. application module 只依赖它实际使用的最小接口；
4. 版本化配置落地后删除文件特有接口和兼容分支。

不要为了形式统一提前建立通用 Repository 基类。只有独立变化或可替换需求已出现时才保留
seam。

### 3.5 Run 运行时基础（契约已实现；持久最小事实集与 worker 循环已实现）

`app/runtime/state_machine.py`、`event_envelope.py` 与 `error_mapping.py` 已提供目标契约的
纯规则模块：状态转移、事件版本、payload/ArtifactRef 分界和 typed error 映射。它们应保持
无网络、无数据库、无全局配置，以便作为写入前的唯一验证入口。

PR-C + PR-D（Step 2）已落地：

- `app/runs/`：BFF 面 `RunApplication`（create/get/cancel/replay），worker 面
  `RunWorker`（claim → execute → settle，默认 handler 驱动
  `HttpCoreRunStream`），handler 是 typed `AsyncIterator[CoreItem]`；
- `RunStore` internal seam：`PgRunStore`（SKIP LOCKED、数据库时钟、CAS、
  1:1 复用 jobs 租约）与 `InMemoryRunStore`（同 contract，禁 SQLite）；
- `map_control.runs` / `map_control.run_events`（UNIQUE(run_id,seq)）与
  public `/api/v1/runs*` 四路由（未切流量）；
- `app/workers/main.py` 同时运行 legacy `JobRunner`（只 claim 已注册
  handler 类型，`job_type="run"` 不进入 legacy）与 `RunWorker.run_forever`；
- retry/timeout 事实源复用 `jobs.max_attempts`（默认 3，退避 2**attempt 秒）：
  transport error / handler 异常可重试，CoreOutcome failed 与耗尽重试为终态；
  回收 claim 不重复发 `run.started`。

Run 模块隐藏：Run+command+idempotency+job 的原子创建；claim/lease/fencing；
Event seq 分配与追加；取消命令与终态 CAS；typed CoreItem 到 durable fact 的
翻译。五方竞态/crash takeover 的完整 AC-RUN 矩阵以 CI/E2E 证据为准；
Checkpoint/ArtifactRef 表在获得第一个 production writer 的步骤按需增加。

## 4. Core 技术设计

### 4.1 HTTP adapter 与运行入口

`map_core/main.py` 装配生命周期、路由和遥测。`routers/` 把 HTTP/SSE 协议翻译为运行调用：

- `global_domain_router.py`：全域问答；
- `flow_domain_router.py`：心流执行；
- `sandbox_router.py`：受控沙箱入口；
- `system_router.py`：健康与系统信息；
- `master_pipeline_router.py`：主编排入口。

这些 router 不应成为执行策略的第二事实源。运行身份从入口冻结后，应通过显式上下文传播，
避免从模块级全局变量重新推导。

### 4.2 AgentRuntime seam（过渡中）

PR-H1 新增 `service/agent_execution/`：caller 只学
`AgentExecutionSpec` 与 `AgentRuntime.execute/stream`，framework 类型关在模块内部；
dispatcher/master/flow 默认走该模块，legacy 引擎仅作为组合根内部回滚开关保留。
`service/agent_runtime.py`（旧 seam）、`service/agent/` 与 `service/agentscope2/`
仍保留到 PR-H2（canary/排空证据后删除）。

目标是保留单一 AgentScope runtime interface，并把 legacy 仅有行为通过 golden trace
迁移后删除。迁移必须满足：

- 相同输入产生等价的业务语义和终态；
- Tool Invocation、模型流和取消均产生统一 typed event；
- 场景配置不再通过引擎条件分支解释；
- `MAP_AGENT_ENGINE` 的兼容窗口、使用证据和删除条件明确。

### 4.3 场景、流程与技能

当前关键实现包括：

- `service/scenario_hub.py`、`scenario_resolver.py`、`scene_selector.py`；
- `service/skill_hub.py`、`dynamic_tools.py`；
- `service/flow_domain.py`、`flow_config_provider.py`；
- `service/global_domain.py`、`master_pipeline.py`、`agent_dispatcher.py`。

这些模块的主要风险是同一个选择/编排策略分散在 router、helper、provider 和 agent 中。
修改前先确定单一策略拥有者，再移动调用方。不要直接创建通用“manager”或“大 facade”把
分散代码包起来；那只会增加接口而不隐藏复杂度。

### 4.4 模型调用（当前热点）

`utils/model_invocation/`（PR-I B0–B6）已把同步、异步、流式、结构化输出、重试和观测
等变化维度收敛为单一 async typed `invoke`；旧 `utils/llm_engine.py` 兼容壳与
sync/chat/simple_chat/invoke 同义族已删除，仓库中零旧符号引用。ModelInvocation 的
稳定语义：

- 规范化请求与 typed outcome / terminal event；
- 流式 chunk / 终态协议；
- 超时、重试和取消；
- 用量、错误和 span 归属；
- provider 差异的 adapter（direct openai 只在 `openai_compatible.py`）。

迁移按调用族逐个进行（B2–B5 已全部完成，B6 删除旧壳）；禁止建立
同时覆盖所有现存参数的“新”万能接口。新 caller 只学 `invoke`，不得直连
provider SDK 或自建 retry/usage 逻辑。

### 4.5 Sandbox（当前基础，待归一）

`opensandbox_client.py`、`sandbox_auth.py`、`sandbox_tools.py` 与 `sandbox_ledger.py` 提供远程
沙箱访问、身份验证、稳定执行键、lease/fencing 和 crash recovery。`mcp_egress.py` 管理
MCP 出站限制，`disabled_capabilities.py` 保证历史宿主能力 fail-closed。

PR-E（Step 3 实现面）已把沙箱执行收敛为 Canonical Run 内的 Effect：

- BFF `app/runs/sandbox_effects.py`：effect.* 事件构造与 `project_effects`
  重放投影，执行键规则唯一（request_digest/create_key/execute_key）；
- BFF `app/runs/sandbox_remote.py`：`SandboxRemote` remote-owned seam
  （Http + InMemory adapter），六 header/路径/错误投影只在 adapter；
- core `/sandbox/exec` 与新增 `/sandbox/reconcile` 已无状态化（不写 PG）；
- `RunCommand.kind` 支持 `sandbox_invocation`，RunWorker 按 kind 选择
  handler；sandbox 执行是 Run 内 effect.* durable facts。

目标（剩余 destructive cleanup 另行排空）：

- 旧 `sandbox_invocations` 与 BFF EffectGuard 停止新写后按
  停写→迁移→观察→drop 退役；
- 只有一个执行键生成规则、一个超时与未知结果语义；
- OpenSandbox adapter 不泄露服务协议给 Agent；
- 无沙箱或身份不完整时默认拒绝。

## 5. 前端技术设计

业务前端以 `src/app/App.tsx` 和 `router.tsx` 组合页面：

- `features/chat/`：兼容问答 UI；
- `features/conversation/`：持久 Conversation UI，当前由开关控制；
- `features/admin/`：模型、Agent、场景与心流管理；
- `api/`：BFF client、DTO 与 SSE parser。

前端只依赖 BFF。跨页面共享的是稳定呈现能力，而不是业务状态：

- 调用树呈现复用 `packages/map-tree-core/`；
- SSE parser 在 `src/api/sse.ts` 单点处理帧与错误；
- 目标 `/api/v1` DTO 由 public OpenAPI 生成，禁止手写同义类型；
- Chat 与 Conversation 收敛后删除旧 controller/reducer，而不是长期双向同步。

观测前端只访问观测后端；业务控制操作不得从观测 UI 绕过 BFF。

## 6. 跨模块接口

| 接口 | 拥有者 | 消费者 | 事实源 / 规则 |
| --- | --- | --- | --- |
| Public HTTP `/api/v1` | BFF | 业务前端 / 企业代理 | OpenAPI + `SPEC/contracts/` |
| Internal HTTP `/internal/v1` | BFF / Core | 服务模块 | internal OpenAPI + service identity |
| Conversation SSE | BFF | 业务前端 | `contracts/conversation.md` |
| Canonical Event `event.v1` | Run 模块 | BFF 投影 / 观测 | `contracts/run.md` |
| Runtime Snapshot | BFF 配置模块 | Run worker / Core | 版本与内容哈希；目标契约待补齐 |
| Job / Effect | Worker 模块 | BFF application modules | `contracts/job-outbox.md` |
| Trace context | 请求入口 | 下游 HTTP / Model / Tool | W3C trace context + 稳定运行标识 |

新增字段时优先扩展拥有者的版本化接口，不允许消费者直接读取拥有者私有表或文件。

## 7. 并发、事务与错误

### 并发

- 状态机写入使用 expected-state 条件更新；
- Worker 写入使用 lease owner、attempt 或 dispatch token fencing；
- Event 序号在同一持久化模块分配并受唯一约束；
- stop/done、timeout/result 和 reconcile/result 竞争只能产生一个终态；
- 进程内 Registry 不是多实例真相，横向扩容前必须替换为共享取消通道或粘性路由。

### 事务

- 事务覆盖一个业务不变量，不跨越慢 HTTP、模型或工具调用；
- Job handler 不自行提交；业务写与 fenced complete 由 runner 统一提交；
- 需要可靠通知的业务写与 Outbox Event 同事务；
- 文件或外部系统无法参加数据库事务时，显式记录 pending/applied/failed/uncertain 并对账。

### 错误

- Public / internal API 使用 `{code, message, details, request_id}`；
- 流式错误以合法 SSE `error` 终止；EOF 不是成功；
- adapter 把 provider 异常翻译为稳定内部错误，调用方不判断厂商文本；
- 鉴权、凭据、版本和执行能力缺失时 fail-closed。

## 8. 配置与依赖

- 环境变量只在 settings / composition root 读取，内部模块接收已验证配置对象；
- `.env.example` 只提供非秘密模板；生产必需值在启动或 readiness 时校验；
- Python 使用各自 `uv.lock` 和 `uv sync --frozen`，前端使用 lockfile 和 `npm ci`；
- 新依赖需证明现有标准库或已安装依赖无法合理解决，并进入依赖审计；
- 共享 package 只承载稳定、真实复用的能力，禁止把偶然相似代码提升为公共层。

## 9. 可测试 seam

适合独立替换的 seam：

- PostgreSQL repository 与 Mongo query adapter；
- Core HTTP client、模型 provider、OpenSandbox client 和外部工具 adapter；
- clock、ID、lease owner 和取消信号；
- public/internal HTTP 与 application module；
- typed event 生产与持久 Event writer。

不建议替换的内部细节：纯状态机、事件校验、错误映射、JSON diff 和脱敏函数；这些应直接
使用真实实现做快速确定性测试。测试替身不得绕过生产身份、状态机或 fencing 规则。

## 10. 收敛顺序与删除规则

技术收敛按以下依赖顺序执行：

1. 固定行为基线与无效代码清单；
2. 建立 Canonical Run/Run Attempt，并归一 Sandbox Invocation/Effect；
3. 迁移 Conversation，删除 `/api/chat*` 与旧前端状态管理；
4. AgentScope 单引擎；
5. ModelInvocation 小接口；
6. 版本化配置与 Runtime Snapshot；
7. Canonical Event 投影和 Mongo 角色收敛；
8. 最后再做目录、命名、保留策略与公共包精简。

每项以 [`代码精简与可读性改造执行计划.md`](../TODO/代码精简与可读性改造执行计划.md)
中的删除门槛和验证命令为准。不得在替代路径尚未通过等价性、故障与恢复测试前删除兼容
路径，也不得在兼容路径迁移完成后无限期保留双实现。

## 11. 技术变更检查单

实现跨模块变更前回答：

1. 谁拥有这个决策和事实？
2. 调用方真正需要的最小接口是什么？
3. 是否已有第二套状态机、重试、身份或错误策略？
4. 新 seam 是否有独立变化来源或可测试替换需求？
5. 失败、取消、重试、未知结果和迟到写如何处理？
6. 当前行为、目标契约和迁移步骤是否分别标明？
7. 哪些旧代码和文档会在本次变更后删除？

完成标准见 [`DEVELOPMENT.md`](DEVELOPMENT.md#10-完成定义)。
