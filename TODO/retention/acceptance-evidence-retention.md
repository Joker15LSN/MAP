# Acceptance Evidence Retention 策略（Step 10）

> 状态：Normative（本地策略落地；物理归档/删除由 owner 在 immutable storage
> 就绪后执行）。

## 原则

1. Git 只应保留**当前有效 freeze** 的 `tmp/acceptance/<task>/<freeze_sha>/<ac>/`
   结构（index + manifest + logs）；历史 freeze 的完整数据放 CI artifact /
   immutable object storage。
2. Superseded manifest 只在 restore、trust chain 验证或 owner 审批后处理；
   agent 不得自行删除历史 evidence 文件。
3. 本地生成的 pass manifest 是 **unattested**，结构验证通过但 release 验证
   不通过；只有受保护 CI 才能 attest。

## 当前状态

- 当前有效 freeze：`0eb61ed2`（Step 8 实现后重冻结）。
- 历史 freeze：`03ad82a6…、16b6b3c5…、1feb804c…` 等（多数已标 superseded）。
- 生成索引：`python3 scripts/evidence_retention.py --repo . --out tmp/acceptance/index.json`
- 打包归档（owner 执行，需要 `$ARCHIVE_URI` 指向 immutable storage）：
  ```bash
  python3 scripts/evidence_retention.py --repo . --archive-candidates \
    --archive-uri "$ARCHIVE_URI"
  ```
  该命令只输出/上传候选清单并记录 manifest，**不删除本地文件**。
- 物理清理（仅 owner + immutable storage 校验通过后）：
  ```bash
  python3 scripts/evidence_retention.py --repo . --prune-archived \
    --archive-manifest <sha256-verified-manifest>
  ```

## 索引字段

`tmp/acceptance/index.json`：`generated_at/freeze_sha/current_manifest_count/
superseded_freeze_shas/archive_commands/tracked_file_count`。
