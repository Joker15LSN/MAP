# INCIDENT-2026-08-13 — 仓库固定凭据与宿主执行路径处置记录

> 对应黄金任务书 **TASK P0-SEC-01** 动作 1–2（安全止血）。
> 基线：`e019059c2c8499454ecddc9eb63655aeadb0bd90`（处置前）。
>
> 本文件按代码审查意见书 R-01 修订：**不含任何可用凭据原文**，
> 只保留不可逆指纹（`sha256:<16hex>`）、secret-manager 引用、影响范围、
> owner 和撤销状态。指纹由 `scripts/security_scan.py` 同一算法生成。

## 1. 处置结论（SUMMARY）

- **已禁用**：进程内 `python_exec_tool`、`bash_tool`（宿主执行路径）从生产
  tool registry 物理移除；任何显式调用稳定返回 `CAPABILITY_DISABLED`
  （fail-closed），不再存在宿主 `exec`/`bwrap` 代码路径。
- **已移除**：仓库内全部固定凭据（gpustack 系列 token、rerank schema
  运行时默认值、Mongo/Postgres URI 内嵌口令、Milvus 口令、行业问答 key、
  wenshu 注释中的历史 token/cookie/密码）。生产代码一律改为环境变量注入，
  未配置时下游 fail-closed。
- **待运营执行**：下表列出的旧 key 需要在凭据管理端撤销/轮换（仓库侧
  只能移除引用，无法代替运营撤销）。在这些外部撤销被确认之前，
  `AC-SEC-01` 验收状态保持 `blocked`（owner 见下表），不得判 `pass`。

## 2. 旧 key 撤销清单（REVOCATION TRACKER）

> 状态语义：`PENDING-REVOCATION` = 已从代码/文档/镜像移除引用、
> 等待凭据管理端撤销并出具可核验凭证。验证方式：secret manager 吊销
> 工单号 + 撤销后指纹在扫描器报告中不再出现的复测记录。

| # | 凭据指纹（sha256 前 16 位） | 出现位置（处置前） | 处置 | 撤销状态 | 负责人 |
| - | --------------------------- | ------------------ | ---- | -------- | ------ |
| 1 | `f486b3dfef02c125`（LLM api_key） | `rerank_model_schema.py`（schema 运行时默认值）、`kb_tools/scripts/*`、`zhiwen_agent/scripts/*`、`remote_config_compare/sacne_config_fetch.py`（示例） | 代码移除，改 `MAP_LLM_AUTH_TOKEN` env | PENDING-REVOCATION | security owner |
| 2 | `46e6b1f93a9eef2b`（rerank token） | `rerank_model_schema.py`（默认值）、`kb_tools/{remote_api,knowledge_base_agent_tool}.py`（`__main__` demo，已删除）、dev scripts | 代码移除，改 `MAP_RERANK_AUTH_TOKEN` env | PENDING-REVOCATION | security owner |
| 3 | `99cba70ccb65936b`（embed token） | `kb_tools/{remote_api,knowledge_base_agent_tool}.py`（`__main__` demo，已删除）、dev scripts | 代码移除，改 `MAP_EMBED_AUTH_TOKEN` env | PENDING-REVOCATION | security owner |
| 4 | `b14b41f038c345bd`（行业问答 api_key）+ 内网端点（10.x 网段） | `industry_chat_agent.py`（类属性，生产路径） | 改 `MAP_INDUSTRY_CHAT_API_KEY` / `MAP_INDUSTRY_CHAT_URL` env，未配置 fail-closed | PENDING-REVOCATION | security owner |
| 5 | Milvus 固定 `root`/低熵口令（原值即通用单词，不单独指纹） | `wenshu_agent.py`（生产路径）、`_wenshu_split_question/{split_question,usage_examples}.py`（测试/示例函数）、wenshu 注释死代码 | 改 `MAP_MILVUS_USER` / `MAP_MILVUS_PASSWORD` env；注释死代码块删除；空口令 fail-closed | PENDING-REVOCATION | security owner |
| 6 | `ce9ae29c228aaf89`（prod/test Mongo URI 内嵌口令） | `config/prod.py`、`config/test.py`（MONGODB_CONFIG.uri 硬编码） | 改 `MONGODB_URI` env 注入，空值 fail-closed（无法连接）；compose 已映射 env | PENDING-REVOCATION | security owner |
| 7 | `f384c1854ca16e74` / `2ef8370c88d19f87`（本地默认 URI 口令） | `config/common.py`（DEFAULT_* 常量） | 默认值删除，改纯 env 注入（`POSTGRES_DSN` / `MONGODB_URI`） | PENDING-REVOCATION | security owner |
| 8 | `9f5a425c1d37582f`（wenshu 历史 Basic token）、`a687aa85988ec355`（jarvis 密码）、历史 session cookie | `wenshu_agent.py:161-166`（注释） | 注释行删除 | PENDING-REVOCATION | security owner |
| 9 | compose 口令默认值（`MAP_POSTGRES_{ADMIN,APP,MIGRATOR}_PASSWORD`、`MONGO_INITDB_ROOT_PASSWORD`、DSN 内嵌口令） | `docker-compose.yml`、`db/init/01-roles.sh` fallback | 全部改为 `:?required` fail-fast 注入（无仓库默认）；E2E runner 注入隔离口令 | 本地 dev 值保留于 `.env.example`（扫描器豁免登记，dev-only 模板，owner=platform-security）；生产无默认 | security owner |

