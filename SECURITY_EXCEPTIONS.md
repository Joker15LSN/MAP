# SECURITY_EXCEPTIONS.md — 供应链安全例外登记

R2-P2-03 交付物；R3-P2-04 起例外改由机器可读登记表强制执行。所有依赖
审计例外必须同时登记在本文件（人读记录）与
`security/dependency_exceptions.json`（机器可读，gate 唯一数据源），
且每条必须包含：advisory ID、可达性分析、缓解措施、owner（可追责个人）、
工单号、到期时间、批准人。

## Gate 约定

| Gate | 命令 | 通过条件 |
| --- | --- | --- |
| Python 依赖审计 | `bash scripts/dependency_audit.sh` | exit 0；`--ignore-vuln` 只能来自 `security/dependency_exceptions.json` 中字段齐全且**未过期**的条目（由 `scripts/load_dependency_exceptions.py` 逐条校验） |
| 前端依赖审计 | `npm audit --omit=dev --audit-level=high`（两前端） | exit 0；moderate 例外须在本文件 §npm audit 例外表登记 |

- CI（`.github/workflows/ci.yml` release-gate）对**新出现的 high/critical** 漏洞直接失败；
- **例外过期由脚本自动执行**：`load_dependency_exceptions.py` 用严格 ISO 日期
  解析 `approved_at`/`expires`，`expires <= today` 的条目直接 exit 2（视同新漏洞），
  字段缺失/日期非法/批准时间在未来同样 fail closed；自动化失败复现测试为
  `map-business-backend/tests/test_dependency_exceptions.py`；
- pip-audit 固定于 digest 镜像 `python@sha256:9662...25e6`（python:3.13-slim）
  + `pip-audit==2.10.1`（见 `scripts/dependency_audit.sh`）；升级必须以独立
  依赖更新提交进行；审计日志记录工具版本、镜像 digest 与各 lockfile sha256；
- 禁止以扩大 allowlist 的方式消化审计结果；新增例外必须走下面的模板、
  写入 JSON 登记表并经批准人签认。

## §Resolved（2026-08 审计结果已全部关闭，无例外）

### Python（pip-audit，容器化 `python:3.13-slim` 取证）

| 服务 | 升级前 findings | 处置 | 验证 |
| --- | --- | --- | --- |
| map_core | 30 条 / 11 包（click、lxml、orjson、pydantic-settings、pygments、python-dotenv、python-multipart、setuptools、starlette、ujson、urllib3） | `uv lock --upgrade-package fastapi --upgrade-package starlette`（fastapi 0.128.0→0.141.1、starlette 0.50.0→1.6.0，ujson 移出依赖树）+ `[tool.uv] constraint-dependencies` 9 条传递依赖安全下限 | pip-audit exit 0；map_core 全量 126 项回归通过（含新增 lifespan 回归 `tests/test_app_lifespan.py`，修复 fastapi 0.141 移除 `add_event_handler` 导致的启动崩溃） |
| map-business-backend | 3 条 Starlette（PYSEC-2026-248/249，starlette 1.1.0） | fastapi 0.136.3→0.141.1 / starlette 1.1.0→1.6.0 | pip-audit exit 0；BFF 全量 pytest exit 0 |
| map-observability-backend | 0 条 | 无需处置 | pip-audit exit 0 |

### 前端（npm audit）

| 包 | 升级前 findings | 处置 | 验证 |
| --- | --- | --- | --- |
| dompurify 2.5.9（经 @agentscope-ai/design@1.0.32 传递引入） | 16 条 advisory（GHSA-vhxf-7vqr-mrjg 等，最高影响范围 `<=3.4.12`） | 两前端 `package.json` 增加 `overrides: { "dompurify": "3.4.13" }` 直接升到修复版本，**不走例外** | 两前端 `npm audit --omit=dev --audit-level=high` exit 0（0 vulnerabilities）；业务前端 30 / 观测前端 44 测试全绿；build exit 0 |

**DOMPurify 可达性分析**（报告要求"验证项目实际没有进入受影响 API 模式"）：
- 项目源码（两前端 `src/` 与 `packages/`）**无任何** `dompurify` / `safeHtml` 直接调用；
- 唯一消费点是 @agentscope-ai/design 内部 `lib/libs/dom.js`：`DOMPurify.sanitize(html, { ADD_ATTR: ['target'] })`；
- 未使用受影响 advisory 依赖的配置模式（函数式 `ADD_TAGS` predicate、`FORBID_TAGS`/`FORBID_ATTR` 组合、`SANITIZE_NAMED_PROPS` 等），仅基础 `sanitize` + `ADD_ATTR`；
- 且已 override 到 3.4.13（超出全部已知 advisory 影响范围），风险关闭。

## §pip-audit 已批准例外

**当前为空。** 所有 findings 已通过升级关闭。

## §npm audit 已批准例外

**当前为空。** DOMPurify 已通过 override 修复，不走例外。

## 例外登记模板（新增时必须填全）

```markdown
| 字段 | 内容 |
| --- | --- |
| 例外编号 | SEC-EX-NNN |
| advisory ID | （如 PYSEC-2026-xxxx / GHSA-xxxx） |
| 受影响包@版本 | |
| 引入路径 | （直接依赖 or 经哪个上游包传递） |
| 可达性 | （受影响 API/模式在本项目是否可达，证据） |
| 缓解措施 | |
| 不可升级原因 | |
| owner | （可追责个人，非角色） |
| 工单号 | （可追踪编号，非描述性文字） |
| 批准人 | |
| 到期时间 | （到期未关闭 = gate 失败） |
```

登记后必须同步写入机器可读登记表 `security/dependency_exceptions.json`
（字段：exception_id/advisory/package/introduction/reachability/mitigation/
why_not_upgraded/owner/ticket/approver/approved_at/expires，日期严格
YYYY-MM-DD）。`scripts/dependency_audit.sh` 仅从该文件生成 `--ignore-vuln`，
手工维护的 IGNORE_ARGS 已废弃；npm audit 例外仍在本文件 §npm audit 表登记，
由前端审计命令人工对照（暂无机器化需求，当前为空）。
