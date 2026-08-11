# FIX-P2-QUALITY-01 / R2-P2-02 质量收口记录

> 对应整改：FIX-P2-QUALITY-01（静态检查、告警、供应链收口）与第二轮
> R2-P2-01 / R2-P2-02（测试退出码、panel 矩阵、统一工具链、bundle gate）。
> 规则：本文件所有"全绿"声明必须附 exit code 与日志 artifact 路径；
> 无法复现的"全绿"视为无效。

## 1. 静态检查与测试（含 exit code）

- `git diff --check`：0 错误（清理了 35 个文件的尾随空格，含初始迁移文件）。
- Ruff：三个 Python 服务统一配置（`[tool.ruff]`，line-length=100，
  `B008/RUF001/RUF003/UP042` 按项目惯例忽略）。
  - **R2-P2-02 修正**：ruff 统一放入 `[dependency-groups].dev`（原 BFF 在
    optional extra、观测后端完全缺失），干净 checkout `uv sync --frozen` 后
    `uv run ruff check app tests`（map_core 为 `map_core tests`）直接成功，
    三个服务均 exit 0（artifact：`tmp/gate-logs/*-lint.log`）。
- 观测前端 Vitest：
  - **R2-P2-01 修正**：此前 `npm test` 实际 exit 1（1 个 unhandled
    TypeError：`ToolCallDetailDrawer` 在 partial payload 下直接解引用
    `main_flow_logs_page.items`）。修复 = `normalizeToolTracePayload`
    schema 归一化 + 渲染处 optional fallback；失败复现测试为
    `ToolCallDetailDrawer.test.tsx` 的 "survives a partial payload (regression)"。
  - 同轮发现并修复测试底座缺陷：vitest `globals: false` 下未注册 RTL
    `cleanup()`，DOM 跨用例累积造成假阳性（`src/test/setup.ts`）。
  - 修复后：`npm test` exit 0，`Tests 44 passed (44)`；**R3-P2-01 起
    “Errors 0” 由机制保证**：`src/test/setup.ts` 对未预期 console.error /
    unhandled rejection 直接 fail 用例；jsdom `getComputedStyle(elt, pseudoElt)`
    不再转发 pseudoElt（Not implemented 计数 0）；@emotion/cache@11.14.0 的
    `:first-child` SSR 告警按完整前缀精确隔离（计数 0）
    （artifact：`tmp/gate-logs/obs-fe-test.log`）。
- 业务前端 Vitest：`npm test` exit 0，`Tests 30 passed (30)`；R3-P2-01：
  MSW 已为初始化 GET（`/api/admin/summary`、`/api/admin/full-config`）补
  handler，全局无 unmatched；expected network error 在用例内显式 spy；
  setup.ts 同样 fail-on-unexpected-error（artifact：`tmp/gate-logs/biz-fe-test.log`）。
- panel 矩阵（R2-P2-01 验收）：
  - `RequestDetailDrawer.test.tsx`：容器级 loading / empty(missing trace) /
    request error(invalid request_id) / happy(四 panel)；
  - `ToolCallDetailDrawer.test.tsx`：loading+partial payload 回归 / 完整
    happy fixture(真实 API contract) / request error(invalid tool_call_id) /
    trace missing；
  - `panels.test.tsx`：四个 panel 各自 happy / empty / error 或
    malformed-row 鲁棒性。真实 request error 在容器层（panel 为纯展示）。

## 2. 告警与资源泄漏

- asyncpg `Connection._cancel was never awaited`：根因是全局 engine 连接跨事件循环
  复用；BFF 的 `build_engine` 改为 `NullPool`（每请求新建连接）后消除。
- 全量测试以 `pytest.PytestUnraisableExceptionWarning` 为 error 运行时全绿
  （无未等待协程/未处理 promise/资源泄漏）。
- **前端第三方告警（R2-P2-02，精确隔离而非吞错）**：两个前端的
  `src/test/setup.ts` 仅按完整前缀过滤以下两类已知第三方告警，其余
  console.error 一律放行：
  1. `[antd: Tooltip] overlayClassName deprecated` —— 来源
     antd@5.29.3 经 @agentscope-ai/design@1.0.32 的内部调用；
  2. `Warning: forwardRef render functions accept exactly two parameters`
     —— react-dom@18.3.1 对上述第三方组件内部 forwardRef 用法的告警。
  上游升级至新 API 后应删除过滤；过滤清单与版本固定在 setup.ts 注释中。

## 3. 供应链

### 3.1 依赖审计命令（release gate 内置，可复现）

```bash
# Python 三个服务：frozen 运行时依赖在 python:3.13-slim 容器内 pip-audit
bash scripts/dependency_audit.sh        # 全部 exit 0 才算通过
# 业务前端
cd map-business-frontend && npm audit --omit=dev --audit-level=high
# 观测前端
cd map-observability/map-observability-frontend && npm audit --omit=dev --audit-level=high
```

三步均已纳入 `scripts/release_gate.sh`（步骤 `py-dep-audit` /
`biz-fe-audit` / `obs-fe-audit`）。high/critical 漏洞必须满足：有 owner、
有缓解或例外审批（登记于仓库根 `SECURITY_EXCEPTIONS.md`，带到期日），
否则 gate 失败。

### 3.2 本轮修复的依赖（R2-P2-03，全部已关闭，无遗留例外）

