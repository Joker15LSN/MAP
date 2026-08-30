# MAP 迁移台账（Migration Ledger）

> 建立依据：`代码精简与可读性改造执行计划.md` Step 0 第 5 项。
> 基线：分支 `qoder/dev-modelscope`，提交 `e781ecd84d4fa54ddf5aa4dd33c9c321b4031f53`。
> 状态词汇：`跟踪中`（尚未排空）、`排空中`（替代路径已承载流量）、`已删除`（实现已物理移除）。
> 已删除项保留一行历史与回滚点，不保留旧实现文件。

## 使用规则

1. 删除任何退役对象前，必须在本表补全 owner、consumer、流量/非终态数据、截止、删除门槛和回滚方式。
2. `rg` 零引用不能证明 route、反射、配置或外部流量为零；流量型对象必须有 HAR/Compose/CI 证据。
3. owner 为“待认领”的对象，在进入对应 Step 的 destructive cleanup 前必须由模块 owner 认领。
4. 每步执行后在“执行记录”追加命令、退出码与证据路径，不覆盖历史行。

## 总表

| ID | 退役对象 | 对应 Step / 交付批次 | owner | 状态 |
| --- | --- | --- | --- | --- |
| ML-01 | legacy `/api/chat*`、global/flow/master 兼容路径 | Step 4 / PR-G | 本计划执行者（删除待流量证据） | 排空中 |
| ML-02 | conversation message proxy（`stream_conversation_message`、进程内 stream registry、message-level 终态） | Step 2–4 / PR-F、PR-G | 本计划执行者（新路径已切换，旧代码停用待删） | 排空中 |
| ML-03 | BFF Effect guard 与 Core Sandbox ledger 的双份 lease/fence/unknown 语义 | Step 3 / PR-E | 本计划执行者（destructive cleanup 待排空） | 排空中 |
| ML-04 | legacy/AgentScope 双引擎、`MAP_AGENT_ENGINE`、`RuntimeAgent` seam、`_compat_session` | Step 5 / PR-H | 本计划执行者（PR-H1 已切换，PR-H2 待证据） | 排空中 |
| ML-05 | `LLMEngine` 宽接口、同义命名族、sync path | Step 6 / PR-I | 本计划执行者（B0–B6 完成，AC-03 CI/durable 待补） | 已删除 |
| ML-06 | 文件快照 `admin_state.json` 主路径、startup reconciler、generic AdminState 更新 | Step 7 / PR-J | 本计划执行者（J1–J7b 完成，AC-06/07/08 与 generated DTO 待续） | 已删除（资产语义待续） |
| ML-07 | Mongo `state_store`、Webhook handler、`fire_and_forget`、telemetry collections | Step 8 / PR-K | 本计划执行者（K1–K8 完成，K9 retention/drop 待 owner） | 已停写（drop 待 retention） |
| ML-08 | Step 1 dead code / unused import 清单（下表） | Step 1 / PR-B | 本计划执行者 | 已删除（C901 债留 Step 9） |

## ML-01 legacy chat/global/flow/master 路径

- **对象**：`map-business-backend/app/api/chat.py`；OpenAPI 中的
  `POST /api/chat`、`POST /api/chat/stream/v2`、`POST /api/chat/flow/v1`、
  `POST /api/chat/stream/flow/v1`；前端 `features/chat/` 的 controller/reducer。
- **consumer**（源码证据，基线 e781ecd8）：
  - `map-business-frontend/src/features/chat/useChatController.ts` 按 requestMode
    使用四个 legacy endpoint（sync/stream 各两个）；
  - `map-business-frontend/src/app/App.tsx` 与 `router.tsx` 默认路由到
    `ChatView`，Conversation UI 由 `VITE_MAP_CONVERSATIONS_ENABLED` 开关（默认关闭）；
  - `map-business-frontend/src/features/admin/useAdminController.ts`、
    `useFlowStrategyController.ts`、`components/AppSidebar.tsx` 复用 `chat/`
    的 reducer/flow 工具；
  - BFF contract test `tests/contracts/test_openapi_contract.py` 冻结
    `LEGACY_CHAT_PATHS`。
