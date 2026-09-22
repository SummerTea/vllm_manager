# vllm_manager

vLLM 推理服务集群管理平台（基于 **project-scaffold 样板工程**的模块化单体骨架）。

> 当前为**纯样板骨架**状态：backend（FastAPI + PostgreSQL + Redis + SAQ）与 frontend（React SPA）基础设施已就位并验证通过，业务功能（节点管理、vLLM 实例分配/启停、GPU 监控）将逐个在此骨架上实现。

## 目录结构

```
vllm_manager/
├── backend/                  # FastAPI 后端（Poetry 工程，in-project venv）
│   ├── pyproject.toml        # 依赖（fastapi/sqlalchemy-asyncpg/redis/saq/uuid-utils）
│   ├── .env.example          # 配置模板（应用/数据库/Redis/日志/可选）
│   ├── .env                  # 本机配置（不入库）
│   ├── app/
│   │   ├── config.py         # AppConfig 单例（pydantic-settings，frozen）
│   │   ├── exception.py      # 分层异常体系
│   │   ├── main/             # 入口包：app 单例/lifespan/routers/web_routers/static_files
│   │   ├── work_saq.py       # SAQ worker 入口（-q/-c/--check）
│   │   ├── base/             # base_config/base_crud/base_enum/base_model/响应契约
│   │   ├── extensions/       # database/redis/saq/logging（懒加载，import 零连接）
│   │   └── utils/
│   └── tests/                # 样板契约测试
├── frontend/                 # React 19 + Vite + Tailwind + shadcn/ui（bun）
│   ├── package.json / vite.config.ts   # base=/vllm_manager/frontend/
│   └── src/                  # 路由/布局/登录占位（样板示例页）
├── docs/                     # 架构与借鉴文档（gpustack-borrowings.md）
├── docker-compose.yml        # PG + Redis（本机已有环境时可跳过）
└── AGENTS.md
```

## 快速开始

```bash
# backend（工作目录 backend/）
poetry install
cp .env.example .env          # 按需修改（本机默认配置已写好 backend/.env）
poetry run uvicorn app.main:app --port 8000     # manager 入口
poetry run pytest                              # 样板契约测试（16 passed）

# frontend（工作目录 frontend/）
bun install
bun run dev                   # 开发 :5178，SPA 访问 /vllm_manager/frontend/
bun run build                 # 构建
```

## 样板关键机制（写业务前必读）

- **配置**：`app_config`（frozen）读 `.env`；`BASE_URL_PATH`（默认 `/vllm_manager`）为所有 REST 与静态资源路径前缀。
- **入口**：`app/main/` 模块级单例 + lifespan（init_logging → db → redis → saq，`DISABLED_EXTENSIONS` 可禁用 db/redis/saq）。
- **响应契约**：`BaseResponse{code, message, data}`；业务失败抛 `app.exception` 分层异常，全局 handler 渲染。
- **懒加载**：`extensions/` 的 database/redis/saq 连接首次调用才创建，import 零连接。
- **前端契约**：`types/api.ts` 与后端 `BaseResponse` 对齐；axios 拦截器透出后端 message；登录为 501 占位（`/vllm_manager/web_api/account/*`）。

详细参考：`backend/tests/`、`docs/gpustack-borrowings.md`（vLLM 实例管理与显存分配的借鉴调研）。