| 服务 | 修复方式 | 结果 |
| --- | --- | --- |
| map_core | `uv lock --upgrade` 全量升级（含 fastapi→0.141.1、starlette→1.6.0 等） | pip-audit exit 0（30 条 advisory 清零） |
| map-business-backend | `uv lock --upgrade-package fastapi --upgrade-package starlette`（0.136.3→0.141.1 / 1.1.0→1.6.0） | pip-audit exit 0（3 条 starlette 清零） |
| map-observability-backend | 上轮已升级 | pip-audit exit 0 |
| 观测前端 | `npm audit fix`（axios/form-data/js-cookie/uuid 传递依赖） | npm audit exit 0 |
| 两前端 DOMPurify | `package.json` `overrides: {"dompurify": "3.4.13"}`（超全部 advisory 影响范围），`npm install` 重锁 | npm audit exit 0（16 条清零，**不走例外**） |

fastapi 0.141 破坏性变更配套修复：`add_event_handler` 被移除，map_core
的 postgre/mongodb/milvus 三个数据库模块改为 `verify_startup()` 方法，
由 `main.py` lifespan 显式驱动启动校验与关闭；失败复现回归测试
`map_core/tests/test_app_lifespan.py`（修复前 AttributeError）。

### 3.3 DOMPurify 收口（原"例外"已被 override 修复替代）

| 项 | 值 |
| --- | --- |
| 原影响版本 | `dompurify <= 3.4.12`（npm advisory 系列） |
| 影响面 | 通过 `@agentscope-ai/design` → `map-tree-core` 传递引入，两个前端均有 |
| 最终处置 | **不走例外**：两前端 `package.json` 增加 `overrides: {"dompurify": "3.4.13"}` 并重新锁定，16 条 advisory 全部清零 |
| 可达性分析 | 项目源码无 `dompurify`/`safeHtml` 直接调用；唯一消费点为 design 库内部 `sanitize(html, {ADD_ATTR:['target']})`，未触及受影响 API 模式（函数式 ADD_TAGS predicate 等），升级无行为变化 |
| 验证 | 两前端 `npm audit --omit=dev --audit-level=high` exit 0；`npm test` / `npm run build` 回归通过（artifact：`tmp/gate-logs/*-fe-*.log`） |
| 例外登记 | `SECURITY_EXCEPTIONS.md` §Resolved；当前 pip-audit / npm audit 例外表均为空 |

## 4. Bundle gate（R2-P2-02，已自动化）

脚本：`scripts/check_bundle_size.py`，预算：`scripts/bundle-budget.json`。
入口 chunk（`dist/index.html` 引用者）与其余 lazy/shared chunk 分别设
raw/gzip 上限；超限 exit 1。**调高任何预算需带编号批准**（在本文件追加
`BUNDLE-EX-<序号>` 条目并写明申请人与理由），否则 gate 失败。

当前基线（实测值 +10% 余量，防回归）：

| 项目 | chunk | raw 上限 | gzip 上限 | 实测 raw | 实测 gzip |
| --- | --- | --- | --- | --- | --- |
| map-business-frontend | entry `index-*.js` | 760000 | 245000 | 684332 | 219392 |
| map-business-frontend | 其余 chunk | 740000 | 225000 | ≤660900 | ≤198856 |
| map-observability-frontend | entry `index-*.js` | 1280000 | 405000 | 1152716 | 361246 |
| map-observability-frontend | 其余 chunk | 1630000 | 485000 | ≤1472721 | ≤433607 |

> 修正此前记录：观测前端"主入口约 250 kB"仅是二级 chunk；真实入口为
> 1.15 MB（gzip 361 kB），`makeChartComp` chunk 1.47 MB。压缩这两个大
> chunk 需另行立项，不通过放宽预算解决。
> gate 最近一次运行：exit 0（artifact：`tmp/gate-logs/bundle-gate.log`）。

## 5. Release gate（已落地：本地脚本 + CI 同一命令集）

- 脚本：`scripts/release_gate.sh`（每步打印 exit code，日志写入
  `tmp/gate-logs/<step>.log`，任一失败总 exit 1）。
- **R3-P2-01：步数不再手写** —— 脚本结尾自动输出
  `[gate] steps=<N> failed=<M>`；本文件与任何文档只引用 gate 输出，
  不硬编码步数/份数。
- R3-P2-01 新增两个统一标准命令步骤：`diff-check`（`git diff --check`）
  与 `compose-config`（`docker compose config --quiet`）。
- CI：`.github/workflows/ci.yml` push/PR 运行同一 gate 脚本 + E2E PR 子集
  （browser happy path + identity boundary）；完整故障矩阵为 nightly/
  手动触发的 `--suite full`（E2E 定位：PR gate = browser+identity，
  nightly gate = full）。
- 所有依赖解析从 `uv sync --frozen` / `npm ci` 开始，不依赖本地残留工具。

```bash
bash scripts/release_gate.sh
# 步骤构成（具体步数以 gate 输出的 [gate] steps= 为准）：
# diff-check / compose-config /
# {bff,core,obs} × {deps,lint,test} / {biz,obs}-fe × {deps,test,build} /
# bundle-gate / py-dep-audit / {biz,obs}-fe-audit
```

> gate 最近一次本地实跑结果见本文档 §10 的 artifact 记录（步数/通过数
> 以 gate 自身输出为准，不在此复述手写数字）。
> BFF 集成测试需 dev PostgreSQL（127.0.0.1:15432，三角色由
> `db/init/01-roles.sh` 创建）。

## 6. 存量 lint 债（整改基线之外，已全部清零）

三个 Python 服务的 `app tests`（或 `map_core tests`）`ruff check` 均为 0 错误；
无未处理例外。

## 7. 第二轮勘误（针对第一轮记录的不实表述）

- 第一轮称"两个前端 `npm test`、`npm run build` 全绿"不成立：观测前端
  真实 exit 1（见 §1 R2-P2-01），已修复并附失败复现测试。
