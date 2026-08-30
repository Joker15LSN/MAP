# Step 10 收口报告与 FINAL gate runbook

> 状态：**非最终**。本报告记录 Step 0–10 的本地执行事实与待 CI/owner 项；
> 不构成 releasable 验收。FINAL acceptance 必须由受保护 CI（独立 acceptor）
> 生成并签名。

## 1. 本地最终验证

- 全量 release gate（非 FINAL）：`steps=29 skipped=1 failed=0`
  （HEAD `432e0a5f`；Step 9 后）。
- core pytest：383 passed（Step 9 后）；BFF 全量 exit 0。
- security scan：`--scope tree,index,build-context --fail-on-hit` exit 0。
- architecture gate：PASS；F401/F841 debt 0；C901>12：core 25、BFF 7、obs 5；
  cross-router private imports 3；direct SDK：openai 2（仅 ModelInvocation
  adapter）、agentscope 15、motor 1、pymongo 7。

## 2. 规模（LOC，不含 .venv/生成物）

| 组件 | Python/TS LOC |
| --- | --- |
| BFF app | 19,517 |
| Core map_core | 37,806 |
| Obs backend app | 5,458 |
| 业务前端 src | 8,365 |
| 观测前端 src | 6,553 |

## 3. Step 0–9 执行摘要

- Step 0：architecture gate + release gate 基线，F401/F841 清零。
- Step 1：dead code/未用 import 清理。
- Step 2–4：Canonical Run/Attempt/Turn/Run 前端切换。
- Step 5：Agent Execution 公开模块 + AgentScope 默认，legacy switch 保留。
- Step 6：ModelInvocation 单一 typed invoke，旧 LLMEngine 壳删除。
- Step 7：PG AdminState + Runtime Snapshot + Run pin + core fixed-id transport，
  file JSON/volume/reconciler 删除。
- Step 8：core typed execution events + service-identity NDJSON 流，
  Mongo telemetry 停写且抽象删除，retention/drop 留 owner。
- Step 9：core ingress 收口为 `routers/runtime_transport.py` 7 个公开名。

## 4. Interface 缩减 / 删除量（抽样）

- `LLMEngine` 12 个入口 → `ModelInvocation.invoke` 1 个；`llm_engine.py`
  587 行壳删除。
- file `AdminStateStore`/`ConfigMutationService`/reconciler/volume 删除；
  管理写统一 `RuntimeSnapshotService.apply_change`。
- `state_store.py`/Mongo handler/queue/webhook/decorator 删除；事件发射统一
  `ExecutionEventEmitter.emit`。
- core ingress 8 处重复 header/SSE/error helper → 7 个公开名的
  `runtime_transport` 模块。

## 5. 未闭环项（owner/CI 待办）

| 项 | owner | 证据/复现 |
| --- | --- | --- |
| AC-RUN 全矩阵 / E2E | CI | `TODO/代码精简与可读性改造执行计划.md` Step 2 状态块 |
| PR-G legacy chat 排空删除 | 产品/平台 | plan Step 4；HAR 30 天 |
| PR-H2 单引擎删除 | 平台 | plan Step 5 |
| Step 6 AC-CLEAN-LLM-03 durable/CI | CI | plan Step 6 |
| Step 7 AC-CONFIG-01 真实迁移 / 06-08 资产业务 | owner | plan Step 7 |
| Step 8 K9 Mongo export/digest/restore/drop 双签 | platform-security | `TODO/retention/mongo_state_store_retirement.md` |
| Evidence CI attestation | 受保护 CI | `tmp/acceptance/index.json` |
| P1-CLEAN-BUILD 前端 DTO 生成 | 前端/平台 | plan Step 7 |

## 6. FINAL gate runbook

```bash
# 受保护 CI（MAP_EVIDENCE_CI=1、EVIDENCE_SIGNING_KEY、repository/git_ref/run_id）
python3 scripts/generate_acceptance_evidence.py --profile TODO/acceptance-profile.yaml
bash scripts/release_gate.sh --final \
  --baseline-sha "$GATE_BASELINE_SHA"
# 独立 acceptor 校验 evidence-report.json releasable=true 后生成
# FINAL-ACCEPTANCE.md 并签名。
```

在 CI attest 与 FINAL gate releasable 之前，本仓库**不得**标记
P1-RELEASE-01 或 Step 10 completed。
