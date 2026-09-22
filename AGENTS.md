# vllm_manager

vLLM 推理服务集群管理平台。当前为 **project-scaffold 样板骨架**（backend + frontend 双目录模块化单体），业务功能（GPU 节点登记、基于显卡显存的 vLLM 实例分配/启停、GPU 监控）将逐个在此骨架上实现。

**规划**：server 管理端（Web + REST，:8000）通过 REST 向各 GPU 机器上的 worker 管控节点（:8100）转发启停指令，并基于显存对 vLLM 实例做分配管理。

## 先读什么

- `README.md`：工程快速上手（结构、启动、验证）
- `docs/gpustack-borrowings.md`：**gpustack 借鉴调研**——vLLM 实例生命周期与显存分配的落地规格（写业务前必读）
- `backend/tests/`：样板契约测试（了解响应/异常/模型/URL 契约的实际写法）
- `.slim/clonedeps/repos/gpustack__gpustack/codemap.md`：gpustack 参考源码的 Repository Atlas（深入参考前先读导航）

## 项目边界与入口

| 入口 | 位置 | 端口 | 说明 |
|------|------|------|------|
| backend server | `backend/app/main:app` | 8000 | Web + REST 管理端（lifespan：init_logging→db→redis→saq） |
| backend worker | 规划中（骨架无） | 8100 | 管控节点（GPU 机器，**不连 PG**，用 `DISABLED_EXTENSIONS=db`） |
| backend SAQ worker | `backend/app/work_saq.py` | — | `python app/work_saq.py -q default` |
| frontend SPA | `frontend/` | 5178(dev) | 管理页面，访问路径 `/vllm_manager/frontend/` |

## 技术栈

- **backend**：Python 3.11，Poetry（in-project venv，`backend/.venv/`）；FastAPI + uvicorn；SQLAlchemy 2.0 async + asyncpg；redis + saq[hiredis]；uuid-utils（UUID7）；pytest + ruff（dev）
- **frontend**：React 19 + TypeScript + Vite + Tailwind + shadcn/ui，bun 构建链；`vite.config.ts` base=`/vllm_manager/frontend/`，dev proxy `/vllm_manager/*→8000`

## 目录结构

```
vllm_manager/
├── backend/
│   ├── pyproject.toml / poetry.toml   # Poetry（in-project）
│   ├── .env.example / .env            # 配置（.env 不入库）
│   ├── app/
│   │   ├── config.py                  # AppConfig 单例（pydantic-settings frozen，读 .env）
│   │   ├── exception.py               # 分层异常体系（业务异常在此定义）
│   │   ├── main/                      # 入口包：app 单例/lifespan/routers(api/v1 聚合)/web_routers/static_files(SafeStaticFiles)
│   │   ├── work_saq.py                # SAQ worker 入口（argparse -q/-c/--check）
│   │   ├── base/                      # base_config/base_crud(泛型)/base_enum/base_model(UUID7 Id + Timestamp)/响应契约(BaseResponse/ListResponse)
│   │   ├── extensions/                # database/redis/saq/logging 单例（全部懒加载，import 零连接）
│   │   └── utils/
│   └── tests/                         # 样板契约测试（pytest，16 passed）
├── frontend/
│   ├── package.json / vite.config.ts  # bun；base=/vllm_manager/frontend/
│   └── src/
│       ├── App.tsx / pages/           # 样板示例页（首页/登录/404，懒加载路由）
│       ├── components/                # shadcn/ui + AppLayout
│       ├── lib/api/                   # axios 封装（/vllm_manager/api/v1 + message 透出）
│       └── contexts/UserContext.tsx   # 登录态（后端 501 时自动放行）
├── docs/                              # 架构与借鉴文档
├── docker-compose.yml                 # PG + Redis（本机已有可跳过）
└── AGENTS.md
```

## 常用命令

