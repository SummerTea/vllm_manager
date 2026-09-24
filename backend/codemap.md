# backend/

## Responsibility
后端工程根目录（服务端分层的基础设施/服务装配层底座）：承载 FastAPI + SQLAlchemy 2.0 async 的**双进程后端**——server 管理端（:8000，Web + REST，含 node/instance/allocator 业务域）与 worker 管控节点（:8100，独立进程，状态全内存）。本目录定义依赖清单、工具链、测试契约，并作为 `app/` 业务包的宿主。

## Design
- **单项目双入口**：一个 Poetry 工程、两个互不依赖的进程入口——`app.main:app`（server）与 `app.worker.main:create_app`（worker，`work_worker.py` 用 uvicorn factory 模式拉起），worker 不连 PG、不 import server 装配链；worker 依赖 docker + `WORKER_VLLM_IMAGE` 容器化拉起 vLLM，加速器类型按 `WORKER_ACCELERATOR`（gpu|cpu）声明，A800 多卡 tp 部署已打通。
- **模块化单体**：`app/` 内按 业务域（server/worker）+ 基座（base）+ 扩展（extensions）+ 工具（utils）组织，分层依赖单向（业务域 → base → extensions）。
- **Poetry 管理**：运行时依赖收敛（fastapi/uvicorn/sqlalchemy[asyncio]/asyncpg/pydantic-settings/redis/saq[hiredis]/uuid-utils/pytz/greenlet/python-dotenv/httpx/anyio/itsdangerous/psutil/nvidia-ml-py/tenacity），dev 依赖 pytest/pytest-asyncio/aiosqlite/ruff/pyright/mypy；in-project venv（`.venv/`）。
- **配置外置**：`.env`/`.env.example` 供给 pydantic-settings 读取；`.env` 不入库。
- **契约测试**：`tests/`（pytest，284 passed）是业务 API 的响应/异常/URL 契约样板，覆盖 server（node/instance/allocator/prober/转发）+ worker（config/collector/client/lifecycle/process_utils/api）双端，新增业务须仿照补测。

## Flow
- server 启动：`uvicorn app.main:app` → app 单例装配 → lifespan（init_logging→init_db→init_redis→init_saq→prober/reconcile 后台任务）→ 路由挂载（`/vllm_manager/api/v1`、`/vllm_manager/web_api`、前端 SPA 静态挂载）。
- worker 启动：`python app/work_worker.py` → `uvicorn.run("app.worker.main:create_app", factory=True)` → 注册 server → restore 实例 → 后台循环（心跳/状态上报/对账/权威判定）。
- SAQ worker：`python app/work_saq.py -q default` → 自建 DB/Redis/SAQ 连接 → 消费队列任务。

## Integration
- `app/`：全部业务与基础设施代码（见 `app/codemap.md` 索引）。
- `pyproject.toml`：依赖与工具链配置（ruff/pytest/pyright 规则在此声明）。
- `frontend/`：构建产物由 server 通过 `FRONTEND_DIST_DIR`（`../frontend/dist`）静态挂载。
- `docker-compose.yml`（仓库根）：本机 PG + Redis 基础设施，backend 通过 `DB_*`/`REDIS_*` 配置连接。
- `data/`：worker 运行时数据目录（`WORKER_MODEL_ROOT`=模型根、`WORKER_LOG_DIR`=实例日志、`WORKER_CACHE_DIR`=VLLM_CACHE_ROOT 持久目录，均默认 `{BASE_PATH}/data/...`）。
- `logs/`：server 侧应用日志输出（`LOG_FILE` 默认 `{BASE_PATH}/logs/app.log`，RotatingFileHandler 轮转）。

## 导航提示
- 想理解整体架构先读 `docs/architecture.md` 与 `docs/gpustack-borrowings.md`（仓库根）。
- 关键文件：`pyproject.toml`（依赖）、`.env.example`（配置模板）、`app/config.py`（AppConfig 单例）、`app/work_worker.py`/`app/work_saq.py`（入口）。
- 新增业务契约测试参照 `tests/` 样板（响应/异常/URL 契约的既有写法）。