- 第一轮 bundle 段只记录 250 kB 主入口，与真实 1.15 MB 入口不等价；
  已按 §4 全量实测重建基线。
- 第一轮"CI 待接入"已落地为 §5 的脚本 + workflow。

## 8. R2-P2-04 契约与部署可验证性收口

全部交付物均在 `map-business-backend`，随 release gate 的 `bff-*` 步骤
回归（`uv run pytest` exit 0，artifact：`tmp/gate-logs/bff-test.log`）。

### 8.1 OpenAPI 全 schema 快照契约

- 快照：`tests/contracts/openapi_snapshot.json`（再生成器
  `tests/contracts/gen_snapshot.py`，app.main 惰性化后无 import 副作用）。
- 测试：`tests/contracts/test_openapi_contract.py` 递归 deep-diff 产生
  点路径差异；差异必须命中
  `tests/contracts/openapi_change_allowlist.json`（带 reason/approved_by/
  approved_at/expires，过期即失败），当前 entries 为空 = 精确匹配。

### 8.2 v1 错误矩阵（10 操作 × 6 状态码）

`tests/integration/test_v1_error_matrix.py`：401（无凭证，全部 7 个浏览器
操作 + 3 个管理操作）/403（member 访问审计路由）/404（5 个资源查询）/
409（幂等冲突）/422（含 malformed JSON 与字段类型错配）/500（依赖注入
注入 RuntimeError，`ASGITransport(raise_app_exceptions=False)` 配合
starlette ≥1.6 的 500 后 re-raise 行为）；所有响应断言统一
`error_envelope`（含 `INTERNAL_ERROR` 新增 handler，`app/api/errors.py`）。

### 8.3 ops 脚本双形式可执行

- `scripts/verify_audit_chain.py` / `verify_feedback_backfill.py` /
  `quarantine_audit_chain.py` 增加 sys.path bootstrap + `scripts/__init__.py`，
  `python scripts/x.py` 与 `python -m scripts.x` 均 exit 0。
- 失败复现测试：`tests/integration/test_ops_scripts.py` 在子进程实跑
  文档命令并断言 exit 0（修复前 ImportError）。

### 8.4 readiness 种子双条件校验

`app/api/readiness.py`：seed 校验改为 `WHERE id = :id AND code = :code`；
错误 workspace UUID 必须 503，回归测试
`tests/integration/test_deploy_defaults.py::test_readiness_fails_with_wrong_workspace_id`。

### 8.5 干净环境默认启动可证明

`tests/integration/test_default_boot_clean_env.py`：无 MAP_* 环境变量下
import `app.main` 零文件系统副作用（PEP 562 惰性单例，修复前 OSError
'/app' read-only）；`load_settings()` 默认值逐条匹配部署契约。

## 9. Compose 跨服务 E2E（R2-P1-05；R3-P1-02 扩展为真实浏览器 + 安全/故障矩阵）

- 入口：`python3 e2e/run_e2e.py --suite {pr,full}`（fake LLM + 独立容器名
  + PG/Mongo/Jaeger 交叉校验 + 报告 + 失败时自动导出 compose logs）。
- **R3-P1-02：frontend 纳入 Compose 拓扑**（`VITE_MAP_CONVERSATIONS_ENABLED=true`），
  `e2e/browser_e2e.py` 用 Playwright/Chromium 走真实用户路径
  create → stream → reload → stop → feedback → withdraw，并在 wire 层断言
  X-Request-ID / X-Session-ID / Idempotency-Key；runner 用浏览器捕获的
  conversation/session ID 反查 PostgreSQL 与 MongoDB。
- 安全矩阵（secure profile，`scenario_identity_boundary`）：forged admin
  （无/错 proxy secret）恒 401、member 403、跨用户/跨 workspace 404 无泄漏、
  service identity audience/scope 固有声明校验（wrong-aud 401 / no-scope 403 /
  伪造 X-Service-* 401）。
- 故障矩阵（full suite）：duplicate replay / stop mid-stream / BFF restart /
  PostgreSQL interruption(pause-unpause) / worker kill + lease takeover /
  core restart / feedback-withdraw audit / worker reconcile。
- ID 一致性：request/session/trace ID 在 PG（conversations/messages）、
  Mongo（request_records/llm_call_records，含 workspace_id）与 Jaeger
  （trace_id）三处交叉断言。
- CI 定位：PR gate = `--suite pr`（happy path + browser + identity boundary）；
  nightly/手动 = `--suite full`（完整故障矩阵）。
- 最近全绿 artifact 见本文档 §10（报告 JSON `e2e/tmp/report-*.json`，
  `"result": "PASS"`）。

## 10. R3 第三轮整改收口记录

### 10.1 R3-P2-01 质量记录与 gate 证据一致性

- 观测前端：`getComputedStyle` shim 不再把 pseudoElt 传给 jsdom 未实现
  分支；`@emotion/cache@11.14.0` 的 SSR `:first-child` 告警按完整前缀
  精确隔离（来源/版本记录于 `src/test/setup.ts` 注释）；复跑 artifact 中
  上述错误/警告计数为 0（复跑日志 `tmp/gate-logs/obs-fe-test.log`）。
- 业务前端：MSW 为初始化 GET（summary/full-config）补 handler；expected
  network error 在 `adminSave.test.tsx` 内显式 spy；复跑日志无 stderr 段。
- 两前端 setup.ts：未预期 console.error、MSW unhandled、unhandled
  rejection 直接 fail 用例（不再只检查退出码）。
- gate 步数由 `release_gate.sh` 自动统计（`[gate] steps=<N> failed=<M>`），
  本文件不再出现手写步数；gate 新增 `diff-check`/`compose-config` 两个
  统一标准命令步骤；E2E 定位：PR = browser happy path + identity
  boundary，nightly = full。
