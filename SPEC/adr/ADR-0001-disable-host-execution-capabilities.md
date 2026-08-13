# ADR-0001: 宿主执行能力止血 — python_exec_tool / bash_tool 禁用与凭据移除

- 状态：Accepted
- 日期：2026-08-13
- 任务：`TASK P0-SEC-01`（动作 1–2，可独立紧急发布）
- 基线：`e019059c2c8499454ecddc9eb63655aeadb0bd90`

## Context

黄金任务书边界第 4 条：生产代码/命令/文件执行唯一调用 OpenSandbox Server；
map_core 不挂 Docker socket/kubeconfig，不存在宿主/本地 fallback。当前实现
中，`python_exec_tool`（进程内 `exec`）与 `bash_tool`（宿主 bwrap 子进程）
均为宿主执行路径；同时仓库内存在多处固定凭据（gpustack 系列 token、
rerank schema 运行时默认值、行业问答/Milvus 硬编码口令），在 OpenSandbox
上线前必须先止血。

## Decision

1. **物理删除** `python_exec_tool.py`、`bash_tool.py`（宿主执行实现）。
2. 两个工具名保留为 **known-but-disabled capabilities**
   （`DISABLED_HOST_EXEC_CAPABILITIES`）：
   - 不在生产 registry 注册（LLM 不会看到、不会主动调用）；
   - 旧场景配置引用它们时校验仍通过（不 400）；
   - 任何执行尝试在 `ToolExecutor` 单点稳定返回
     `{"error": "CAPABILITY_DISABLED", "code": "capability_disabled", ...}`，
     对 legacy 与 AgentScope 双引擎生效，TOOL span 标记失败（fail-closed）。
3. 固定凭据一律移除，改为环境变量注入（`MAP_*` 前缀），未配置时下游
   fail-closed（空值被消费方拒绝）。删除 `kb_tools` 中两个含真实 token 的
   `__main__` demo 块；dev scripts 改为 env 读取。
4. 防回归：`tests/test_disabled_capabilities.py`（禁用行为）与
   `tests/test_hardcoded_credential_scan.py`（凭据模式扫描，进 pytest 全量，
   即进 release gate）。

## Consequences

- 配置了 `python_exec_tool`/`bash_tool` 的历史场景：请求合法，但执行时工具
  返回 `CAPABILITY_DISABLED`；后续由 OpenSandbox Workspace（AgentScope
  2.0.6 升级）重新提供同能力。
- 旧 key 的撤销需要凭据管理端操作，仓库侧登记
  `security/INCIDENT-2026-08-13-hardcoded-credentials.md` 撤销清单跟踪。
- 未配置 `MAP_RERANK_AUTH_TOKEN` 等环境变量的部署，rerank/行业问答/Milvus
  相关能力 fail-closed（空配置不发起网络调用），部署侧需补齐注入。