- **流量/非终态数据**：legacy 为当前默认 UI 主路径；尚未采集浏览器 HAR 证明
  consumer=0。非终态数据包括 `streaming`/处理中的 message 行与 Core 进程内状态。
- **截止**：P1-API-01（AC-API-01 至 AC-API-07）；删除动作对应 Step 4 完成条件。
- **删除门槛**：Canonical Run 全量承载 global/flow、history、trace/source、SSE、
  stop/done、失败终态、反馈、刷新恢复、attachment；浏览器 HAR 只有批准的
  `/api/v1` Run/Conversation 路径；前端、contract、OpenAPI、测试、文档零引用。
- **回滚**：PR-G 独立回滚；删除前 routes 与前端 flag 均在 git 历史，可 revert；
  flag 回退窗口为删除后的下一次部署前。

## ML-02 conversation message proxy

- **对象**：`app/services/conversation_service.py::stream_conversation_message`、
  `app/services/stream_registry.py`、`app/services/message_reconciler.py`、
  `app/services/run_event_stream.py` 的 test-only row helper。
- **consumer**：`app/api/conversations.py`；`app/main.py` 装配
  `message_reconciler`；`app/workers/main.py` worker 路径；
  `tests/integration/test_conversation_stream.py`。
- **流量/非终态数据**：`messages.status` 为 message-level 终态；断流/停止依赖
  进程内 registry；跨实例 stop 尚未由 proxy 支持（Step 2 迁移目标）。
- **截止**：Step 2（Run Attempt 深化）后、Step 4（前端接管）删除被替代部分。
- **删除门槛**：`(run_id, seq)` durable Event replay 取代 proxy 的 raw Core SSE
  拼接；stop/done/timeout/retry/reconcile 五方竞态由 Canonical Run 覆盖；
  `run_event_stream` consumer=0 或被 Run persistence adapter 吸收。
- **回滚**：PR-F 与 PR-G 分离；旧 service 保留到前端全量切换后，可独立 revert。

## ML-03 BFF Effect guard / Core Sandbox ledger 双账本

- **对象**：`map-business-backend/app/workers/job_runner.py` 的 EffectGuard 路径与
  `map_core/map_core/service/sandbox_ledger.py`（+ reconciler）中重复的
  lease、fencing、unknown outcome、identity、错误映射。
- **consumer**：worker `run_effect_once` / effect handler；
  `map_core/tests/test_sandbox_ledger.py`、`test_sandbox_reconciler.py`、
  `test_sandbox_crash_recovery.py`；BFF `tests/integration/test_effect_protocol_windows.py`、
  `test_job_lease_fencing.py`、`test_sandbox_worker_identity.py`。
- **流量/非终态数据**：PG `map_control` 中 Effect/Job ledger 行与 Core 直写的
  PG sandbox ledger；两处 durable 事实当前并存（与 ADR-0002 目标冲突）。
- **截止**：Step 3；唯一 durable owner 决定先于删除。
- **删除门槛**：replay/conflict/takeover/unknown/terminal 从同一 module interface
  验证；故障矩阵通过；worker/router/tool/reconciler 不再各自解释同一协议；
  core 不直接写 Run/Event PG。
- **回滚**：实现与 destructive cleanup 分开提交（PR-E 内两个 commit）。

## ML-04 双引擎与 engine switch

- **对象**：`map_core/map_core/service/agent_runtime.py` 的
  `MAP_AGENT_ENGINE` / `RuntimeAgent` Protocol seam；legacy `service/agent/`
  与 `service/agentscope2/` 双实现；`_compat_session` 与 legacy result/action
  translation；engine-switch tests/fixtures。
