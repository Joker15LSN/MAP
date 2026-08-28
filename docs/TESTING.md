# MAP 测试策略与执行指南

- 状态：Living
- 最后核对：2026-08-24
- 目标：用最小但充分的测试组合证明行为、契约、故障恢复和发布可接受性

## 1. 原则

- 测试公共行为和不变量，不绑定无意义的内部调用顺序。
- 纯规则用快速单元测试；数据库并发、事务和迁移使用真实 PostgreSQL。
- 跨服务契约在拥有者和消费者两侧验证，不能只测 happy path。
- 外部模型可以使用确定性 fake；身份、数据库、Worker、浏览器和协议边界不应被整体 mock。
- 每个缺陷至少补一条能在修复前失败的回归测试。
- 测试产物只能证明生成它的 commit 和配置，不能用旧 Evidence 覆盖新失败。

## 2. 测试层级

| 层级 | 证明内容 | 典型位置 | 允许的替身 |
| --- | --- | --- | --- |
| 纯规则 | 状态机、事件校验、脱敏、解析、diff | 各模块 `tests/` | clock / ID；不替换被测规则 |
| 模块 | application module 与 adapter 的协作 | BFF/Core/观测后端测试 | HTTP/model/sandbox adapter |
| 契约 | OpenAPI、SSE、错误、事件版本、身份传播 | BFF 与 Core contract tests | 确定性 provider |
| 持久化集成 | 迁移、约束、事务、lease/fencing、审计链 | BFF integration/e2e | 真实 PostgreSQL |
| 跨服务 E2E | 浏览器到 BFF/Worker/Core/数据库/观测的闭环 | `e2e/` | 仅 LLM 和外部沙箱可用受控 double |
| 故障与恢复 | kill、重启、网络/DB 中断、迟到写、结果不确定 | `e2e/`、Core sandbox tests | 故障注入器 |
| 发布门禁 | lint、test、build、bundle、供应链、Compose | `scripts/release_gate.sh` | 无 |

## 3. 快速本地验证

从对应目录执行；首次运行先同步冻结依赖。

### BFF / Worker（Python >= 3.11）

```bash
cd map-business-backend
uv sync --frozen
uv run ruff check .
uv run pytest
```

涉及数据库事务、迁移或 Worker 并发时，必须使用测试要求的真实 PostgreSQL，不能以 SQLite
或内存 repository 作为最终结论。

### Core（Python >= 3.13）

```bash
cd map_core
uv sync --frozen
uv run ruff check .
uv run pytest
```

可按风险先运行定向测试，例如 Agent 引擎、OpenSandbox、身份传播或 typed event，再运行
全量 suite。测试选择以文件名和 pytest node id 为准，不新增仅在个人环境生效的 marker。

### 观测后端（Python >= 3.9）

```bash
cd map-observability/map-observability-backend
uv sync --frozen
uv run ruff check .
uv run pytest
```

### 业务前端

```bash
cd map-business-frontend
npm ci
npm test
npm run build
```

### 观测前端

```bash
cd map-observability/map-observability-frontend
npm ci
npm test
npm run build
```

`map-tree-core` 由两个前端以 workspace 文件依赖消费；修改它时至少运行两个前端的 test 和
build，并执行 bundle 检查。

## 4. 契约验证

跨服务变更必须覆盖：

- 请求与响应 schema，包括未知字段、缺失字段和版本不支持；
- Public 与 internal OpenAPI 分层；
- 用户身份、服务身份、Workspace 所有权和错误 envelope；
- SSE 分帧、UTF-8 边界、EOF、`error` 与唯一终态；
- `Idempotency-Key` 同键同体重放、同键异体冲突；
- Event major/minor 兼容、严格序号、64KiB/ArtifactRef 分界；
- trace 与运行标识跨 BFF、Worker、Core、Model、Tool 的传播。