- ops subprocess 矩阵：`tests/integration/test_ops_scripts.py` 参数化覆盖
  三个脚本（verify_audit_chain / verify_feedback_backfill /
  quarantine_audit_chain）× 两种文档化调用形式（direct/module）的成功
  路径，另覆盖不可达 DSN 与缺 migration DSN 的失败退出码。

### 10.2 R3-P2-03 PG role 初始化脚本安全引用

- `db/init/01-roles.sh`：角色名 `^[a-z_][a-z0-9_]{0,62}$` fail-closed 校验；
  标识符仅经 `format('%I')`、密码经 psql `:variables`（在 dollar-quote 外
  用 `set_config` 暂存，块内 `current_setting` 读取）+ `format('%L')`。
- fresh-volume 测试：`tests/integration/test_roles_init_script.py`，含
  空格/单引号/美元符号/混合特殊字符四组真实密码，每组独立 postgres:16
  容器验证初始化成功 + TCP 密码登录 + 非 superuser + schema 归属；
  非法角色名不泄漏 secret。
- 既有 volume 手工迁移与回滚：`SPEC/contracts/audit.md` §6。

### 10.3 R3-P2-04 供应链例外机器可读 + 审计工具固定

- 机器可读登记表：`security/dependency_exceptions.json`（当前为空）；
  `scripts/load_dependency_exceptions.py` 校验字段齐全、严格 ISO 日期、
  approved_at 不在未来、`expires <= today` 直接 exit 2（过期例外自动失败）。
- `scripts/dependency_audit.sh` 仅从登记表生成 `--ignore-vuln`；固定
  base image digest（python:3.13-slim @sha256:9662...25e6）与
  pip-audit==2.10.1；artifact 记录工具版本、镜像 digest、各 uv.lock sha256。
- 自动化测试：`tests/test_dependency_exceptions.py`（含过期失败复现）。

### 10.4 最终 gate / E2E 证据（在不可变 SHA 上运行）

- 最终 SHA：`e0a0320bb63370bca8b7ccef4578e6168eb1fff0`（分支
  `qoder/dev-modelscope`；R3 工作包提交序列：`db5378a`(identity) →
  `3c1ed88`(conversation) → `2bc168e`(worker/effect, R3-P0-01) →
  `d8a6df3`(mutation, R3-P1-01) → `1a60a22`(audit+筛选契约, R3-P1-03) →
  `c017302`(E2E+CI, R3-P1-02) → `08e5840`(OpenAPI, R3-P2-02) →
  `1f25ac4`(PG roles, R3-P2-03) → `f0d2af1`(质量 gate, R3-P2-01) →
  `3e953e7`(供应链, R3-P2-04) → `96a12e1`→amend→lint 修复 `e0a0320`
  (R3-P1-04)）。`git diff --check 9021065..HEAD` 为空；提交后
  `git status --short` 为空。
- 最终 gate（在 `e0a0320` 上）：`bash scripts/release_gate.sh` 输出
  `[gate] steps=21 failed=0`、`RELEASE GATE PASSED`，全量 artifact 在
  `tmp/gate-logs/`（diff-check / compose-config / 三后端 deps+lint+test /
  两前端 deps+test+build+audit / bundle-gate / py-dep-audit，全部
  exit=0）。首轮 gate 在 `93446de` 曾暴露 bff-lint 两处（test 导入排序
  I001、行长 E501），已在 `e0a0320` 修复并复跑全绿。
- E2E（`e2e/run_e2e.py --suite full`，每次全新命名 volume，结束后
  `down -v`）：
  1. run1 PASS — `e2e/tmp/report-map-e2e-407e09fa.json`；
  2. run2 PASS — `e2e/tmp/report-map-e2e-c99b1f63.json`；
  3. run3 PASS（在不可变 SHA `e0a0320` 上）—
     `e2e/tmp/report-map-e2e-d71fdbb3.json`。
  三次均覆盖 happy path + browser（刷新恢复/stop/feedback/撤回）+
  secure identity boundary + 故障矩阵（BFF restart、PostgreSQL
  interruption、worker kill + lease takeover，`takeover_attempt: 2` 证明
  lease 被接管恰好一次）+ PG/Mongo/Jaeger 交叉校验。报告 JSON 位于被
  gitignore 的 `e2e/tmp/`（运行产物不入交付集合）。

### 10.5 提交数口径修正（R4-P2-03）

- 第四轮复审指出交付说明的“12 个提交”与 `git rev-list --count
  9021065..HEAD` 的 13 不一致。正确口径：**12 个实现/收尾提交
  （`db5378a`…`e0a0320`）+ 1 个证据文档提交（`1057cfa`，docs-only，
  不改变产品 tree），合计 13**。
- 自 R4 起不再依赖人工叙述：`scripts/release_gate.sh` 生成
  `tmp/gate-logs/gate-summary.json`（自带 git SHA / tree hash /
  branch / dirty / 每步 exit code、日志 sha256、UTC 时间），
  `e2e/run_e2e.py` 报告自带 `source_control` 字段；质量记录只引用
  artifact 内字段，提交数一律以 `git rev-list` 实测为准。

## 11. R4 第四轮整改收口记录

### 11.1 R4-P0-01 effect dispatch 恰好一次（key 强制 + 围栏 + 事实恢复）

- `EffectProvider` 协议改为 `send(key) -> bool` + `query(key) ->
  bool | None`：key 成为强制位置参数，任何无 key 的 effect 发送在
  类型层面不可能存在；`query` 返回服务端事实（delivered / 未送达 /
  uncertain）。
