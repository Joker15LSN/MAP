# Runtime Snapshot 契约（P0-CFG-AUTH-01 / P1-CONFIG-01）

> 状态：Normative。ADR：[ADR-0004](../adr/ADR-0004-runtime-snapshot-pinning.md)。
> 实现：`map-business-backend/app/services/runtime_snapshot/`；
> 读取：`map_core/map_core/service/runtime_snapshot_transport.py`。

## 1. 事实与所有权

- `map_control.admin_state`（singleton `id=1`）是完整管理配置的唯一读写事实。
- `map_control.runtime_snapshots` 是不可变运行时投影事实；
  `map_control.runtime_snapshot_current` 是 current pointer 的唯一事实。
- BFF 是唯一写者；core 是只读消费者，不直连 PG。
- file-backed JSON、shared volume、startup file reconciler 已删除。

## 2. 投影 schema 与 digest

Runtime projection（`schema_version=1`）包含：

- `scene_selection`
- `dispatch_config`
- `flow_policy`
- `scenario_packs`
- `flow_skill_descriptors`

digest = `sha256(canonical_json({"schema_version": 1, ...projection}))`，其中
`canonical_json` 固定 `sort_keys=True, ensure_ascii=False,
separators=(",", ":")`。id/parent_id/created_at/status 不参与 digest。
真实 secret 不进入 projection：LLM credential 只以
`{"api_key_ref": "env:MAP_LLM_API_KEY"}` 表达。

## 3. 生命周期状态机

```
draft → published → active → rolled_back → retired(终态)
draft→published|retired
published→active|retired
active→rolled_back（被新 activate/rollback 取代）
rolled_back→active|retired
```

不变量：

- 投影/digest/schema_version/parent_id 不可变（DB trigger 兜底）；
- 至多一个 `active`（partial unique index）；
- 任何指针移动都是 CAS：`expected_current_digest` 不匹配即
  `SNAPSHOT_CONCURRENT_MODIFICATION`，状态前置不满足即
  `SNAPSHOT_STATE_CONFLICT`。

## 4. Run 固定

- `runs.runtime_snapshot_id` / `runs.runtime_snapshot_digest` 显式列；
  新 Run 两列同空同非空，创建时由 BFF 读取 current pointer 固化。
- 同 `Idempotency-Key` + 同 body hash 重放返回已存 Run 与已存固定值。
- 历史 snapshot 在 Run 生命周期内不被改写；activate/rollback 只影响新 Run。

## 5. internal 读取接口

```
GET /internal/v1/runtime-config-snapshots/{snapshot_id}
Authorization: Bearer <service-token>
scope: runtime-config.snapshots.read
```

- 200 body：`{id, schema_version, digest, parent_id, created_at, projection}`；
  headers：`ETag: "<digest>"`、`X-MAP-Snapshot-Digest: <digest>`、
  `Cache-Control: no-store`。
- `draft` 与不存在同形 404 `SNAPSHOT_NOT_FOUND`；
- 无/错 token、audience 不匹配 401 `INVALID_SERVICE_IDENTITY`；
- scope 不足/header 越权 403 `FORBIDDEN`；
- 存储 digest 与投影重算不一致 500 `SNAPSHOT_DIGEST_MISMATCH`。
- 不存在 current 读取端点。

## 6. core 消费

- BFF 转发 `X-Runtime-Snapshot-ID` / `X-Runtime-Snapshot-Digest`；
- core transport 重算 digest 并比较 header/body，任何不一致 fail-closed；
- 缺失 id/digest → `RUNTIME_SNAPSHOT_MISSING`；401/403 →
  `RUNTIME_SNAPSHOT_AUTH`；404 → `RUNTIME_SNAPSHOT_NOT_FOUND`；
  digest → `RUNTIME_SNAPSHOT_DIGEST_MISMATCH`；schema → `RUNTIME_SNAPSHOT_SCHEMA`。
- 无缓存、无 current fetch、无 static fallback。

## 7. 迁移

- `python -m app.services.runtime_snapshot.migrate --state-file <old.json>
  [--check|--apply]`：幂等导入 AdminState 与首个 active snapshot；
  已有 digest 冲突时不写、exit 2，真实生产数据迁移由 owner 执行。
- 旧 pending mutation 表先 drain 后 drop；运行时不依赖任何本地 JSON。
