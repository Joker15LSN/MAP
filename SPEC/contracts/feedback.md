# 反馈契约（FIX-P2-CONTRACT-E2E-01）

> 对应整改：FIX-P1-FEEDBACK-01。

## 1. 数据模型（当前事实）

`message_feedback(id, workspace_id, conversation_id, message_id, request_id, user_id, rating(helpful|unhelpful), reason_codes(枚举), reason_other, correction_text, status(open|converted|dismissed|withdrawn), version, withdrawn_at, created_at, updated_at)`

- 同一用户同一消息当前记录（`status<>withdrawn`）恒为 0 或 1（partial unique `(message_id, user_id)`）。
- 点赞改点踩是覆盖（version+1），不新增行；撤回是 tombstone（`withdrawn`），物理行保留供审计。
- 旧 `kind/reason` 列保留（只读兼容）；legacy 行 `user_id=NULL` 不参与唯一约束。

## 2. API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/api/v1/messages/{id}/feedback` | 创建/覆盖当前用户反馈（幂等；`rating` + `reason_codes` + `reason_other` + `correction_text`） |
| GET | `/api/v1/messages/{id}/feedback` | 当前用户反馈（无则 `null`） |
| DELETE | `/api/v1/messages/{id}/feedback` | 撤回（tombstone + outbox `feedback_withdrawn` 事件） |
| POST | `/api/v1/feedback/aggregate` | 当前用户可见消息的计数（无他人理由） |
| GET | `/api/v1/conversations/{id}/feedback-summary` | 会话级 helpful/unhelpful 计数 |
| GET | `/api/v1/admin/feedback` | 管理列表（audit_viewer；workspace predicate + rating/reason_code 筛选） |
| POST | `/api/v1/admin/feedback/{id}:convert-to-evaluation-case` | R1-EVAL 未实施时 501 `NOT_IMPLEMENTED`（`MAP_EVAL_CONVERT_ENABLED=false`），绝不创建假 case |
| DELETE | `/api/v1/messages/{id}/feedback/{kind}` | 旧兼容 facade（只删 legacy 行） |

## 3. 约束

- 只能反馈当前用户可见且 `status=completed` 的 assistant 消息；非 assistant/未完成 422 `VALIDATION_ERROR`。
- `reason_codes` 枚举：`incorrect|outdated|no_evidence|not_relevant|unsafe|too_verbose|tool_failed|other`；`other` 必须填 `reason_other`。
- 自由文本（`reason_other`/`correction_text`）统一 redaction：`api_key/token/authorization/cookie/password/secret` 键值被替换为 `[REDACTED]` 后才持久化。
- 聚合只对显式 helpful/unhelpful 计数；北极星口径 `helpful/(helpful+unhelpful)` 分母显式（无反馈消息计入分母为 0）。

## 4. 数据迁移

- expand/contract：只加列/索引/backfill，不删旧表旧列。
- `scripts/verify_feedback_backfill.py`：输出 legacy/new 行数、冲突清单（同消息双 kind 需人工决策，不静默任选）与稳定 hash；可重复运行。