- effect 行新增 dispatch 围栏列（owner / attempt / lease），迁移
  `e7f8a9b0c1d2_effect_dispatch_fence.py`：只有持有当前 lease 的
  attempt 才能把行推进到 dispatched，被接管 worker 的迟到写入被行级
  围栏拒绝。
- dispatching 恢复不再假设“没提交=没送达”：按 idempotency key 查
  provider 事实 —— delivered 则直接对账落库、未送达则重发、
  uncertain 则保持 pending 并告警，不做盲重发。
- 测试：崩溃窗口 W1/W2/W2b/W3/W4 各 20 轮（W2b = 新增的“已调
  provider、未写结果”崩溃点），PG 持久 fake provider 记录真实事实，
  每轮断言事实数=1；另有并发围栏测试（`test_job_lease_fencing.py`、
  `test_effect_protocol_windows.py`）。后端全套 pytest 303 个通过。

### 11.2 R4-P1-01 E2E 杀真实 lease owner（barrier 崩溃窗口）

- E2E `worker_kill_lease_takeover` 不再杀空闲容器：通过
  `MAP_WORKER_ID` + 环境变量 barrier（`MAP_E2E_RECONCILE_BARRIER_S`
  / `MAP_E2E_EFFECT_BARRIER_S`）让受害者 worker 在精确崩溃点暂停
  （`barrier_after_claim_before_business_write` /
  `barrier_after_intent_before_dispatch`），确认其为 lease owner 后
  `docker kill` 真实容器，再由接管 worker 恢复。
- 报告 artifact `faults.worker_kill_detail`（r4b 运行实测值）：
  reconcile 侧 `attempt_sequence=[1,2]`、`business_write_count=1`、
  `killed_worker_commits=0`、`reconciled=1`；effect 侧
  `provider_action_count=1`、`killed_worker_commits=0`、
  `idempotency_key` 记录在案 —— 业务写入与外部副作用各恰好一次。

### 11.3 R4-P2-01 browser E2E 事件卫生 fail-closed

- `e2e/browser_e2e.py` 对以下任一情况直接 FAIL：未捕获 pageerror、
  非隔离名单的 console error/warning、任何 >=400 响应、不在白名单
  的 requestfailed。白名单是精确匹配（scenario + method + 路由
  regex + failure reason），不是通配放行；`net::ERR_ABORTED` console
  副作用仅在同场景存在白名单 abort 时归因放行。
- 隔离名单（`CONSOLE_QUARANTINE`）每条记录 package / version /
  owner / review_until（当前 3 条均来自 @agentscope-ai/design 1.0.32，
  review_until=2026-11-30），且报告里每条隔离命中都携带完整元数据。
- `--self-test` 无栈失败复现：注入 console.error / 500 / 非白名单
  abort 各必须使评估 FAIL；隔离 warning 与白名单 abort 必须放行且
  隔离记录元数据完整。该 self-test 已作为 release gate 第 1 步
  （`browser-e2e-self-test`）常设执行。
- r4b E2E 实测：`page_errors` / `unexpected_console` /
  `unexpected_failed_requests` / `failed_responses` 均为 `[]`，
  `quarantined_console` 46 条全部携带完整隔离元数据。

### 11.4 R4-P2-02 供应链审计参数安全

- `load_dependency_exceptions.py` 对每条 advisory 做显式格式校验
  （CVE / GHSA / PYSEC 正则 fullmatch；空白、控制字符、shell 元字
  符、多余 token 一律拒绝），过期例外 fail closed（exit 2）。
- `dependency_audit.sh` 不再拼接命令字符串：allowlist 保存为 bash
  数组，以位置参数（`"$@"`）进入容器内固定脚本体；恶意值在结构上
  不可能逃逸成 shell 语法。漏洞库服务固定为 PyPI 官方 advisory 库
  （OSV 端点在本网络环境主机+容器双层验证不可达，脚本注释留证），
  服务不可达或真实 finding 仍然 fail gate；对瞬时超时仅重试，不吞
  失败（非零退出原样传播）。
- `test_dependency_exceptions.py` 32 个测试：合法 ID 6 例、恶意值
  15 例全部被拒、argv 探针镜像容器契约证明 1 条/2 条例外分别得到
  1 对/2 对 `--ignore-vuln` 参数、`exit 3` 探针证明非零退出不被掩盖。

### 11.5 R4-P2-03 artifact 自证（git SHA / tree / dirty / UTC）

- gate 启动即采集 source_control（SHA / tree hash / branch /
  baseline / dirty 文件清单，docs 与产品代码分类），每步记录
  command、exit_code、日志 sha256、UTC 起止时间，结尾组装
  `gate-summary.json`；`RELEASE_GATE_FINAL=1` 时产品代码 dirty 直接
  拒绝（docs-only 容忍并记录）。
- E2E 报告同样携带 `source_control` + `started_utc` /
  `finished_utc` / `final_mode`（`MAP_E2E_FINAL=1` 同规则）。

### 11.6 最终验证证据（同一工作树上运行）

- Release gate（final5）：`tmp/gate-logs/gate-summary.json` ——
  result=PASS，steps=22，failed=0，2026-08-10T14:34:35Z →
  15:02:56Z；source_control git_sha=1057cfa43e55、
  tree=4da78ab875aa、dirty=True（本轮未提交整改本身，final 模式
  关闭）。首步 `browser-e2e-self-test` exit 0。
- Full E2E（r4b，全新命名 volume）：
  `e2e/tmp/report-map-e2e-ba291521.json` —— result=PASS，12 个场景
  全 PASS（含 worker_kill_lease_takeover、pg_interruption_recovery、
  browser 三场景），`duration_s=500.7`，2026-08-10T14:25:46Z →
  14:34:07Z；worker kill 与 browser 卫生实测值见 §11.2 / §11.3。
