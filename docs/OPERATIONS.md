# MAP 运维手册

- 状态：Living
- 最后核对：2026-08-24
- 适用范围：本仓库 Compose 部署、健康检查、升级、恢复和故障分诊

本文不是生产平台专用 runbook。集群、负载均衡、秘密管理、对象存储和备份平台的具体
操作应由部署环境维护；本文件固定应用侧顺序、不变量和验证点。

## 1. 环境与拓扑

| 模式 | 入口 | 用途 |
| --- | --- | --- |
| 开发基础栈 | `docker-compose.yml` | 本地数据库、BFF/Worker/Core、两个前端和观测后端 |
| 沙箱 | 基础栈 + `--profile sandbox` | 启用 OpenSandbox Server |
| 分布式追踪 | 基础栈 + `docker-compose.otel.yml --profile otel` | 启用 Collector/Jaeger 和 exporter |
| 生产约束 | `scripts/compose-prod.sh` + `docker-compose.prod.yml` | 强制 `MAP_ENV=prod`、显式 CORS 和沙箱服务凭据 |

默认宿主机入口：

| 模块 | 地址 |
| --- | --- |
| 业务前端 | `http://localhost:5174` |
| BFF | `http://localhost:18080` |
| Core（仅开发，绑定 loopback） | `http://localhost:10000` |
| 观测前端 | `http://localhost:15152` |
| 观测后端 | `http://localhost:15151/api/v1` |
| PostgreSQL | `localhost:15432` |
| MongoDB | `localhost:27017` |
| OpenSandbox（profile） | `http://localhost:8080` |
| Jaeger（profile） | `http://localhost:16686` |

生产 override 不发布 Core 宿主机端口。浏览器始终只访问 BFF。

## 2. 配置与启动前检查

`.env.example` 是变量清单和优先级说明，`.env` 是本地秘密载体且不得提交。启动前至少确认：

- PostgreSQL/Mongo 管理、迁移和应用凭据已显式设置；
- 模型 provider、endpoint、model 和 token 满足当前场景；
- `MAP_AUTH_MODE` 与环境相符；生产禁止 `dev`；
- 生产可信代理、CORS origin 和服务凭据已显式配置；
- 启用沙箱时，OpenSandbox 地址、client 与 service credentials 成对配置；
- OTLP protocol 与端口匹配：HTTP/protobuf 通常 `4318`，gRPC 通常 `4317`；
- volume、磁盘、端口和镜像来源满足部署环境要求。

配置检查：

```bash
docker compose config
```

生产必须通过受控入口：

```bash
MAP_ENV=prod scripts/compose-prod.sh up -d
```

不要直接把开发基础栈当作生产部署命令。

## 3. 启动与依赖顺序

Compose 编排的逻辑顺序为：

1. PostgreSQL、MongoDB 健康；
2. Core 与 `migrate` 分别在依赖满足后启动，二者可并行；
3. BFF 等待 Core 健康且 migration 成功，再通过 readiness；
4. Worker 等待 PostgreSQL 健康且 migration 成功，再开始 claim；
5. 业务前端等待 BFF 健康，观测前端等待观测后端健康；
6. 可选 OpenSandbox 与 OTel/Jaeger 由 profile 启动；启用能力前分别验证其健康和配置。

完整开发栈：

```bash
docker compose up -d --build
docker compose ps
```

启用沙箱：

```bash
docker compose --profile sandbox up -d --build
```

启用 OTel overlay：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.otel.yml \
  --profile otel up -d --build
