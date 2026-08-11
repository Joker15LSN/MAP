# 不可抵赖审计契约（FIX-P2-CONTRACT-E2E-01）

> 对应整改：FIX-P1-AUDIT-01。

## 1. 表

`config_audit_events`（append-only，hash chain）：
`id, workspace_id, resource_type, resource_id, action, actor_user_id, actor_subject, actor_roles, request_id, source_ip, user_agent, before_version, after_version, json_patch, before_hash, after_hash, status(applied|failed|rejected), failure_code, recovered, prev_entry_hash(NOT NULL, genesis='', UNIQUE), entry_hash, ordinal(BIGINT UNIQUE ≥0), error_message, created_at`

`config_audit_chain_head`（单行串行追加点，R2-P1-03）：
`chain_id(PK, 固定 1), head_ordinal, head_entry_hash, updated_at`

`config_audit_events_quarantine`（坏链后缀隔离区，原样保存 + `original_id, quarantined_at`）

`config_mutations`（可变的崩溃恢复编排表，不属于审计链）：
`id, resource, expected_hash, target_hash, status(pending|applied|failed), error, workspace_id, action, actor_user_id, actor_subject, actor_roles, request_id, created_at, finished_at`

## 2. 写流程（所有 `/api/admin/*` 写必须经 ConfigMutationService）

R3-P1-01 起 mutation 分为 prepare 与 apply 两阶段：

1. **prepare**（纯计算）：expected hash 校验 + 计算 target state/hash 与脱敏 patch，不写文件；业务拒绝/并发冲突在此阶段直接审计 rejected，不产生 pending 行；
2. 短事务：插入 `pending` mutation 并提交 —— 行上持久化 `expected_hash + target_hash + 原请求上下文（workspace/actor/request/resource/action）`，这是**rename 之前**的崩溃恢复点；
3. **apply**：再次 expected hash CAS + 临时文件 + fsync + 原子 rename；
4. 短事务：追加 applied/failed/rejected 审计事件（脱敏 JSON Patch + hash chain）并终结 mutation。

- 审计失败绝不吞掉并返回成功：请求返回 500 `AUDIT_WRITE_FAILED`，mutation 保持 pending，由启动 reconciler 补 `recovered=true` 事件。
- 并发写（同 expected hash）只有一个成功，另一个 409 `CONCURRENT_MODIFICATION`。
- **崩溃恢复判定只允许精确匹配**：`current == expected_hash` → `failed/NO_WRITE`；`current == target_hash` → `applied`（`after_hash` 精确等于 `target_hash`）；其他任何 hash（含旧版无 target_hash 的行）→ `failed/UNKNOWN_STATE`，绝不猜测 applied。recovered 事件保留原 workspace、actor、request_id、resource/action（旧行缺上下文时回落 `system:reconciler`）。

## 3. Hash chain（R2-P1-03 单链不变量）

`entry_hash = sha256(prev_entry_hash + canonical(record))`；canonical 字段集由
`app.services.config_mutation.audit_record_payload` 定义，**全部持久化在表列上**
（含脱敏截断后的 `error_message`）——writer、reconciler、verifier 共用同一个 schema。

- **hash-relevant（canonical）字段**：`workspace_id, resource_type, resource_id, action, actor_user_id, actor_subject, actor_roles, request_id, status, failure_code, before_hash, after_hash, json_patch, recovered, error_message`；
- **非 hash 字段**（仅展示/取证，见 `NON_HASH_RELEVANT_COLUMNS`）：`id, source_ip, user_agent, before_version, after_version, created_at`；链位置字段 `ordinal, prev_entry_hash, entry_hash` 由 verifier 结构性校验，不混入 record。

**并发与防分叉**：所有追加在调用方事务内先 `SELECT ... FOR UPDATE` 锁定
`config_audit_chain_head` 单行，head 推进与 event 插入同事务提交；数据库另有
`UNIQUE(prev_entry_hash)`（每个前驱恰有一个 child、唯一 genesis）与 `UNIQUE(ordinal)`
双不变量兜底，共同 predecessor 的第二个分支必然被约束拒绝。