- 注：E2E r4b 之后仅对 `e2e/browser_e2e.py` 追加了隔离记录元数据
  字段与 self-test 断言（§11.3），随后 gate final5 已在包含该改动
  的工作树上整体复跑全绿，两份 artifact 指向同一 tree。

## 12. R5 第五轮整改收口记录

### 12.1 R5-P0-01 effect dispatch fencing token CAS（提交 `e13b7d0`）

- 第五轮在真实 PostgreSQL 证明：`adopt_dispatch` / `ack_effect` /
  `mark_uncertain` 的 SQL 条件未携带 owner/attempt/token，旧 owner 可
  覆盖合法接管，两个 `run_effect_once` 均失败且行停在 `uncertain`。
- 修复：effect 行新增 `dispatch_token`（迁移 `f8a9b0c1d2e3`，单
  head、可逆、旧行 NULL 经 `IS NOT DISTINCT FROM` 兼容）；
  `begin_dispatch` 铸造 uuid4 围栏 token；`adopt_dispatch` 为单条原子
  CAS（observed token/owner/attempt 匹配 + 数据库时间 lease 过期，接
  管者铸造新 token，败者 rowcount=0 必须重新解析）；ack/mark_uncertain
  只结算本 generation；`confirm_provider_fact` 为唯一单调对账路径。
- 反例固化：live-lease 拒绝接管、协议级 barrier 20 轮（provider 动作
  =1、ledger=delivered、A 迟到写 rowcount=0）、并发 recovery 单一发
  送权、query=None 围栏，全部真实 PostgreSQL 通过。

### 12.2 R5-P1-01 async predicate + gate warning policy（`e17bd5e`、`fe34bd9`）

- disconnect 清理等待曾把 `async def` predicate 不带 await 调用：
  coroutine 恒真，30 秒等待首轮即退出，检查被整体跳过且 RuntimeWarning
  被吞。`_wait_until` 现在强制同步 predicate（coroutine function /
  返回 awaitable 一律 TypeError，coroutine 关闭不外泄），并带
  `diagnose` 诊断；新增失败复现测试。
- gate 的 bff-test 升级为 `-W error::RuntimeWarning
  -W error::pytest.PytestUnraisableExceptionWarning`：该假绿形态从此
  在 gate 层直接失败。

### 12.3 R5-P2-01 quarantine review_until 到期 fail-closed（`e419a97`）

- 启动即严格解析每条 `review_until`（ISO `YYYY-MM-DD`，缺失/畸形先于
  浏览器启动即 fail closed）；UTC date 比较且 self-test 注入固定
  today；边界按文档定义（当天到期仍有效）；过期匹配进入新的
  `expired_quarantine` 数组并令 run exit 1；artifact 记录
  `expired`/`days_remaining` 治理字段；`--self-test` 新增过期/边界/
  未来三类用例共 9 例全过；`run_e2e.py` 强制 `expired_quarantine == []`。

### 12.4 R5-P2-02 统一 NUL-safe source-control 证据链（`fe34bd9`）

- 新建 `scripts/source_control.py`：以 bytes/NUL 解析
  `git -c core.quotepath=false status --porcelain=v1 -z`（首行、空
  格、中文、rename 双路径、untracked 全部逐字节；禁止整体 strip 与固
  定列切片），docs/product 分类集中一处；dirty 非 final 运行额外记录
  `diff_head_sha256` + untracked 逐文件 sha256 manifest（工作树内容
  证据）；CLI `--require-clean-product` 在产品 dirty 时退出 2。
- gate 与 E2E 均委托该实现：gate 落盘 `tmp/gate-logs/source-control.json`
  并逐字并入 gate-summary；E2E report 的 `source_control` 与其同源。
- parser 单测在真实临时 git 仓库覆盖报告要求的 7 类路径形态 + 分类
  + 退出契约（`tests/test_source_control_snapshot.py`，11 例）；
  R4 供应链 argv 安全与前端隔离元数据同批入库（`51cdd30`）。

### 12.5 最终验证证据（不可变 HEAD `39621a1` 上执行）

- 工作包提交：`e13b7d0`（P0 围栏）、`e17bd5e`（P1 predicate）、
  `e419a97`（P2-01 到期）、`fe34bd9`（P2-02 证据链）、`51cdd30`
  （R4 供应链遗留）、`39621a1`（docs：复审报告 + 本记录），合计 6；
  加上本证据提交为 7（`git rev-list --count 1057cfa..HEAD` 实测为准）。
- Release gate（`RELEASE_GATE_FINAL=1`）：`tmp/gate-logs/gate-summary.json`
  —— result=PASS，steps=22，failed=0，final_mode=true，
  git_sha=`39621a12fc86`、tree=`f44951a2cb94`、branch=
  `qoder/dev-modelscope`、dirty=false，2026-08-11T01:58:07Z →
  02:11:16Z；每步 command/exit/log sha256 齐全；bff-test 在严格
  warning policy 下全绿。
- Full E2E（`MAP_E2E_FINAL=1`，全新命名 volume）：
  `e2e/tmp/report-map-e2e-a488f51f.json` —— result=PASS，12 个场景
  全 PASS，final_mode=true，source_control 与 gate 同一
  SHA/tree（`39621a12fc86`/`f44951a2cb94`）、dirty=false，
  duration_s=377.4，2026-08-11T02:13:36Z → 02:19:53Z。