Canonical Run 的冻结验收项见 [`SPEC/contracts/run.md`](../SPEC/contracts/run.md#8-契约测试ac-contract-010304050708)。
Conversation 事件集见 [`SPEC/contracts/conversation.md`](../SPEC/contracts/conversation.md)。

## 5. 状态、并发与故障矩阵

凡修改生命周期或 Worker 行为，至少验证：

| 风险 | 必测场景 |
| --- | --- |
| 重复请求 | 同一幂等键并发提交；响应丢失后重放 |
| lease 丢失 | heartbeat 失败；旧 owner 完成；新 owner takeover |
| 迟到结果 | stop 与 done、timeout 与 result、reconcile 与 result 竞争 |
| 进程退出 | handler 前、外部调用中、外部成功后但提交前 SIGTERM/kill |
| 数据库中断 | claim、heartbeat、业务提交、审计 append 时中断 |
| 外部结果未知 | provider 已执行但响应丢失；对账不可用；禁止盲重试 |
| 流式中断 | 非法帧、非法 UTF-8、EOF、客户端断开、上游断开 |
| 权限失败 | 缺身份、伪造可信 Header、跨 Workspace、无服务身份 |

终态不可逆、fencing 和未知结果不是可通过“最终一致”跳过的细节。

## 6. Compose 跨服务 E2E

完整说明见 [`e2e/README.md`](../e2e/README.md)。根目录执行：

```bash
# PR 稳定子集：真实浏览器与身份边界
MAP_E2E_FINAL=1 python3 e2e/run_e2e.py --suite pr

# 完整故障矩阵
MAP_E2E_FINAL=1 python3 e2e/run_e2e.py --suite full
```

E2E 每次使用随机 Compose project 和新 volumes。报告写入 `e2e/tmp/report-*.json`；这些是
临时验证产物，不应成为代码或文档的运行时输入。

## 7. Release gate

```bash
# 日常开发
bash scripts/release_gate.sh

# 发布候选；baseline 必须是已批准的固定 commit
RELEASE_GATE_FINAL=1 \
GATE_BASELINE_SHA=<approved-base-commit> \
  bash scripts/release_gate.sh
```

Gate 覆盖 Compose 校验、Python frozen sync/Ruff/pytest、前端 clean install/test/build、bundle、
依赖审计、安全扫描和变更范围检查。日志与机器可读摘要位于 `tmp/gate-logs/`。

Final gate 的 `baseline..HEAD` 与 worktree 检查含义不同，两者都必须通过。不得选择性删除
失败日志，或用非 final 运行冒充发布结论。

## 8. Test double 规则

- Fake LLM 必须固定响应、事件顺序和故障注入点，不读取真实密钥。
- OpenSandbox double 只用于协议和崩溃场景；生产验收仍需真实服务 Evidence。
- 时间、UUID 和随机退避可注入，但默认实现仍需测试。
- 内存 repository 适合 application module 快测，不证明 SQL 约束、锁、隔离或迁移正确。
- MSW 只替代 BFF 网络边界，不复制前端自身的业务 reducer 逻辑。
- 禁止 mock 掉身份校验、状态机、fencing 或敏感数据过滤后再宣称安全行为通过。

## 9. 测试数据与 Evidence

- 测试账户、Workspace 和资源使用随机或 suite 专属身份，避免跨用例共享可变状态。
- 机密使用测试专用占位值，不把真实 token 写入 fixture、snapshot 或日志。
- Snapshot 只用于稳定协议/呈现；高频变化的大对象应使用语义断言。
- Evidence 必须包含 implementation SHA、命令、配置摘要、退出码和原始结果引用。
- `tmp/acceptance/`、`tmp/gate-logs/` 和 `e2e/tmp/` 是生成目录，不作为源码扫描和知识图谱输入。

## 10. 按变更选择验证

| 变更 | 最低验证 |
| --- | --- |
| 纯文档 | 链接、路径、命令与当前实现人工/脚本校验 |
| 纯函数或局部 UI | 定向测试 + 所属模块 lint/test |
| Public/internal API 或 SSE | 两侧契约测试 + BFF/Core 全量相关 suite |
| 数据模型/迁移 | migration upgrade + repository/integration + rollback/forward 说明 |
| Worker/Effect/Run | 真实 PG 并发测试 + 故障矩阵 + full E2E |
| 身份、凭据、沙箱 | 安全回归 + fail-closed 场景 + release gate |
| 前端共享 package | 两个前端 test/build + bundle |
| Compose/部署 | `docker compose config` + smoke/E2E + OPERATIONS 更新 |

风险高于表中默认时向上追加测试，不以表格作为减少验证的理由。