```bash
# backend（工作目录 backend/）
poetry install
poetry run uvicorn app.main:app --port 8000        # server 入口（需 PG + Redis）
poetry run python app/work_saq.py -q default       # SAQ worker
poetry run pytest                                  # 样板契约测试
poetry run ruff check app                          # lint

# frontend（工作目录 frontend/）
bun install && bun run dev       # 开发 :5178
bun run build                    # 构建（tsc + vite）
```

## 后端开发约束

- **分层依赖单向**：业务域 → `base/` → `extensions/`（业务不反向依赖；`extensions/` 内部互相独立）。
- **路径前缀**：`BASE_URL_PATH`（`app_config.BASE_URL_PATH` 默认 `/vllm_manager`）为所有 REST 端点与前端静态资源前缀 → `/vllm_manager/api/v1/...`、SPA 挂 `/vllm_manager/frontend/`。
- **统一响应**：接口返回 `BaseResponse{code, message, data}`（列表用 `ListResponse`）；业务失败**抛 `app.exception` 分层异常**（全局 handler 渲染），不要自行拼错误 JSON。
- **配置 frozen**：`app_config` 为 `frozen=True`，运行时不可写；各扩展（database/redis/saq）有独立 Config 子类，读 `.env` 的 `DB_*`/`REDIS_*` 分组。
- **懒加载零副作用**：`extensions/` 的 engine/redis/queue 首次调用才创建，import 阶段零连接；lifespan 按 `DISABLED_EXTENSIONS`（逗号分隔 db/redis/saq）控制初始化。
- **数据模型**：用 `base/base_model.py` 的 Mixin（UUID7 Id + Timestamp），列类型用 `UniversalJSON`（PG 落 JSONB）；表名遵循 `TABLE_NAME_PREFIX` 约定（`.env` 已配 `vllm_manager`）。
- **GPU 监控必须优雅降级**：本开发机无 NVIDIA GPU，pynvml 不可用或无 GPU 时返回空列表并告警一次，绝不抛异常。

### 命名与代码结构规范（参考 synapse-agent）

**业务域包组织**：两级结构——业务域包（如 `app/server/`、`app/worker/`）→ 子域包（如 `app/server/node/`、`instance/`、`allocator/`）→ 职责文件。子域按业务聚合边界拆分（node=基础设施存在 / instance=工作负载生命周期 / allocator=分配决策），**子域间依赖单向：仅允许只读引用他域的 model/schema，禁止跨域 service 互调**（allocator 只读 node+instance，产出纯数据决策）。子域内部按职责拆文件，不做大单体模块：
- `model.py`（ORM 模型）/ `schema.py`（Pydantic 请求/响应）/ `service.py`（业务逻辑，泛型 `BaseCrudService[T]` 基类）
- `api.py`（公共 API，挂 `/vllm_manager/api/v1`）/ `web_api.py`（前端专用 API，挂 `/vllm_manager/web_api`）
- `dependencies.py`（FastAPI 依赖注入）/ `enum.py`（业务枚举）/ `tasks.py`（SAQ 任务）
- 横切文件（web_api/tasks 等）按子域归属，跨域路由聚合在 `app/main/routers.py` / `web_routers.py` 层，**不为二期预留空占位文件**（按需新建）

**模型命名**：
- 表类：单数驼峰，`class User(IdMixin, BaseModel, table=True)`；`__tablename__ = f"{db_config.TABLE_NAME_PREFIX}_user"`
- 字段：蛇形命名，多词带领域前缀（`user_id`/`user_name`）；每个字段 `Field(..., description="中文说明")`；类型/长度用 `sa_column=Column(...)` 显式声明
- 表注释与索引：`__table_args__` 里写 `{"comment": "中文表注释"}` + `Index("idx_xxx", ...)`

**Schema 命名**：
- 创建请求 `XxxCreateSchema`、更新请求 `XxxUpdateSchema`、操作请求 `XxxRequest`（如 `LoginRequest`）、响应 `XxxSchema`/`XxxOut`
- 所有字段 `Field(..., description="中文说明")`；响应 Schema 统一 `BaseResponse[XxxSchema]` 包裹