```

## 4. 健康、就绪与冒烟

| 检查 | 语义 | 预期 |
| --- | --- | --- |
| BFF `GET /health` | 进程存活 | 固定成功响应，不证明依赖可用 |
| BFF `GET /ready` | 数据库、迁移、seed 等可接流量条件 | 就绪 200；不就绪 503 与固定 envelope |
| Core `GET /health` | Core 进程健康 | 200 |
| 观测后端 `GET /api/v1/health` | API 进程健康 | 200 |
| OpenSandbox `GET /health` | 沙箱服务健康 | profile 启用时 200 |

基本冒烟：

```bash
curl -fsS http://localhost:18080/health
curl -fsS http://localhost:18080/ready
curl -fsS http://localhost:10000/health
curl -fsS http://localhost:15151/api/v1/health
```

健康探针不携带用户/服务身份，因此只能返回最少基础设施信息。业务能力是否可用还需通过
受身份保护的 smoke 或 E2E 验证。

## 5. 升级流程

### 常规发布

1. 固定批准的 baseline，运行 final release gate 和相应 E2E；
2. 备份并验证 PostgreSQL/Mongo 恢复点；
3. 停止 Worker 领取新任务，发送 SIGTERM 并等待当前 handler 安全结束或 lease 到期；
4. 运行兼容旧应用版本的 expand migration；
5. 部署 BFF/Core，再部署 Worker；
6. 等待 readiness，执行身份、会话、Worker、审计和观测 smoke；
7. 观察错误率、Job lease/uncertain Effect、SSE 中断与 trace 缺口；
8. 完成回填和兼容窗口后，另一次发布执行 contract migration 和旧代码删除。

不要在 Worker 仍运行时直接替换与其 fencing/状态机不兼容的 schema。

### Worker 排空

- SIGTERM 后停止 claim 新 Job；
- 运行中 handler 收到取消信号；
- 等待时间至少覆盖当前 handler 的优雅停止预算；
- 强制终止后，依靠 lease expiry/reconcile 接管，禁止手工把 Job 直接改为成功；
- 外部调用结果未知时保持 `uncertain` 并对账，不能简单重放。

## 6. 数据迁移、备份与恢复

### PostgreSQL

- `migrate` 是唯一自动 DDL 执行者；BFF、Worker、Core 使用非超级应用角色。
- 每次发布记录 Alembic current/head、镜像 SHA 和 migration 日志。
- 备份至少覆盖 `map_control` 的事实、约束、序列和角色权限；恢复演练必须验证审计链。
- 破坏性 migration 前先完成逻辑/物理备份和独立恢复验证。

### MongoDB

- 备份运行记录和观测查询所需 collections/indexes；
- 恢复后校验时间字段、索引和 request/run/trace 关联；
- Mongo 当前是运行观测事实源，但目标只作为 Canonical Event 的投影；迁移期间不要提前删除
  仍被观测页面消费的字段。

### 管理配置快照

管理配置当前值是 PostgreSQL `map_control.admin_state` 单行（JSONB + 哈希）和
`runtime_snapshots` / `runtime_snapshot_current` 的投影。恢复时：

1. 停止配置写入；
2. 恢复 `map_control` 数据库到一致恢复点；
3. 启动 BFF（lifespan 会幂等补齐空库的默认 AdminState 和 active snapshot）；
4. 验证审计链与当前 snapshot 指针一致；
5. 再开放管理写流量。

本仓库当前没有一键生产 backup/restore 脚本；这是部署平台必须补齐并定期演练的运维缺口。

## 7. 回滚原则

- 优先回滚应用镜像，数据库采用前滚修复；只有经过验证且不丢数据的 downgrade 才可执行。
- 新旧应用需在 expand 阶段同时兼容 schema；否则不能进行滚动发布。
- 已产生的 Event、Audit、Attempt、Effect 或 Evidence 事实不可通过回滚删除。
- 配置写入失败先通过 mutation/reconciler 判断结果，不能用旧文件直接覆盖。
- 沙箱或服务身份异常时保持能力关闭，不能为恢复可用性切回宿主执行。

## 8. 观测与告警建议

至少监控：

- BFF/Core/观测 API readiness、5xx、延迟和 SSE 异常终止；
- Job queue depth、claim 延迟、lease loss、attempt、reconcile 和 dead/failed；
- Effect `uncertain`、dispatch timeout、重复执行防护；
- PostgreSQL 连接、锁、磁盘、migration 版本；Mongo 连接、索引和查询延迟；
- 模型/工具超时、重试、token/用量和敏感数据过滤失败；
- OTel exporter drop/queue、trace 断链以及关键运行标识缺失；
- 审计链验证失败、权限拒绝异常波动和安全扫描失败。

排查时优先使用 `request_id`、`workspace_id`、`conversation_id`、适用时的 `run_id` 与
`trace_id` 关联，不以用户输入内容作为唯一检索键。

## 9. 故障分诊

| 现象 | 首查 | 禁止操作 |
| --- | --- | --- |
| BFF 不就绪 | `/ready` 固定 checks、migration、PG 连接/seed | 绕过 readiness 接流量 |
| Message 长期 streaming | Worker、message reconcile、Core 流终态 | 直接改库为 completed |
| Job 重复/卡住 | lease owner/attempt、DB time、heartbeat、handler log | 清空 attempt 或无 fencing 重跑 |
| Effect uncertain | provider 事实查询、dispatch token、幂等键 | 盲目重发外部操作 |
| 审计链失败 | verify 结果、head、migration/权限、恢复点 | UPDATE/DELETE append-only 表 |
| Core 工具不可用 | 沙箱 health、service identity、能力策略 | 开启宿主 shell/Python fallback |
| Trace 缺失 | overlay、endpoint/protocol、export queue、context 传播 | 记录未脱敏业务 payload 代替 trace |
| 跨 Workspace 403 | 可信代理身份、资源所有权、workspace header | 暂时关闭权限校验 |

## 10. 停止与数据清理

停止容器但保留 volumes：

```bash
docker compose down
```

`docker compose down -v` 会删除本地 PostgreSQL/Mongo volumes，属于数据清理操作，只能在
明确不需要数据并已有所需备份时执行。生产环境不得把它当作常规停止命令。

## 11. 发布完成标准

- final release gate 和需要的 E2E 对目标 SHA 通过；
- migration、镜像和配置版本可追溯；
- BFF readiness、Core/观测 health 与受保护 smoke 通过；
- Worker 正常 claim，未出现异常 lease loss 或 Effect uncertain 增长；
- 审计链验证通过；
- trace/运行标识可关联，敏感字段未泄漏；
- 回滚/前滚路径和已知风险已交接。
