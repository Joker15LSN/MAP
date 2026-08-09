# 身份与权限契约（FIX-P2-CONTRACT-E2E-01）

> 对应整改：FIX-P0-AUTH-01。本契约是 BFF 与前端/代理/内部服务之间的权威约定。

## 1. 认证模式（`MAP_AUTH_MODE`）

| 模式 | 适用 | 行为 |
| --- | --- | --- |
| `dev` | 仅非 prod（`MAP_ENV=prod` 时启动失败） | 固定本地管理员 `local-admin`，roles=`platform_admin` |
| `trusted_header` | 所有环境 | 必须 `MAP_TRUSTED_PROXY_REQUIRED=true` 且配置 `MAP_TRUSTED_PROXY_SECRET`，否则启动失败（fail-closed） |
| `oidc` | R3 | 未实现，任何请求 501 `NOT_IMPLEMENTED` |

## 2. 代理身份 Header（仅可信代理注入）

| Header | 含义 |
| --- | --- |
| `X-Trusted-Proxy-Secret` | 代理共享 secret（常量时间比较；从不记录/返回/审计） |
| `X-UserId` | 用户 subject/user_id（必须非空） |
| `X-User-Roles` | 逗号分隔角色，如 `platform_admin,audit_viewer` |
| `X-User-Staff-Code` / `X-User-Name` / `X-User-Department` | 可选扩展 |
| `X-Workspace-ID` | 可选；缺省用 `MAP_DEFAULT_WORKSPACE_ID` |

浏览器自报的 roles/workspace/subject 一律不可信；只有通过代理验证后才读取。

## 3. 权限矩阵（统一 PermissionService）

| 资源 | platform_admin | audit_viewer | member |
| --- | --- | --- | --- |
| `/api/admin/*` 写 | ✅ | ❌ | ❌ |
| `/api/v1/admin/*` 读（audit/feedback） | ✅ | ✅ | ❌ |
| 自己的会话/消息/反馈 | ✅ | ✅ | ✅ |
| 他人会话/消息/反馈 | 404 | 404 | 404 |

## 4. 服务身份（`/internal/v1`）

- `Authorization: Bearer <service-token>`：token 来自 `MAP_SERVICE_TOKEN_SECRET`（逗号分隔支持轮换），常量时间比较。
- `X-Service-Name`（合法 ID）、`X-Service-Audience`（须等于 `MAP_SERVICE_AUDIENCE`，默认 `map-bff`）、`X-Service-Scopes`（逗号分隔）。
- 浏览器/用户 token 永远无法通过服务身份校验（401 `INVALID_SERVICE_IDENTITY`）；scope 不足 403 `FORBIDDEN`。

## 5. 错误 envelope（所有 `/api/v1` 与 `/internal/v1`）

```json
{"code": "FORBIDDEN", "message": "platform_admin role required", "details": null, "request_id": "..."}
```

稳定 code：`AUTHENTICATION_REQUIRED` / `FORBIDDEN` / `RESOURCE_NOT_FOUND` / `VALIDATION_ERROR` /
`VERSION_CONFLICT` / `IDEMPOTENCY_CONFLICT` / `NOT_IMPLEMENTED` / `INTERNAL_ERROR` /
`BAD_STATE_FILE` / `STORE_WRITE_FAILED` / `CONCURRENT_MODIFICATION` / `AUDIT_WRITE_FAILED`。
旧 `/api/*` 路径保持 `{"detail": ...}`（兼容）。

## 6. 数据所有权不变量

- repository 查询自带 `workspace_id + owner/user scope`；先查后过滤 = 缺陷。
- 跨 workspace/跨用户读取统一 404（不泄漏资源存在性）。