- 校验：`GET /api/v1/admin/audit-events/verify` 或 `scripts/verify_audit_chain.py`，按 `ordinal` 遍历并返回第一条断点。
- 坏链处置：只检测、不静默重算；`scripts/quarantine_audit_chain.py`（迁移角色 DSN）把断点及后缀原样移入 `config_audit_events_quarantine` 并把 head 重置到最后一条已验证事件，链从可信前缀继续增长。
- 查询：`GET /api/v1/admin/audit-events` 支持 `resource_type, resource_id, actor, status, request_id, action, created_from, created_to, limit, offset`（R3-P1-03）。时间筛选为**含边界**的带时区 ISO-8601；naive 时间戳与 `created_from > created_to` 返回标准 422 envelope（`INVALID_TIME_RANGE`）。所有查询 SQL 从第一个谓词起就带 workspace predicate，任何筛选组合不得绕过。

## 4. JSON Patch

RFC 6902 风格；list 项按业务 key（id/code/name/...）对齐——排序变化不产生整表 delete/add。

## 5. Redaction

审计前统一脱敏：token/authorization/cookie/password/secret 的值不落盘；`headers/env_refs` 只记录键名与变更标记。

## 6. 权限

三角色最小权限模型（R2-P1-04，由 `db/init/01-roles.sh` + 迁移 `9a2b3c4d5e6f` 在数据库层强制）：

- `map_admin`：Compose bootstrap/维护用 superuser，应用服务一律不使用。
- `map_migrator`：DDL 角色，仅 Alembic 迁移使用（`MAP_CONTROL_MIGRATION_DSN`）。
- `map`：应用角色，`NOSUPERUSER NOCREATEDB NOCREATEROLE`；普通表经 default privileges 获得全 DML，审计表权限精确为：
  - `config_audit_events`：SELECT + INSERT（无 UPDATE/DELETE/TRUNCATE，append-only）
  - `config_audit_events_quarantine`：SELECT（隔离修复由运维用迁移角色执行）
  - `config_audit_chain_head`：SELECT + INSERT + UPDATE（追加点，无 DELETE/TRUNCATE）
  - `config_mutations`：全 DML（可变的编排表）
  - `alembic_version`：SELECT（readiness 探针）
- 只读审计员（`audit_viewer`）可读 audit-events，不可写配置。
- 生产部署必须覆盖 compose 默认密码；`01-roles.sh` 对角色名做 `^[a-z_][a-z0-9_]{0,62}$` 校验（非法名 fail-closed），标识符仅经 `format('%I')`、密码仅经 psql 变量 + `format('%L')` 拼接，含空格/引号/`$` 的真实 secret 不会破坏 SQL；失败输出不含密码。
- 既有 volume 手工迁移（`01-roles.sh` 只在全新 volume 首启时运行）：
  1. 以 bootstrap superuser 连接后手工执行 `01-roles.sh` 内的 DO 块（或通过临时容器挂载该脚本并设置 `POSTGRES_USER`/`POSTGRES_DB` 环境变量后运行）；
  2. 再用 `map_migrator` 执行 `alembic upgrade head`。
- 回滚：角色/权限回滚为 `DROP OWNED BY <role>; DROP ROLE <role>;`（先备份数据）；迁移回滚用对应 `alembic downgrade`，不得直接 TRUNCATE 审计表。

## 7. 故障语义

- AdminState 解析失败：保留坏文件、返回 500 `BAD_STATE_FILE`，绝不回退默认后覆盖。
- 文件写失败：原文件完整，`failed` 审计（`STORE_WRITE_FAILED`）。
- 崩溃恢复（启动 reconciler）：按第 2 节的精确匹配规则终结 pending mutation 并补 `recovered=true` 事件；崩溃窗口测试必须在真实 `apply_mutation()` 内注入 crash point（禁止手工造理想行），每个崩溃窗口连续 20 轮验证链 verify OK 且状态/归因一致。

## 8. 兼容

旧 `audit_logs` 表与 `/api/admin/audit-logs` 保持只读兼容；旧数据不迁移，新审计走新表。
