# 不可抵赖审计契约（FIX-P2-CONTRACT-E2E-01）

> 对应整改：FIX-P1-AUDIT-01。

## 1. 表

`config_audit_events`（append-only，hash chain）：
`id, workspace_id, resource_type, resource_id, action, actor_user_id, actor_subject, actor_roles, request_id, source_ip, user_agent, before_version, after_version, json_patch, before_hash, after_hash, status(applied|failed|rejected), failure_code, recovered, prev_entry_hash, entry_hash, created_at`

`config_mutations`（可变的崩溃恢复编排表，不属于审计链）：
`id, resource, expected_hash, target_hash, status(pending|applied|failed), error, created_at, finished_at`

## 2. 写流程（所有 `/api/admin/*` 写必须经 ConfigMutationService）

1. 短事务：插入 `pending` mutation（expected hash）并提交（崩溃恢复点）；
2. `update_with_hash`：expected hash 校验 + 临时文件 + fsync + 原子 rename；
3. 短事务：追加 applied/failed/rejected 审计事件（脱敏 JSON Patch + hash chain）并终结 mutation。

- 审计失败绝不吞掉并返回成功：请求返回 500 `AUDIT_WRITE_FAILED`，mutation 保持 pending，由启动 reconciler 补 `recovered=true` 事件。
- 并发写（同 expected hash）只有一个成功，另一个 409 `CONCURRENT_MODIFICATION`。

## 3. Hash chain

`entry_hash = sha256(prev_entry_hash + canonical(record))`；canonical 字段集由
`app.services.config_mutation.audit_record_payload` 定义（写入与校验共用）。
并发追加在事务内 `SELECT ... FOR UPDATE` 锁定链尾，杜绝分叉。

- 校验：`GET /api/v1/admin/audit-events/verify` 或 `scripts/verify_audit_chain.py`，返回第一条断点。

## 4. JSON Patch

RFC 6902 风格；list 项按业务 key（id/code/name/...）对齐——排序变化不产生整表 delete/add。

## 5. Redaction

审计前统一脱敏：token/authorization/cookie/password/secret 的值不落盘；`headers/env_refs` 只记录键名与变更标记。

## 6. 权限

- 应用角色（`map`）对 `config_audit_events` 只有 SELECT/INSERT，无 UPDATE/DELETE（迁移角色 `map_migrator` 除外）；mutation 表可由 service 更新。
- 只读审计员（`audit_viewer`）可读 audit-events，不可写配置。

## 7. 故障语义

- AdminState 解析失败：保留坏文件、返回 500 `BAD_STATE_FILE`，绝不回退默认后覆盖。
- 文件写失败：原文件完整，`failed` 审计（`STORE_WRITE_FAILED`）。

## 8. 兼容

旧 `audit_logs` 表与 `/api/admin/audit-logs` 保持只读兼容；旧数据不迁移，新审计走新表。
