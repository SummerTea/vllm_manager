# backend/app/main/

## Responsibility
FastAPI 应用装配层（server 管理端 :8000 的入口与容器）：集中完成 app 单例构造、中间件、lifespan 生命周期、路由聚合、前端静态文件挂载与全局异常处理。**本层只做装配，不含业务逻辑**——业务集中在 `server/` 域，由本层路由聚合。

## Design
- **app 单例**（`__init__.py`）：模块加载最先 `load_dotenv()`（把 `.env` 写入 os.environ 供子进程继承）；`FastAPI(title/version, lifespan, docs_url=f"{BASE_URL_PATH}/docs")`；`SessionMiddleware`（`SECRET_KEY` 签名、session cookie、生产强制 https_only）。
- **路由三挂载**：`get_api_router()`（公共 API，前缀 `{BASE_URL_PATH}/api/v1`，聚合 `node_router`/`instance_router`，见 `routers.py`）、`get_web_api_router()`（前端专用，前缀 `{BASE_URL_PATH}/web_api`，现含 `/account/*` 501 契约占位供前端 UserContext，见 `web_routers.py`）、SPA 静态挂载 `{BASE_URL_PATH}/frontend`（`SafeStaticFiles`，目录不存在静默）。
- **lifespan**（lifespan.py）：`init_logging` → 按 `DISABLED_EXTENSIONS` 跳过式 `init_db(create_tables=(DEPLOY_ENV==DEVELOPMENT))` → `init_redis` → `init_saq` → 启动 `node.prober.probe_loop` 后台任务（依赖 DB，db 禁用时不启动）；组合根装配 `on_node_lost` 回调：探测失联节点批量交给 `VllmInstanceService.reconcile_lost_nodes` 联动（running→unreachable），跨域装配只发生在此（node 域零跨域 import，回调是参数）；yield 后反向取消任务、关 redis、dispose db、停日志。
- **全局异常处理**（exceptions.py）：
  - `APIRequestIDMiddleware`：仅对 API 路径注入/透传 `X-Request-ID`。
  - 分层 handler：`HttpException`（status_code 直映）→ `BusinessException`（默认 400，`ResourceNotExistException`→404、`DuplicateResourceException`→409）→ 兜底 `VllmManagerException`（500）；另有 `HttpClientException`（worker 转发错误透传：取 `details.status_code` 透传 worker 状态码，worker 401/403 映射 502——worker 鉴权失败不代表 server 登录态失效，防前端误判被踢）、FastAPI `HTTPException`/`RequestValidationError`(422)/`Exception` 兜底。
  - **API vs SPA 双轨**：API 路径回 `BaseResponse` JSON（含 request_id/error_code/details）；非 API 回 HTML；SPA 深路由 404 回 `index.html`（`Cache-Control: no-cache` 防 hash chunk 过期），但静态资源缺失（按 `/assets` 前缀 + 扩展名后缀判定）回 404 JSON，避免 module script 拿到 text/html 白屏。
- **SafeStaticFiles**（static_files.py）：覆写 `check_config` 为空操作 + `get_response` 兜底 404，使前端产物缺失时静默挂载。

## Flow
- 启动：`uvicorn app.main:app` → `__init__.py` 装配 app → lifespan 顺序初始化（见上）→ 后台任务循环常驻 → 收到关闭信号反向清理。
- 请求：中间件 → 路由分发（`/vllm_manager/api/v1/nodes|instances/...`）→ 业务 API（`Depends(get_session)`）→ Service → base CRUD → 响应 `BaseResponse` 返回；业务异常上抛至此层 handler 统一渲染。
- 404 分流：`not_found_handler` 先判 API 路径，再判 SPA 前缀 + 静态资源后缀，最后 HTML 404。

## Integration
- 上游依赖：`app.config.app_config`（BASE_URL_PATH/DISABLED_EXTENSIONS/SECRET_KEY/SESSION_*/FRONTEND_DIST_DIR/DEPLOY_ENV）、`app.extensions`（全部 init/close）、`app.exception` 分层异常、`app.server.node.api`/`app.server.instance.api` 路由、`app.server.node.prober` 后台任务（lifespan 组合根装配 on_node_lost 回调 → `app.server.instance.service` 失联联动）、`app.base.base_response_schema`。
- 被依赖：`work_saq.py` 独立初始化（不挂本 app）；worker 域**禁止 import 本层**（避免拉入 server 配置耦合）。
- 前端集成：`SafeStaticFiles` 挂 `{BASE_URL_PATH}/frontend`，`/` 重定向到该前缀；SPA 深路由回源由异常层兜底。

## 导航提示
- 文件→职责对照：`__init__.py`（app 单例+中间件+挂载）、`lifespan.py`（初始化/清理编排）、`routers.py`（/api/v1 聚合）、`web_routers.py`（/web_api 聚合，含 /account 契约占位）、`exceptions.py`（全局 handler + APIRequestIDMiddleware）、`static_files.py`（SafeStaticFiles）。
- 新增业务域路由在 `routers.py`/`web_routers.py` include；改动响应/异常结构时同时核对 `tests/` 契约测试与前端 `lib/api`。
