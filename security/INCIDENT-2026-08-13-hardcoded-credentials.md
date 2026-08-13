# INCIDENT-2026-08-13 — 仓库固定凭据与宿主执行路径处置记录

> 对应黄金任务书 **TASK P0-SEC-01** 动作 1–2（安全止血）。
> 基线：`e019059c2c8499454ecddc9eb63655aeadb0bd90`（处置前）。

## 1. 处置结论（SUMMARY）

- **已禁用**：进程内 `python_exec_tool`、`bash_tool`（宿主执行路径）从生产
  tool registry 物理移除；任何显式调用稳定返回 `CAPABILITY_DISABLED`
  （fail-closed），不再存在宿主 `exec`/`bwrap` 代码路径。
- **已移除**：仓库内全部固定凭据（gpustack 系列 token、rerank schema
  运行时默认值、Mongo/Postgres URI 内嵌口令、Milvus 口令、行业问答 key、
  wenshu 注释中的历史 token/cookie/密码）。生产代码一律改为环境变量注入，
  未配置时下游 fail-closed。
- **待运营执行**：下表列出的旧 key 需要在凭据管理端撤销/轮换（仓库侧
  只能移除引用，无法代替运营撤销）。

## 2. 旧 key 撤销清单（REVOCATION TRACKER）

> 状态：`PENDING-REVOCATION` = 已从代码移除、等待凭据管理端撤销。

| # | 凭据 | 出现位置（处置前） | 处置 | 撤销状态 | 负责人 |
| - | ---- | ------------------ | ---- | -------- | ------ |
| 1 | `gpustack_de6adf356d53ae9f_c803a9395068e4879708f267852629cb`（LLM api_key） | `rerank_model_schema.py`（schema 运行时默认值）、`kb_tools/scripts/*`、`zhiwen_agent/scripts/*`、`remote_config_compare/sacne_config_fetch.py`（示例） | 代码移除，改 `MAP_LLM_AUTH_TOKEN` env | PENDING-REVOCATION | security owner |
| 2 | `gpustack_c60ea7b6efa4784c_22039bb6f38836e6a955588a5df04306`（rerank token） | `rerank_model_schema.py`（默认值）、`kb_tools/{remote_api,knowledge_base_agent_tool}.py`（`__main__` demo，已删除）、dev scripts | 代码移除，改 `MAP_RERANK_AUTH_TOKEN` env | PENDING-REVOCATION | security owner |
| 3 | `gpustack_67740332be54f86f_6711f81dbbcecdf9f85be842418e44d9`（embed token） | `kb_tools/{remote_api,knowledge_base_agent_tool}.py`（`__main__` demo，已删除）、dev scripts | 代码移除，改 `MAP_EMBED_AUTH_TOKEN` env | PENDING-REVOCATION | security owner |
| 4 | `api_key = "zhiwen"` + 内网端点 `http://10.50.49.35:20010/industry_chat` | `industry_chat_agent.py`（类属性，生产路径） | 改 `MAP_INDUSTRY_CHAT_API_KEY` / `MAP_INDUSTRY_CHAT_URL` env，未配置 fail-closed | PENDING-REVOCATION | security owner |
| 5 | Milvus `root` / `"password"` 固定口令 | `wenshu_agent.py`（生产路径）、`_wenshu_split_question/{split_question,usage_examples}.py`（测试/示例函数）、wenshu 注释死代码 | 改 `MAP_MILVUS_USER` / `MAP_MILVUS_PASSWORD` env；注释死代码块删除 | PENDING-REVOCATION | security owner |
| 6 | Mongo URI 内嵌口令 `mongodb://root:48f#7fQuk6!@...`（prod/test 环境） | `config/prod.py`、`config/test.py`（MONGODB_CONFIG.uri 硬编码） | 改 `MONGODB_URI` env 注入，空值 fail-closed（无法连接）；compose 已映射 env | PENDING-REVOCATION | security owner |
| 7 | 本地默认 URI 口令 `postgresql://map:map@` / `mongodb://map:map@` | `config/common.py`（DEFAULT_* 常量） | 默认值删除，改纯 env 注入（`POSTGRES_DSN` / `MONGODB_URI`） | PENDING-REVOCATION | security owner |
| 8 | wenshu 历史 token：Basic `d2ViQXBwOndlYkFwcA==`、`SESSION_ESSENDATA=...` cookie、`jarvis_dev`/`Zwzj0h0z` 密码（注释态） | `wenshu_agent.py:161-166`（注释） | 注释行删除 | PENDING-REVOCATION | security owner |

## 3. 修复后的注入环境变量清单

| 变量 | 用途 | 缺失行为 |
| ---- | ---- | -------- |
| `MAP_RERANK_BASE_URL` / `MAP_RERANK_MODEL_NAME` / `MAP_RERANK_AUTH_TOKEN` | rerank 默认配置 | 空值 → 下游拒绝不完整 config（fail-closed） |
| `MAP_LLM_AUTH_TOKEN` / `MAP_EMBED_AUTH_TOKEN` | dev 脚本中 llm/embed 调用 | 空值 → 请求被远端拒绝 |
| `MAP_INDUSTRY_CHAT_URL` / `MAP_INDUSTRY_CHAT_API_KEY` | 行业问答接口 | 空 URL → httpx 拒绝（fail-closed） |
| `MAP_MILVUS_USER` / `MAP_MILVUS_PASSWORD` | Milvus 连接 | 空口令 → 远端认证拒绝 |
| `MONGODB_URI` / `MONGODB_DATABASE` | Mongo 连接（common/prod/test 配置） | 空 URI → 无法连接（fail-closed）；compose 注入 |
| `POSTGRES_DSN` | PG 连接 | 空 DSN → 无法连接（fail-closed）；compose 注入 |

## 4. Known risk register（后续任务处置）

| # | 风险 | 处置任务 | 期限 |
| - | ---- | -------- | ---- |
| R1 | MCP stdio 工具（`dynamic_tools._call_stdio_mcp_tool`）在 core 进程内以 `asyncio.create_subprocess_exec(command, *args)` 启动子进程，command/args 来自请求体 `dispatch_config.mcp_servers`（当前无 schema 校验）；core 路由尚无 service identity 防护，依赖网络隔离。OpenSandbox 上线前如 core 暴露面扩大必须先加认证 | P0-SEC-01 动作 3（stdio MCP 移入沙箱）+ P0-CFG-AUTH-01（service identity） | OpenSandbox 上线前完成动作 3 |
| R2 | dev 配置（config/dev.py 等）中的内网端点（10.x.x.x）保留为环境配置，非凭据；如需进一步收敛由 P1-CONFIG-01 统一治理 | P1-CONFIG-01 | 视 config 任务排期 |

## 5. 防回归

- 自动扫描门禁：`map_core/tests/test_hardcoded_credential_scan.py` 在 CI
  （pytest 全量）中扫描 `map_core/map_core/**/*.py`（165+ 文件），覆盖
  gpustack/AWS/GitHub/Slack/OpenAI key、URI 内嵌口令（mongodb://user:pass@）、
  Basic token、session cookie、`password="<literal>"` 等模式；另有
  `test_source_root_resolves_to_real_tree` 防止门禁退化为 no-op。
- 宿主执行禁用回归：`map_core/tests/test_disabled_capabilities.py` 验证
  registry 移除、实现文件物理删除、执行稳定返回 `CAPABILITY_DISABLED`。