- 报告 §6.1 回归复验：`worker_kill_detail.effect` ——
  provider_action_count=1、takeover_attempt=2、killed_worker_commits=0、
  两侧 killed container ID 与 barrier crash point 均在；场景断言硬编
  码接管后 `effect_ledger.status == 'delivered'`（uncertain 即失败），
  P0 修复后 kill 场景确认 delivered/动作 1，而非“事实 1、ledger
  uncertain”。browser 卫生：`page_errors=[]`、`unexpected_console=[]`、
  `expired_quarantine=[]`、`unexpected_failed_requests=[]`、
  `failed_responses=[]`；隔离记录携带 days_remaining=111 治理字段。

## 13. R6 第六轮整改收口记录

### 13.1 R6-P2-01 rename 跨 docs/product 边界分类绕过（提交 `78c0ba5`）

- 第六轮真实重放：`git mv app.py TODO/app.py.md`（已跟踪产品文件
  staged rename 进文档目录）时，snapshot 只把新路径放入
  `dirty_files`/`dirty_product`，`orig_path` 不参与分类，
  `docs_only_dirty=true`、`--require-clean-product` 错误返回 0——
  产品文件的删除/移出可绕过 clean-product final gate。
- 修复（仅 `scripts/source_control.py` 与其测试，不动任何运行时代
  码）：新增 `affected_paths_for()`——rename 是“旧路径删除 + 新路径
  增加”，destination 与 origin 双路径都参与 docs/product 分类；copy
  的 origin 未被删除，只按目标路径分类，但 origin 保留在 entry 供审
  计。`snapshot()` 输出显式去重集合 `affected_paths`，`dirty_files`
  镜像该集合、`dirty_product` 过滤该集合，gate 与 E2E 继续只消费这
  一个集合，不可能分类不一致。
- 验收测试（`tests/test_source_control_snapshot.py`，现 15 例）：
  逐字固化 R6 重放（`dirty_product == ['app.py']`、CLI 退出 2）；
  四象限 rename——product→product / product→docs / docs→product
  均 `dirty_product` 非空且精确断言受影响集合，仅 docs→docs 保持
  `docs_only_dirty=true`；copy 语义（只按目标分类、origin 留审计）；
  既有 11 例（首行/中文/空格/staged+unstaged/删除/untracked 等）全
  部继续通过。

### 13.2 R6 最终验证证据（验证 HEAD `78c0ba5`，产品树 clean）

- 修复提交：`78c0ba5 fix(evidence): R6-P2-01 rename contributes BOTH
  paths to classification`（2 文件，+124/-13，仅
  `scripts/source_control.py` 与 `tests/test_source_control_snapshot.py`）。
- Release gate（`RELEASE_GATE_FINAL=1`）：`tmp/gate-logs/gate-summary.json`
  （sha256 `aa307418f468e68be5fa5d2019f0f605288263d009580ef59b823c2dd4f7db0f`）
  —— result=PASS，steps=22，failed=0，final_mode=true，
  git_sha=`78c0ba5a1fb8`、tree=`309885153dcd`、`product_dirty=[]`、
  `docs_only_dirty=true`（唯一未跟踪文件为第六轮复审报告文档，
  final 模式按设计容忍并记录），bff-test 在严格 warning policy 下
  全绿，2026-08-11T05:13:06Z → 05:29:33Z 全部 22 步 exit=0。
- Final PR E2E（`MAP_E2E_FINAL=1 python3 e2e/run_e2e.py --suite pr`）：
  `e2e/tmp/report-map-e2e-8efcb96a.json`（sha256
  `43838c6b2248736b4e9d73fd0f0ff1af0bdb1e42a114a392bd154e50c5ae1213`）
  —— result=PASS，final_mode=true，source_control 与 gate 同一
  SHA/tree（`78c0ba5a1fb8`/`309885153dcd`）、`dirty_product=[]`，
  4 个场景（model_center_redirect / happy_path / browser /
  identity_boundary）全 PASS，duration_s=185.8；browser 卫生：
  `page_errors=0`、`unexpected_console=[]`、`expired_quarantine=[]`、
  `unexpected_failed_requests=[]`、`failed_responses=0`。
- 业务回归证据：本次修复不含任何 effect、worker、browser 或运行时
  产品代码变化；在同一产品树 `49e6295` 上连续两轮 fresh-volume
  full E2E 双绿：
  `report-map-e2e-e9f59d91.json`（sha256
  `74fdba7ed15d27a4a2cf89f2ce380256d88658c808d4de473e44d9b93dbdd7a9`）
  与 `report-map-e2e-d87a796f.json`（sha256
  `de747372e6dd2710e4c8fbc194145d726c57ef7f516395ceef3f95b4952e24e3`）。
- 第七轮最小验收门槛自检：四象限 rename 自动化全过；临时仓库
  product→docs 重放 final CLI exit=2（独立重放确认）；source
  snapshot 全套、BFF ruff/全量 pytest（严格 warning）、browser
  self-test 全过；修复为独立提交；final gate 22/22 全绿；final PR
  E2E 一轮 PASS；证据回填本节，不再以第六轮 `49e6295` 作为修复后
  的最终验证 HEAD。

## 14. R7 第七轮整改收口记录

### 14.1 第七轮两个失败复现与修复

- **R7-P2-01 工作树侧 rename（`XY=" R"`）绕过**：第六轮修复只覆盖
  索引列（`xy[0] == "R"`），而 porcelain rename 合法地出现在 XY 任一
  列——`mv app.py TODO/app.py.md && git add -N TODO/app.py.md` 在真
  实仓库稳定产生 `XY=" R"`，此时 origin 被分类层丢弃，
  `docs_only_dirty=true`、`--require-clean-product` 错误 exit 0。第六
  轮“R6-P2-01 关闭”的结论仅对暂存态 `git mv`（`R `）成立，工作树
  侧状态空间未覆盖；本节予以补齐，不再沿用该结论。
