# MAP 工程规范

## 1. 命名规范

- 项目统一品牌：`MAP`（Multi Agent Path）。
- 服务命名统一使用：
  - `frontend-service`
  - `backend-service`
  - `algorithm-service`
  - `observability-service`
- 环境变量统一前缀：`MAP_`（业务层）或服务约定前缀（如 `VITE_`）。

## 2. 目录规范

- 根目录必须包含：
  - `README.md`
  - `SPEC/`
  - `docker-compose.yml`
- 四类服务目录必须各自包含 `README.md`。
- 共享能力放在 `packages/`，禁止复制粘贴到多个服务。

## 3. 接口规范

- 前端不得直连算法服务，统一走 BFF。
- 流式接口优先使用 SSE，事件包含：`start/meta/content_delta/done/error`。
- 新增接口时需在对应服务 README 中补充示例与字段说明。

## 4. 配置规范

- 所有可变配置优先走环境变量。
- 禁止在代码中写死部署地址、账号、路径（本地开发默认值除外）。
- 与容器相关的默认路径应使用容器内路径（例如 `/app/data/...`）。

## 5. 容器规范

- Python 服务容器必须使用 `uv` 管理依赖：
  - 构建阶段：`uv sync --no-dev`
  - 运行阶段：`uv run ...`
- Docker Compose 作为本地联调统一入口。
- 每个核心服务建议提供 `health` 接口并配置 `healthcheck`。

## 6. 文档规范

- 设计变更先改 `SPEC`，再改代码。
- 端口、路径、启动命令必须与仓库真实配置一致。
- 文档优先写“职责边界 + 运行方式 + 依赖关系”。
