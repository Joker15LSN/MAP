# MAP 开发指南

- 状态：Living
- 最后核对：2026-08-24

## 1. 开始前

先阅读：

1. [`CONTEXT.md`](../CONTEXT.md)：统一领域名称；
2. [`SDD.md`](SDD.md)：当前与目标系统；
3. [`TDD.md`](TDD.md)：模块和迁移约束；
4. 任务对应的 [`SPEC/contracts/`](../SPEC/contracts/) 与 ADR；
5. 对应服务 README。

前置工具：Docker Engine/Desktop、Docker Compose v2；本地直接运行模块时还需适配版本的
Python、[`uv`](https://docs.astral.sh/uv/) 和 Node.js/npm。Python 最低版本以各模块
`pyproject.toml` 为准：BFF 3.11、Core 3.13、观测后端 3.9。

## 2. 推荐开发方式

### 完整栈

首次运行从根目录复制环境模板并填写必需值：

```bash
cp .env.example .env
docker compose up -d --build
```

端口、必填模型配置、健康验证和停止方式以根目录 [`README.md`](../README.md#快速开始)
为准。`.env` 可能包含秘密，不提交、不粘贴到日志或测试 Evidence。

可选模块：

- OpenSandbox：Compose `sandbox` profile；
- OTel Collector / Jaeger：`docker-compose.otel.yml` 或 `otel` profile；
- 生产约束：`docker-compose.prod.yml`，不能用开发默认值代替生产配置。

### 单模块

当改动局部且依赖可替换时，可只运行所属模块。准确命令见：

- [`map-business-backend/README.md`](../map-business-backend/README.md)
- [`map_core/README.md`](../map_core/README.md)
- [`map-business-frontend/README.md`](../map-business-frontend/README.md)
- [`map-observability/README.md`](../map-observability/README.md)

依赖同步使用冻结 lockfile：Python `uv sync --frozen`，前端 `npm ci`。不要通过临时升级依赖
解决本地环境问题后遗漏 lockfile 和供应链验证。

## 3. 日常工作流

1. **确定事实和目标**：定位当前实现、权威 contract/ADR 和执行计划，标明差异。
2. **缩小修改范围**：找出拥有该策略的模块、调用者和适配器，不从相似文件批量改起。
3. **先固定行为**：为删除、迁移或缺陷补充可失败的测试；记录兼容与故障语义。
4. **逐个迁移调用者**：每次保持系统可运行，替代路径通过后立即删除旧入口。
5. **按风险验证**：使用 [`TESTING.md`](TESTING.md#10-按变更选择验证) 选择测试并保存结果。
6. **同步文档**：更新事实源、说明文档、服务 README 和操作手册。
7. **检查差异**：确认没有意外格式化、生成物、秘密或与任务无关的修改。

## 4. 模块与代码规则

- Router/Controller 只负责协议 adapter；业务规则进入拥有该决策的 application module。
- 状态转移、重试、身份、错误和事件 schema 各保留一个事实源。
- 优先小接口、清晰数据流和显式依赖；避免 `Manager`、`Helper`、`Common` 式无边界聚合。
- 没有真实替换需求时不创建 interface + single implementation 的样板层。
- 不以继承复用偶然相似流程；优先组合纯规则与小 adapter。
- 删除兼容代码前提供使用证据、迁移完成证明和回滚路径。
- 变更命名时使用 [`CONTEXT.md`](../CONTEXT.md) 的统一语言，避免同一概念多个别名。
- 注释说明“为什么”和约束，代码本身表达“做什么”。

详细评审标准见 [`TDD.md`](TDD.md#1-技术设计原则) 和
[`SPEC/STANDARDS.md`](../SPEC/STANDARDS.md)。

## 5. API 与事件变更

1. 先修改对应 contract 或新增 ADR；
2. 更新拥有者 schema/OpenAPI/event validator；
3. 增加 producer 与 consumer 两侧契约测试；
4. 定义版本、兼容窗口和未知版本行为；
5. 更新生成的前端 DTO，不手写同义类型；
6. 更新 SDD/TDD 中的状态，不把目标功能写成已实现。

Public `/api/v1` 与 internal `/internal/v1` 分开维护。新增浏览器能力不能通过 internal 路由或
直连 Core 绕过 BFF。

## 6. 数据库与迁移

- Schema 变化只通过 Alembic migration；应用启动不自动执行 DDL。
- Compose 中由一次性 `migrate` 模块使用 `map_migrator` 角色，应用使用非超级角色。
- 长期运行环境采用 expand/migrate/contract：先兼容读写，再迁移/回填，最后删除旧结构。
- migration 必须对 fresh database 和已存在数据分别验证。
- 回填需可重入、可观测并限制批量；不在请求事务中做大规模迁移。
- 降级会丢数据时不伪造可逆 downgrade，应在运维手册明确前滚恢复方式。
- 修改所有权、唯一约束、lease 或 append-only 表时，补真实 PostgreSQL 并发/权限测试。

## 7. 配置与秘密

- 新环境变量使用清晰前缀并加入 `.env.example`、settings 校验、Compose 和相关 README。
- 生产必需配置必须 fail-fast 或使 readiness 失败，不能回退到仓库默认口令/地址。
- 环境变量只在组合根/settings 解析；内部模块接收验证后的配置对象。
- 不读取或提交 `.env`、私钥、token、真实连接串、浏览器凭据或生产 Evidence。
- 新依赖或临时安全例外遵循 [`SECURITY_EXCEPTIONS.md`](../SECURITY_EXCEPTIONS.md)。

## 8. 文档随代码变化

| 代码变化 | 文档动作 |
| --- | --- |
| 新领域概念或改名 | 更新 `CONTEXT.md`，迁移代码/contract 中的旧词 |
| 新模块或所有权变化 | 更新 SDD、TDD，必要时写 ADR |
| API/Event/状态变化 | 更新 contract、OpenAPI、测试与服务 README |
| 新端口/profile/变量 | 更新 Compose、`.env.example`、OPERATIONS、根 README |
| 新故障/恢复语义 | 更新 TDD、TESTING、OPERATIONS |
| 计划项完成 | 把“目标/过渡中”改为“已实现”，附验证依据 |

不要把同一大段协议复制到多个 README；服务 README 只保留局部运行和入口信息，并链接到
权威 contract。

## 9. 提交前检查

```bash
git diff --check
git status --short
```

然后运行与风险相称的定向测试、所属模块全量测试，以及需要的 E2E/release gate。检查：

- 无秘密、临时日志、缓存、构建产物或验收产物；
- 新旧接口没有无期限双写/双读；
- 错误、取消、超时、重试和未知结果有测试；
- 文档链接、命令和状态声明准确；
- 变更没有扩大到任务无关区域。

## 10. 完成定义

一个变更只有同时满足以下条件才算完成：

1. 目标行为和非目标范围清楚；
2. 实现遵守 contract、ADR、身份、所有权与单写者约束；
3. 正常、错误、并发和恢复路径按风险验证；
4. 替代路径落地后，旧代码、旧测试、旧配置和旧文档已删除或有明确退役门槛；
5. 相关 SDD/TDD/contract/README/运维文档已同步；
6. 测试命令、结果和任何未解决风险可追溯；
7. 工作树只包含预期修改。
