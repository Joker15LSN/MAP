# FIX-P2-QUALITY-01 质量收口记录

> 对应整改：FIX-P2-QUALITY-01（静态检查、告警、供应链收口）。

## 1. 静态检查（均已通过）

- `git diff --check`：0 错误（清理了 35 个文件的尾随空格，含初始迁移文件）。
- Ruff：三个 Python 服务统一配置（`[tool.ruff]`，line-length=100，
  `B008/RUF001/RUF003/UP042` 按项目惯例忽略）；`ruff check app tests` 三服务全绿。
- 观测后端存量格式：`ruff format` 统一（12 个文件）。
- Vitest：业务前端重复 `css` 键已删；两个前端 `npm test`、`npm run build` 全绿。

## 2. 告警与资源泄漏

- asyncpg `Connection._cancel was never awaited`：根因是全局 engine 连接跨事件循环
  复用；BFF 的 `build_engine` 改为 `NullPool`（每请求新建连接）后消除。
- 全量测试以 `pytest.PytestUnraisableExceptionWarning` 为 error 运行时 147 项全绿
  （无未等待协程/未处理 promise/资源泄漏）。
- React forwardRef / Ant Design 弃用告警：来源为 `@agentscope-ai/design` 第三方
  组件（非本项目代码），无法在本仓库消除；影响面仅为控制台噪音，不改变行为。

## 3. 供应链

### 3.1 依赖审计命令（release gate）

```bash
# 业务前端
cd map-business-frontend && npm audit --omit=dev --audit-level=high
# 观测前端
cd map-observability/map-observability-frontend && npm audit --omit=dev --audit-level=high
# Python（三个服务；pip-audit 需安装）
uvx pip-audit -r <(uv export --frozen --no-dev)   # 或 uv export 后执行
```

high/critical 漏洞必须满足：有 owner、有缓解或例外审批，否则 CI 失败。

### 3.2 本轮修复的依赖

观测前端：`npm audit fix` 修复 axios/form-data/js-cookie/uuid 传递依赖（
3 high + 4 moderate 消除；34 项测试与 build 回归通过）。

### 3.3 DOMPurify 风险例外（上游无修复，不可忽略）

| 项 | 值 |
| --- | --- |
| 影响版本 | `dompurify <= 3.4.12`（npm advisory 系列，无上游修复） |
| 影响面 | 通过 `@agentscope-ai/design` → `map-tree-core` 传递引入，两个前端均有 |
| 实际使用方式 | 本项目未直接 import/调用 DOMPurify；仅 design 组件内部对富文本渲染兜底 |
| 缓解措施 | 前端不渲染用户可控 HTML 于 DOMPurify 路径；消息内容以纯文本渲染；管理端 JSON 以 JSON.stringify 展示 |
| owner | 前端负责人（BFF 工作包 owner） |
| 升级卡号 | 依赖升级卡：跟踪 `@agentscope-ai/design` 发布，升级后移除本例外 |
| 到期时间 | 2026-11-09（90 天）复查；届时若无修复则升级 design 或替换富文本渲染路径 |

## 4. Bundle 预算

- 业务前端主入口 `index-*.js` ≈ 684 kB（gzip ≈ 221 kB）——既有值；新会话 feature
  按 feature flag 打包，budget = 当前值 +10%，超预算需批准。
- 观测前端主入口 `index-*.js` ≈ 250 kB（gzip ≈ 76 kB），budget = +10%。
- 两个前端 `build.rollupOptions` 大 chunk 告警保留为预算信号；CI 中主 chunk 超过
  budget 时失败或要求批准（待 CI 落地，见 §5）。

## 5. Release gate（CI 待接入的命令清单）

```bash
git diff --check <基线>..HEAD
cd map-business-backend && uv run ruff check app tests && uv run pytest -W error::pytest.PytestUnraisableExceptionWarning
cd map_core && uv run ruff check map_core tests && uv run pytest -q
cd map-observability/map-observability-backend && uv run ruff check app tests && uv run pytest -q
cd map-business-frontend && npm test && npm run build && npm audit --omit=dev --audit-level=high
cd map-observability/map-observability-frontend && npm test && npm run build && npm audit --omit=dev --audit-level=high
docker compose config --quiet
```

## 6. 存量 lint 债（整改基线之外，已全部清零）

三个 Python 服务的 `app tests`（或 `map_core tests`）`ruff check` 均为 0 错误；
无未处理例外。
