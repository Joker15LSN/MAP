# Mongo state_store 退役 retention runbook（P1-CLEAN-STATE-01）

> 状态：待执行（owner：platform-security / development owner）。
> 前置：core 已停止写四个 telemetry collection（PR-K8）；observability backend
> 在 P1-OBS-01 切换 OTel 原生查询前仍可能读旧 collection，故**只停写、不提前 drop**。
> 本文不构成已完成证据；完成时须回填「执行记录」与签名。

## 1. 涉及 collection（`map_core` 旧 telemetry，非业务数据）

- `agent_executions`
- `tool_call_records`
- `request_records`
- `llm_call_records`

`agent_session_memories` 是业务数据，不在本 runbook，不得随本流程 drop。

## 2. Export + digest（owner 执行，写前锁库只读窗口）

```bash
OUT=/var/backups/map/mongo-state-retirement-$(date +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT"
for COL in agent_executions tool_call_records request_records llm_call_records; do
  mongodump \
    --uri "$MONGO_PROD_URI" \
    --db "$MONGO_PROD_DB" \
    --collection "$COL" \
    --gzip --archive="$OUT/$COL.archive"
  sha256sum "$OUT/$COL.archive" > "$OUT/$COL.archive.sha256"
done
cat "$OUT"/*.sha256 > "$OUT/manifest.sha256"
```

- 用 `mongosh "$MONGO_PROD_URI/$MONGO_PROD_DB" --eval 'db.stats()'` 记录
  `dataSize/avgObjSize/count` 作为对账基准。
- 对每个 archive 记录 `sha256`、`count`、`exported_at` 到本文件「执行记录」。

## 3. Restore 演练（隔离库）

```bash
RESTORE_DB="map_state_restore_verify"
for COL in agent_executions tool_call_records request_records llm_call_records; do
  mongorestore \
    --uri "$MONGO_PROD_URI" \
    --nsFrom "$MONGO_PROD_DB.$COL" \
    --nsTo "$RESTORE_DB.$COL" \
    --gzip --archive="$OUT/$COL.archive"
done
mongosh "$MONGO_PROD_URI/$RESTORE_DB" \
  --eval 'for (const c of ["agent_executions","tool_call_records","request_records","llm_call_records"]) print(c, db[c].countDocuments({}))'
```

验收：每 collection `count` 与 export 基准一致；抽样 100 条逐字段比对；
archive sha256 与 manifest 一致。完成后删除 `RESTORE_DB`。

## 4. Drop（仅在 P1-OBS-01 切读 + 本演练双签后）

```bash
for COL in agent_executions tool_call_records request_records llm_call_records; do
  mongosh "$MONGO_PROD_URI/$MONGO_PROD_DB" \
    --eval "db.$COL.drop()"
done
```

之后执行 `grep`：仓库 compose/代码/文档/镜像环境零引用四个 collection 名。

## 5. 签名与执行记录

| 字段 | 值 |
| --- | --- |
| export 执行人 | （owner 填写） |
| restore 验证人 | （owner 填写） |
| drop 批准人 A / B | （owner 填写） |
| export manifest sha256 | （owner 填写） |
| restore count 对账 | （owner 填写） |
| drop 日期 / 窗口 | （owner 填写） |

未填写前，AC-CLEAN-STATE-03 视为未通过；本地代码零引用只满足其静态部分。