- **consumer**：`agent_runtime.py` 调用方（global/flow/master dispatcher 链）；
  `map_core/tests/test_engine_switch.py`；config/env 中的 `MAP_AGENT_ENGINE`。
- **流量/非终态数据**：legacy 非终态 Run（排空门槛见删除门槛）。
- **截止**：Step 5，依赖 P1-EVAL-01、P1-OTEL-01、P1-CTX-01。
- **删除门槛**：12/12 cross-engine golden 与 canary 非劣；连续两个 lease TTL 无
  新 legacy Run；request/schema/UI/env 无 engine switch；dispatcher 不依赖
  AgentScope concrete type。
- **回滚**：PR-H destructive cleanup 独立 commit；legacy engine 保留至排空验证后。

## ML-05 LLMEngine 宽接口

- **对象**：`map_core/map_core/utils/llm_engine.py`（1,872 行）的 sync/async/
  stream/structured 命名族与 exception swallowing；`object.__new__`、
  `_prepare/_coerce/_ainvoke_once` 的 implementation tests。
- **consumer**：38 个文件引用 `LLMEngine`/`llm_engine`（含 tests），25 个
  production 文件 import 该模块；scene selector、summarizer、AgentScope model、
  业务 agent、content review 等调用族。
- **流量/非终态数据**：无迁移数据；行为冻结见 Step 6 工作项 1。
- **截止**：Step 6，依赖 Steps 2、5 与 P1-OTEL-01。
- **删除门槛**：所有 production caller 只依赖单一 typed ModelInvocation
  interface；direct provider SDK import 只在 adapter；AC-CLEAN-LLM-01..03。
- **回滚**：按 caller 批次迁移，旧入口随批次删除；PR-I 实现与清理分离。

## ML-06 文件快照配置主路径

- **对象**：`admin_state.json` 文件主路径与 shared volume、startup reconciler、
  `ConfigRepository.load/update(updater)` 与 `AdminStateStore.prepare_update/
  apply_prepared` 的 seam 不一致、前端 `AdminApi.ts` raw state/setter。
- **consumer**：`app/api/admin_config.py`、`admin_assets.py`、`admin_master.py`、
  `deps.py`、`services/config_mutation.py`、`store.py`、`main.py`；
  core 通过 BFF runtime snapshot 消费心流配置。
- **流量/非终态数据**：当前 `admin_state.json` 文件与 PG mutation/audit 事实
  需要 version 迁移与重放演练；历史 Run 需固定 snapshot id/digest。
- **截止**：Step 7，依赖 P0-CFG-AUTH-01、Step 2。
- **删除门槛**：PG production adapter + in-memory test adapter 证明 seam；
  caller 不再触碰 prepare/apply/hash sequencing；Run 全程一个 snapshot
  id/digest；AC-CONFIG-01..09、AC-CLEAN-BUILD-01。
- **回滚**：PR-J 实现与文件路径删除分离；旧文件 schema 保留 export/restore
  演练记录。

## ML-07 Mongo state_store 与 fire_and_forget

- **对象**：`map_core/map_core/service/state_store.py` 的 Mongo handlers、
  queue、singleton、`fire_and_forget`、`record_event(dict)` 装饰器与
  `MONGODB_*_COLLECTION` telemetry 配置。