## 3. 修复后的注入环境变量清单

| 变量 | 用途 | 缺失行为 |
| ---- | ---- | -------- |
| `MAP_RERANK_BASE_URL` / `MAP_RERANK_MODEL_NAME` / `MAP_RERANK_AUTH_TOKEN` | rerank 默认配置 | 空值 → 下游拒绝不完整 config（fail-closed） |
| `MAP_LLM_AUTH_TOKEN` / `MAP_EMBED_AUTH_TOKEN` | dev 脚本中 llm/embed 调用 | 空值 → 请求被远端拒绝 |
| `MAP_INDUSTRY_CHAT_URL` / `MAP_INDUSTRY_CHAT_API_KEY` | 行业问答接口 | 空 URL/空 key → 发请求前 fail-closed（`CAPABILITY_CONFIG_MISSING`） |
| `MAP_MILVUS_USER` / `MAP_MILVUS_PASSWORD` | Milvus 连接 | 空口令 → 发请求前 fail-closed |
| `MONGODB_URI` / `MONGODB_DATABASE` | Mongo 连接（common/prod/test 配置） | 空 URI → 无法连接（fail-closed）；compose 注入 |
| `POSTGRES_DSN` | PG 连接 | 空 DSN → 无法连接（fail-closed）；compose 注入 |

## 4. Known risk register（后续任务处置）

| # | 风险 | 处置任务 | 期限 |
| - | ---- | -------- | ---- |
| R1 | MCP stdio 工具原以宿主子进程运行；已按审查意见书 R-02 禁用（`_call_stdio_mcp_tool` 稳定返回 fail-closed，不再 `create_subprocess_exec`）。HTTP MCP 的 egress/SSRF/TLS/大小限制在 OpenSandbox 上线前随动作 3 完成 | P0-SEC-01 动作 3 + P0-CFG-AUTH-01（service identity） | OpenSandbox 上线前完成动作 3 |
| R2 | dev 配置（config/dev.py 等）中的内网端点（10.x.x.x）保留为环境配置，非凭据；如需进一步收敛由 P1-CONFIG-01 统一治理 | P1-CONFIG-01 | 视 config 任务排期 |
| R3 | 本轮新增 OpenSandbox 认证 HTTP 客户端（`map_core/map_core/service/opensandbox_client.py`）；真实 Server 的 contract/integration/fault/security 验收（AC-SEC-12）在 OpenSandbox Server 部署后执行，当前 `blocked` | P0-SEC-01 动作 4–7 | OpenSandbox Server 0.2.2 部署后 |

## 5. 防回归

- 自动扫描门禁：`scripts/security_scan.py`（统一入口，覆盖 tree/index/
  build-context/image 四个 scope，输出永远脱敏、命中即非零退出）。
  `map_core/tests/test_hardcoded_credential_scan.py` 在 pytest 全量中调用
  该入口并断言输出不含秘密片段（canary 反证）。release gate 的
  `security-scan` 步骤运行 `--scope tree,index,build-context --fail-on-hit`。
- 宿主执行禁用回归：`map_core/tests/test_disabled_capabilities.py` 验证
  registry 移除、实现文件物理删除、执行稳定返回 `CAPABILITY_DISABLED`；
  `map_core/tests/test_host_boundary.py` 静态断言生产代码无宿主
  subprocess/任意本地路径读写，并验证本地文件工具与 stdio MCP 全部
  fail-closed（审查意见书 R-02）。

## 6. 验收状态

- `AC-SEC-01`：当前树/暂存区/构建上下文零命中（见最终 HEAD 的
  evidence-manifest）；但因第 2 节外部撤销未出具可核验凭证，该 AC 在
  最终 HEAD 证据中状态为 `blocked`（blocker_owner=security owner），
  直到吊销工单齐备后改为 `pass`。
- `AC-SEC-02`：OpenSandbox 未上线前宿主执行/本地文件读写/stdio MCP
  全部禁用，由 `test_disabled_capabilities.py` + `test_host_boundary.py`
  在最终 HEAD 验证。