- **R7-P2-02 gate diff-check 不检查已提交范围**：无参
  `git diff --check` 只查未提交差异，工作树干净时对已进入 HEAD 的
  trailing whitespace / blank-at-EOF 恒绿；`GATE_BASELINE_SHA` 只被
  记录、从未驱动校验（旧 artifact `baseline_sha=null`）。本文件自身
  的 EOF 多余空行即为真实反例（`git diff --check 49e6295..HEAD`
  exit 2），已在提交 `314b313` 删除，`git diff --check 7d2b813..HEAD`
  恢复 exit 0。
- 修复提交：
  - `5c9088c fix(evidence): R7-P2-01 worktree-side rename (XY=" R")
    drives classification`——分类改为 `"R" in entry["xy"]`，两列都
    把 destination 与 origin 计入 `affected_paths`；copy 语义不变
    （origin 未删除，只按目标分类，origin 留审计）。
  - `314b313 fix(gate): R7-P2-02 two-step whitespace check over
    worktree AND committed range`——新增
    `scripts/gate_diff_check.sh`（worktree / validate / committed 三
    模式；缺失/不可解析/非祖先 baseline 一律 exit 3 fail-closed，
    `merge-base --is-ancestor` 固化范围方向）；final 模式在任何步骤
    前强制要求有效 baseline；gate 拆为 `diff-check` 与
    `diff-check-committed` 两个可审计步骤；非 final 无 baseline 时
    committed-range 步骤在 summary 中如实记录 skipped 及原因
    （`steps_skipped` 字段），不伪装通过；`GATE_LOG_DIR` 可覆盖以免
    测试触碰证据 artifact。
- 自动化（全部先红后绿，真实临时仓库）：
  - `tests/test_source_control_snapshot.py`（26 例）：R7 逐字重放
    （`XY=" R"`、`dirty_product=['app.py']`、CLI exit 2）；四象限
    rename × 两种列位置共 8 例（前三象限两形态均 product dirty，
    仅 docs→docs 可 docs-only）；工作树列 copy；CLI/模块在同一仓库
    分类一致。
  - `tests/test_gate_diff_check.py`（9 例）：clean 工作树 + 坏提交
    范围时 worktree check 为 0 而 committed-range 非零；修复提交后
    恢复 0；缺失/不可解析/非祖先 baseline 三条 fail-closed 路径；
    真实 gate 脚本在 `RELEASE_GATE_FINAL=1` 下无 baseline/坏
    baseline 均非零退出。

### 14.2 R7 最终验证证据（验证 HEAD `97e771b`，工作树完全干净）

- 提交序列：`5c9088c`（R7-P2-01）、`314b313`（R7-P2-02 + EOF 修
  复）、`97e771b`（docs：第七轮复审报告）；`git diff --check
  7d2b81342ad96a612ac86091673506e203075c5d..HEAD` exit 0。
- Release gate（`RELEASE_GATE_FINAL=1
  GATE_BASELINE_SHA=7d2b81342ad96a612ac86091673506e203075c5d bash
  scripts/release_gate.sh`）：`tmp/gate-logs/gate-summary.json`
  （sha256 `e2650ac11c93d10a096460f95bbe8e8ca272ff51f76b9dfa288feec6e4ea90cb`）
  —— result=PASS，steps_total=23，steps_failed=0，steps_skipped=0，
  final_mode=true；git_sha=`97e771b2fd36`、tree=`f7caf778da26`、
  dirty=false、product_dirty=[]；baseline_sha=`7d2b81342ad9…` 已入
  artifact；`diff-check`（worktree）与 `diff-check-committed`
  （baseline..HEAD）均 exit 0；2026-08-11T07:13:43Z → 07:26:00Z。
- Final PR E2E（`MAP_E2E_FINAL=1 python3 e2e/run_e2e.py --suite pr`）：
  `e2e/tmp/report-map-e2e-8df4a02a.json`（sha256
  `fa11cf88f42a7a3a8c9d1fa3a4176cbaac8a0a0a86807923088f7b3cebd1845d`）
  —— result=PASS，suite=pr，final_mode=true，与 gate 同一
  SHA/tree、dirty=false，4 场景（model_center_redirect /
  happy_path / browser / identity_boundary）全 PASS，
  duration_s=242.3；browser 卫生：page_errors=0、
  unexpected_console=[]、expired_quarantine=[]、
  unexpected_failed_requests=[]、failed_responses=0。
- 业务回归证据：本轮仅改共享 source-control 分类、release gate、
  对应测试与文档，未触碰 effect/worker/browser/身份/数据库等运行
  时路径（报告 §6 复用条件），继续引用已核验的第六轮两份
  fresh-volume full E2E：`report-map-e2e-e9f59d91.json`（sha256
  `74fdba7ed15d27a4a2cf89f2ce380256d88658c808d4de473e44d9b93dbdd7a9`，
  12/12 PASS）与 `report-map-e2e-d87a796f.json`（sha256
  `de747372e6dd2710e4c8fbc194145d726c57ef7f516395ceef3f95b4952e24e3`，
  12/12 PASS）。
- 第八轮最小验收门槛自检：双形态四象限真实仓库用例全过；两种
  product→docs CLI 重放均 exit 2 且集合精确；R7-P2-02 失败复现自动
  化先红后绿；`git diff --check 7d2b813..HEAD` exit 0；snapshot 全
  套、BFF ruff/全量 pytest（严格 warning）、browser self-test 全
  绿；两个 P2 分工作包独立提交；final gate 23/23 全绿且 artifact
  同时证明 worktree 与 committed-range；final PR E2E 一轮 PASS；证
  据回填本节，交付后不再修改产品/测试/gate/质量记录。
