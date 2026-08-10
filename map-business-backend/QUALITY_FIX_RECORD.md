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

- 待最终运行时回填：`bash scripts/release_gate.sh` 的 `[gate] steps=`
  输出、`tmp/gate-logs/` 全量 artifact、`e2e/run_e2e.py --suite full`
  连续两次干净 volume 的报告 JSON 与 git SHA。