**Service 命名**：`class XxxService(BaseCrudService[XxxModel])`；方法语义化动词（`get_by_username`/`authenticate`/`paginate`/`delete_with_relations`）；**Service/CRUD 默认不提交事务**（`commit=False`），由 API/路由层统一提交（样板 `get_session` 依赖自动 commit/rollback）。

**路由组织**：
- `APIRouter(prefix="/xxx", tags=["中文标签"])`；端点函数 `async def` + `Depends(get_session)`/业务依赖
- 公共 API 在 `app/main/routers.py` 聚合（`/vllm_manager/api/v1`），前端专用 API 在 `app/main/web_routers.py` 聚合（`/vllm_manager/web_api`）
- 接口返回 `BaseResponse[...]`，业务失败抛 `app.exception` 分层异常

**分层与跨层约束**：
- 调用链单向：`API → Service → CRUD → Model`；跨层传递用 Pydantic Schema，**禁止把 ORM 实例直接透给前端**
- `async` 函数中不要直接跑 CPU 密集/同步阻塞逻辑，用 `app.utils.concurrency.make_async` 包装
- 新增 Model/Service/API/Test 前，先读 `backend/tests/` 样板契约测试的写法，保持风格一致

## 后端验证

- `poetry run pytest` 全绿（新增业务须补契约测试，`backend/tests/` 仿照样板）
- `poetry run ruff check app` 通过；pyright basic 模式不阻塞但保持干净
- 本机 PG（127.0.0.1:5432/postgres/123456/vllm_manager）与 Redis（127.0.0.1:6379/123456）已就绪，`uvicorn` 启动 + curl 冒烟验证

## 前端开发约束

- 遵循样板：Tailwind + shadcn/ui 组件、AppLayout、懒加载路由；文案中文。
- API 调用走 `lib/api/`（axios 封装），路径带 `/vllm_manager/api/v1` 前缀；错误 message 由拦截器透出。
- 登录态沿用样板（`UserContext` 501 自动放行）；新增业务页注册到 `App.tsx` 路由 + 侧栏导航。
- `bun run build`（tsc + vite）通过再提交。

### 命名与结构规范（参考 synapse-agent）

- **页面**：`src/pages/<域>Page.tsx`（如 `NodesPage`/`InstancesPage`）；路由懒加载注册到 `App.tsx`
- **组件**：`src/components/` 按用途分：`ui/`（shadcn 生成件）、`layout/`（AppLayout）、业务组件（如 `CreateInstanceDialog`）用 PascalCase
- **API 层**：`src/lib/api/` 按业务域分文件（如 `vllm.ts` 封装节点/实例端点），类型放 `src/types/`（与后端 Schema 对齐）
- **类型**：后端响应 `BaseResponse{code, message, data}` 与前端 `types/api.ts` 对齐；业务类型独立文件（如 `types/vllm.ts`）

## 文档与样例

- 接口/字段契约改动需同步本文件、`README.md` 与前端 `lib/api`。
- `docs/` 现有 `gpustack-borrowings.md`（借鉴调研），后续业务设计文档（如 allocator、worker 契约）也放这里。

## Cloned Dependency Source

Read-only dependency source repositories are available under
`.slim/clonedeps/repos/` for inspection. Do not edit these clones.

- `.slim/clonedeps/repos/gpustack__gpustack/` — gpustack/gpustack at v2.2.3; GPU 集群管理参考实现，worker 端 vLLM 实例启停（serve_manager.py、backends/vllm.py）、GPU 监控与指标采集对标本项目的 worker 端。
  - 仓库导航：先读该仓库根 `codemap.md`（Repository Atlas，含目录职责总表与阅读顺序），深入某目录前读对应 `codemap.md`（如 `gpustack/worker/codemap.md`）。
