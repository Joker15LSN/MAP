# MAP (Multi Agent Path)

MAP 是一个多智能体平台仓库，按职责拆分为四类服务：

1. 前端服务（`map-business-frontend`）
2. 后端服务（`map-business-backend`）
3. 算法服务（`map_core`）
4. 观测服务（`map-observability`）

所有 Python 服务统一在容器内使用 `uv` 管理依赖与运行环境。

## 仓库结构

```text
MAP/
├── SPEC/                              # 系统设计与规范文档
├── map-business-frontend/             # 业务前端服务（React + Vite）
├── map-business-backend/              # 业务后端服务（BFF）
├── map_core/                          # 算法服务（FastAPI）
├── map-observability/                 # 观测服务（前后端）
├── packages/
│   └── map-tree-core/                 # 共享问答树能力
└── docker-compose.yml                 # 全量本地编排入口
```

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

可先复制端口模板：

```bash
cp .env.example .env
```

默认端口：

- 前端服务：`http://localhost:5174`
- 后端服务（BFF）：`http://localhost:18080`
- 算法服务：`http://localhost:10000`
- 观测前端：`http://localhost:15152`
- 观测后端：`http://localhost:15151`
- PostgreSQL：`localhost:15432`
- MongoDB：`localhost:27017`

如果端口冲突，可通过环境变量覆盖（示例）：

```bash
MAP_OBS_BACKEND_PORT=25151 MAP_OBS_FRONTEND_PORT=25152 docker compose up -d --build
```

停止：

```bash
docker compose down
```

清理（含数据卷）：

```bash
docker compose down -v
```

## 文档入口

- 总体设计：`SPEC/ARCHITECTURE.md`
- 工程规范：`SPEC/STANDARDS.md`
- 文档索引：`SPEC/README.md`

## 四类服务说明

- 前端服务说明：`map-business-frontend/README.md`
- 后端服务说明：`map-business-backend/README.md`
- 算法服务说明：`map_core/README.md`
- 观测服务说明：`map-observability/README.md`