- **consumer**：19 个 core 文件引用 `state_store`/`GlobalAgentStateStore`
  （llm_engine、agent_dispatcher、agent_runtime、flow_domain、global_domain、
  master_pipeline、agentscope2/*、tool_runtime、tool_executor 等）。
- **流量/非终态数据**：Mongo collections `agent_executions`、`tool_call_records`、
  `request_records`、`llm_call_records` 与 agent memory；退役前需
  export/digest/restore 演练并按 retention 保留。
- **截止**：Step 8，依赖 Steps 2、6 与 P1-OBS-01。
- **删除门槛**：Run correctness 只依赖 PG durable truth，诊断只依赖 OTel
  projection；普通 agent/model/tool 不再反向依赖 Mongo；AC-CLEAN-STATE-01..03。
- **回滚**：PR-K destructive cleanup 独立 commit；collection drop 前完成
  restore 演练并保留演练证据。

## ML-08 Step 1 零风险清理清单

基线数字：core F401=64、F841=4（合计 68）；BFF 与观测后端为 0。C901>12 债：
core 27 / BFF 6 / obs 5（见 `architecture-baseline.json`，本次不处理，Step 9
按 deletion test 逐个收窄）。私有跨 router import 债 3 条（Step 2/4 处理）。

| 对象 | consumer 证据（rg + route/UI/config） | 处理决定 |
| --- | --- | --- |
| `_looks_like_production`（`app/main.py`） | 仅定义处 | 删除 |
| `_safe_state_call` + `DEFAULT_STATE_STORE_TIMEOUT_S`（`state_store.py`） | 仅定义处 | 删除 |
| `MONGODB_STATE_RECORD_COLLECTION`（`config/common.py`） | 仅定义处；字符串 `agent_call_states` 无其他引用 | 删除 |
| `temp_to_tool_result`（`kb_tools/base.py`） | 两个 import 均为未使用（F401） | 删除函数与 import |
| `BusinessExecutionGraphStore`（`business_execution_graph_store.py`） | 仅 `flow_domain.py` 单 caller，调用 `create`/`append_repair_node` | deletion test 后 inline 到 `flow_domain.py` 并删除模块 |
| 未注册 demo route（`global_domain_router.py` 注释块 + `GlobalDomainDemoResponse`） | 注释代码，`include_router` 不注册；schema 导入为 F401 | 删除注释与 schema 导入 |
| `RequestDetailPage.tsx`（观测前端） | 无任何路由/组件引用；同目录 requestDetail 组件由 `RequestDetailDrawer` 承载 | 删除文件 |
| `kb_tools/scripts/*`（5 个手工 HTTP 脚本，含硬编码员工号/内网 URL） | 无 import consumer，无文档命令引用 | 删除 |
| `zhiwen_agent/scripts/demo.py`、`single_agent_test.py` | 无 import consumer，无文档命令引用 | 删除 |
| `remote_config_compare/scripts/*`（4 个功能性 env 对比工具） | 无 import consumer，但属于可运行运维工具 | 迁至顶层 `examples/remote_config_compare/` 并更新运行说明 |
| `_wenshu_split_question/usage_examples.py` | 无 import consumer；含内网 Milvus 地址 | 删除 |
| 生产文件尾部手工 `__main__` demo（按逐个 deletion test 判定） | 见各文件扫描记录 | 运维入口保留（`main.py` 等）；纯手工 demo 删除；工具脚本按 ML-08 上一条迁移 |
| 60 个 F401 + 4 个 F841 | 见 `architecture-baseline.json` 与 Step 1 执行记录 | 逐项修复后 core 移除 `F401`/`F841` 全局 ignore |
| 注释掉的旧实现、重复 docstring、无效常量、无用 alias | Step 1 执行记录逐项列 consumer | 删除 |

### ML-08 执行记录

- `_looks_like_production`：已删除。consumer 证据：`rg` 全仓仅定义处，无调用。
- `_safe_state_call` + `DEFAULT_STATE_STORE_TIMEOUT_S`：已删除。consumer=0。
- `MONGODB_STATE_RECORD_COLLECTION`：已删除。字符串 `agent_call_states` 全仓无其他引用。
- `temp_to_tool_result`：已删除（含 `uploaded_file_tools.py` / `knowledge_base_tools.py`
  的两处未使用 import）。consumer=0。
- `BusinessExecutionGraphStore`：deletion test 通过（单 caller 只调 `create`/
  `append_repair_node`，复杂度就地内化）→ `GraphRuntimeState` 内联进 `flow_domain.py`，
  模块物理删除。
- 未注册 demo route：`global_domain_router.py` 中 `/demo` 与注释掉的 `/chat/stream` 已删；
  `GlobalDomain.demo`、`GlobalDomainDemoResponse`（router/schema/service 三处）已删；
  文件尾 `global_domain.py` 手工 `main()` 已删。
- `RequestDetailPage.tsx`：已删除。观测前端无任何路由/组件引用；`npm test` 44/44 通过、
  `npm run build` 通过。
- `kb_tools/scripts/*`（5 个手工 HTTP 脚本）与 `zhiwen_agent/scripts/*`（2 个）已删除。
  consumer 证据：无 import/文档/CI 引用；`map_core/pyproject.toml` 中为规避其重名
  basename 的 pytest 注释同步更新。
- `remote_config_compare/scripts/*`（4 个功能性运维工具）：`git mv` 至
  `examples/remote_config_compare/`，运行说明改为
  `PYTHONPATH=map_core uv run --project map_core python -m examples.remote_config_compare.*`；
  `dump_scene_conf.py` 输出目录改为 cwd 下 `dumped_configs/`；两个工具 `--help` smoke 通过。
- `_wenshu_split_question/usage_examples.py`：已删除（含内网 Milvus 地址）。
- 文件尾手工 `__main__` demo：删除 12 处（`config/base_config.py`、`utils/llm_engine.py`、
  `global_domain.py`、`scene_selector.py`、`prompt/scene_classification_prompt.py`、
  `kb_tools/uploaded_file_tools.py`、`zhiwen_agent/agent.py`、
  `annual_performance_agent.py`、`_wenshu_split_question/split_question.py`（含
  `_test_split_question`）、`web_search_agent.py`、`efficiency_pi_agent.py`、
  `wenshu_agent.py`、`_result_calculator/_final_result_parser.py`（含
  `test_parse_final_results`）。保留的运维入口：`map_core/main.py::cli_main`、
  `scene_agent_config_provider.py::_run_cli`。
- 注释掉的旧实现：删除 `global_domain.py` 的 `record_tool_call`、`remote_api.py` 的
  `_get_headers` 与 `search_knowledge` overload 注释、`ask_database_agent.py` 两组注释、
  `efficiency_pi_agent.py` 的 `@record_agent_call` 注释、`wenshu_agent.py` 的
  `_merge_extra` 注释、`sacne_config_fetch.py` 的同步版本注释。
- F401/F841：core 归零（含 tests）；`map_core/pyproject.toml` 移除全局 `F401`/`F841`
  ignore；`tool_call_agent.py` 保留两处显式 `# noqa: F401`，注释标明 public re-export
  seam（`Tool`/`AgentTool`/`RuntimeSchemaTool`/`ToolSet` 与两个 post-summary prompt
  被 9+ 个 caller 消费，改 caller 会扩散 knowledge）。
- 验证：core `ruff check map_core tests` 全绿；core pytest（无 PG）290 passed/2 个
  sandbox-ledger PG 测试因无 PG 失败；obs 前端 44/44 + build 通过；BFF/obs ruff 通过。
  最终 release gate 结果见“执行记录”。

## 执行记录

| 日期 | Step | 动作 | 命令 | 结果 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 2026-08-24 | 0 | fresh PostgreSQL 上首次 gate 基线 | `bash scripts/release_gate.sh`（UV/npm cache 指向 `tmp/cache/`） | 失败：BFF `test_v1_errors_use_standard_envelope_not_fastapi_detail` 依赖前序测试迁移，fresh DB `UndefinedTableError` | `tmp/gate-logs/gate-summary.json` |
| 2026-08-24 | 0 | 修复测试的隐藏 DB 依赖（显式请求 `_engine` fixture）后定向验证 | `uv run pytest tests/contracts/test_openapi_contract.py::test_v1_errors_use_standard_envelope_not_fastapi_detail -q` | fresh DB 上由 fail → pass | 本表与 Step 0 状态块 |
| 2026-08-24 | 0 | 接入 architecture gates 后最终 release gate | `bash scripts/release_gate.sh`（cache 环境同首行） | PASS：steps=29 failed=0 skipped=1；BFF 532 passed；core 290 passed/9 skipped；obs 12 passed；前端 34/44 passed + build | `tmp/gate-logs/gate-summary.json` |
| 2026-08-24 | 1 | Step 1 清理后最终 release gate | `bash scripts/release_gate.sh`（一次性 postgres:16 + 三角色） | PASS：steps=29 failed=0 skipped=1；BFF 532 passed；core 290 passed/9 skipped；obs 12 passed；前端 34/44 passed + build；architecture-gate 通过 | `tmp/gate-logs/gate-summary.json`（同目录 architecture-gate.log） |
| 2026-08-24 | 2/PR-C | Canonical Run skeleton：`app/runs/`、Alembic `runs`+`run_events`、`/api/v1/runs*`、双 store/transport adapter | `uv run pytest tests/test_runs_* -q` + `uv run pytest tests/integration/test_runs_pg_store.py -q` + 全量 release gate | 接口/路由/transport 20 passed；真实 PG 7 passed；全量 gate PASS steps=29 failed=0；OpenAPI snapshot +288（批准差异） | `tmp/gate-logs/gate-summary.json`；plan Step 2 状态块 |
| 2026-08-29 | 5/PR-H1 | `agent_execution` 公开模块（execute/stream、cancel/hooks/usage），dispatcher/master/flow 默认 AgentScope，legacy 内部回滚开关保留 | core 定向 55+9 passed；全量 release gate（fresh PG）PASS steps=29 failed=0 | `tmp/gate-logs/gate-summary.json`（HEAD `2a1fead9`）；plan Step 5 状态块 |
| 2026-08-26 | 4/PR-F | TurnApplication 单事务 turn、/turns 端点、message.delta(ADR-0003)、messages.run_id、Run cancel watcher、前端 runApi/runProjection 切换与 flag 删除、legacy 文件停用保留 | 定向 BFF turns/run tests + 前端 npm test/build + 全量 release gate（含 fresh PG） | BFF 全量、前端 37 passed + build、bundle 通过；gate PASS steps=29 failed=0 | `tmp/gate-logs/gate-summary.json`；plan Step 4 状态块 |
| 2026-08-26 | 3/PR-E | SandboxRemote 双 adapter、effect.* 事件与 project_effects、RunCommand.kind sandbox_invocation、core /sandbox/exec|reconcile 无状态化、core client trust_env=False | `uv run pytest tests/test_runs_* -q`；core `uv run pytest tests/test_sandbox_*.py -q`；全量 release gate（含 fresh PG） | BFF 29 passed；core sandbox 45 passed/2 skipped；gate PASS steps=29 failed=0 | `tmp/gate-logs/gate-summary.json`；plan Step 3 状态块 |
| 2026-08-25 | 2/PR-D | RunWorker 生产循环（worker main 双循环）、legacy JobRunner 只 claim 已注册类型、jobs.max_attempts 重试、reclaim 不重复 run.started、fail_attempt internal seam | `uv run pytest tests/test_runs_interface.py tests/test_runs_core_transport.py tests/test_runs_routes.py -q`；`uv run pytest tests/integration/test_runs_pg_store.py -q`；全量 release gate | 21 接口/transport/路由 passed；10 PG passed；gate PASS steps=29 failed=0 | `tmp/gate-logs/gate-summary.json`；plan Step 2 状态块 |
| 2026-08-29 | 6/PR-I | B0：`utils/model_invocation`（typed invoke/provider seam/openai adapter）+ `llm_engine` 薄壳 + contract tests；B2–B5：25 个 production caller 全量迁移到 `ModelInvocation.invoke`；B1：构造点切 `from_config`；B0 后追加 legacy empty-content/tolerant-JSON parity 修复 | 每批 `uv run ruff check .` + `uv run pytest -q`（fresh PG）+ `architecture_gate.py --baseline-sha HEAD`；最终全量 `bash scripts/release_gate.sh` | B0 335 passed、B1–B5 各 337–338 passed；production `llm_engine` import 清零；gate PASS steps=29 skipped=1 failed=0；B6 删除待独立提交 | `tmp/gate-logs/gate-summary.json`（HEAD `7abc09e4`）；plan Step 6 状态块 |
| 2026-08-29 | 6/PR-I/B6 | 物理删除 `utils/llm_engine.py`（587 行兼容壳）与 `test_shell_parity.py`；三个 agentscope/execution 测试改共享 `tool_outcome()`；删除 sync shell 的 stream-span 测试；旧符号 grep 清零 | `uv run ruff check .` + `uv run pytest -q`（fresh PG）+ `architecture_gate.py --baseline-sha HEAD` | 334 passed；gate PASS；旧符号（llm_engine/LLMEngine/LLMResponse/ToolCallResponse/LLMMessage）零结果 | plan Step 6 状态块；commit `3e112a6c` |
| 2026-08-30 | 7/PR-J1..J6 | Runtime Snapshot PG 三表 + internal 读路由 + 双 adapter 生命周期 + admin 写切 apply_change + admin 生命周期路由 + Run 固定两列 + core pinned transport（删 current fetch/static fallback） | 每批 BFF/core `ruff` + `pytest`（fresh PG）+ `architecture_gate.py --baseline-sha HEAD` | BFF 621→622 passed；core 354 passed；production 无 file JSON 主路径新增；每批 gate PASS | plan Step 7 状态块；commits `00c4be5c..0e096a7c` |
| 2026-08-30 | 7/PR-J7a/J7b | PG 单行 AdminState + async 读路径 + `apply_change` 单事务（不再 pending/rename）；随后独立 commit 删除 `store.py`/`ConfigMutationService`/mutation 表/file reconciler/`MAP_BFF_STATE_FILE`/shared volume/Dockerfile `/app/data` | BFF/core `ruff`+`pytest`（fresh PG）+ architecture gate；最终全量 `bash scripts/release_gate.sh` | BFF 615 passed；core 354 passed；部署面 grep 零 `admin_state.json|/app/data|MAP_BFF_STATE_FILE`；release gate `steps=29 skipped=1 failed=0`（HEAD `46ca90f4`） | `tmp/gate-logs/gate-summary.json`；plan Step 7 状态块；commits `8fe0874f`/`dd6950c4`/`46ca90f4` |
| 2026-08-30 | 8/PR-K1..K6 | K1/K2 typed emitter + legacy Mongo sink；K3/K4 全部 core caller 切 typed emit；K5 core service-identity NDJSON stream；K6 BFF worker 默认 `TypedCoreRunStream`（旧 SSE adapter 保留） | 每批 core/BFF `ruff`+`pytest`（fresh PG）+ architecture gate | core 370→403 passed；BFF 624 collected；golden 12/12 桥接等价；每批 gate PASS | plan Step 8 状态块；commits `b4cbce1a..f40018b7` |
| 2026-08-30 | 8/PR-K7/K8 | golden fixtures 重定型为 `execution_events`（167 条 1:1）；随后独立 commit 删除 `state_store.py`/legacy sink/Mongo telemetry 配置并让 Mongo boot 可选 + OTel projector | core `ruff`+`pytest`（fresh PG）+ security scan + architecture gate | core 403→377 passed；security scan exit 0；grep 零旧 telemetry 符号 | plan Step 8 状态块；commits `b625612c`/`b6b01e9d` |
