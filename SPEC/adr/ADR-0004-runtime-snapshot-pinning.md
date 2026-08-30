# ADR-0004：Runtime Snapshot 固定与 PG AdminState 单行事实

- 状态：Accepted
- 日期：2026-08-30
- 决策者：MAP 开发负责人（Step 7 PR-J 实现后记录）
- 相关：ADR-0002（Canonical Run 边界）、P0-CFG-AUTH-01、P1-CONFIG-01

## 背景

管理配置原先以文件 `admin_state.json` 为主路径：读写靠进程内锁 + 原子 rename，
core 通过无认证的 current fetch（`/api/admin/flow-runtime-snapshot`）读取并保留
本地缓存与 static fallback。Run 的 `snapshot_json` 为空对象，历史 Run 无法对
“当时用哪份配置执行”作出可验证回答。

Step 7 的目标是把运行时配置收敛为 PG 中的不可变 Runtime Snapshot，并让每个 Run
固定一个 snapshot id/digest；core 只允许按固定 id 读取，不允许 current fetch
或静默 fallback。

## 决策

1. **完整 AdminState 是 PG 单行事实**：`map_control.admin_state`（singleton
   `id=1`）保存 `state_json` 与重算可验证的 `state_hash`。管理写与 snapshot
   激活在同一 PG 事务内原子提交；文件 JSON 主路径、shared volume、startup
   file reconciler 物理删除。
2. **Runtime Snapshot 只保存运行时投影**：`scene_selection`、`dispatch_config`、
   `flow_policy`、`scenario_packs`、`flow_skill_descriptors`；不保存完整
   AdminState。snapshot 的 digest 只覆盖投影内容，secret 以
   `api_key_ref: "env:MAP_LLM_API_KEY"` 形式出现，不落真实值。
3. **不可变 + 指针 CAS**：`runtime_snapshots` 行的投影/digest/schema_version/
   parent_id 一经插入不可变（DB trigger + 应用层无 update 口）；全局至多一个
   `active`（partial unique index）。current pointer 单行用 `SELECT ... FOR
   UPDATE` + expected digest 做 CAS，并发 activate/rollback 只有一个 winner。
4. **生命周期状态机**：
   `draft → published → active → rolled_back → retired`（`retired` 终态）；
   允许 `draft→published|retired`、`published→active|retired`、
   `active→rolled_back`、`rolled_back→active|retired`。publish/activate/
   rollback/retire 各产生一条 hash-chained snapshot audit 与一条 outbox。
5. **Run 固定 snapshot**：`runs.runtime_snapshot_id/runtime_snapshot_digest`
   为显式列，创建 Run 时服务端读取 current pointer 固化；旧 Run 允许 NULL，
   新 Run 两列同空同非空。同 idempotency key 重放返回已存 Run 的固定值。
6. **core 只读固定 id**：BFF 暴露
   `GET /internal/v1/runtime-config-snapshots/{id}`（service identity scope
   `runtime-config.snapshots.read`）；core 通过带 Bearer service credential 的
   transport 按 `X-Runtime-Snapshot-ID/Digest` 读取，并双向重算 digest。
   401/403/404/schema mismatch/digest mismatch 全部 fail-closed；不存在
   current fetch、TTL 缓存或 static fallback。
7. **迁移与回滚**：旧 JSON 通过幂等导入命令（`--check/--apply`）迁入 PG
   admin_state 与首个 active snapshot，重跑不重复；旧 `config_mutations`/
   `runtime_snapshot_mutations` pending 先 drain 再 drop。实现与删除分
   commit，可独立 revert。

## 后果

- 每个 Run 全程只有一个 runtime snapshot id/digest，历史 Run 可按 id 重放，
  不受后续 publish/activate/rollback 影响。
- 管理写入、snapshot 激活、审计与 outbox 在同一事务，不再有文件 rename
  与 PG 之间的崩溃窗口，因此不再需要 pending mutation 行与 file reconciler。
- core 与 BFF 之间增加 service identity 与 digest 双重校验；配置读取失败
  不再静默降级，调用方必须处理 typed 错误。
- 七类资产业务语义（owner/version/state/dependency/delete guard/eval/canary）
  与前端 DTO 生成仍属后续工作；本 ADR 不把它们声明为已完成。
