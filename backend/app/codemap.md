# backend/app/

## Responsibility
后端业务代码根包（分层中的「业务 + 基座 + 扩展 + 装配」聚合层）：同时承载 server 管理端（:8000，node/instance/allocator 业务域）、worker 管控节点（:8100 独立进程）与全部基础设施。所有 import 均以 `app.` 为根，入口脚本与配置单例都直接挂在此层。

## Design
- **分层依赖单向**：业务域（`server/`、`worker/`）→ `base/`（样板基座）→ `extensions/`（懒加载外部扩展）；`utils/` 为无状态纯工具被各层复用。业务不反向依赖扩展实现。
- **入口脚本**：`work_saq.py`（SAQ worker，argparse -q/-c/--check）、`work_worker.py`（worker 管控节点，--host/--check，端口取 `worker_config.WORKER_LISTEN_PORT`，默认 0.0.0.0:8100——server 跨机回连硬约束）；两脚本均在启动时把项目根插入 sys.path。
- **配置单例**：`config.py` 的 `app_config`（AppConfig，frozen pydantic-settings，读 `.env`）为 server 侧唯一配置入口；worker 侧用 `worker/config.py` 的 `WorkerConfig`，两者互不 import（避免耦合）。
- **分层异常体系**：`exception.py` 定义根类 `VllmManagerException`（message/code/details + to_dict）→ `HttpException`（按 HTTP 状态码细分 400/401/403/404/409/422/429/500/503）、`BusinessException`（资源不存在/重复/无效状态/版本冲突/操作不允许）、`ExternalServiceException`（DB/Redis/LLM/HTTP 客户端）；worker 独立 JSON-only 异常处理器（`worker/exceptions.py`）。
- **路径前缀**：所有 REST 端点与前端静态资源统一挂 `BASE_URL_PATH`（默认 `/vllm_manager`）。

## Flow
- 请求进入：`app/main/__init__.py` 的 app 单例 → 中间件（CORS/Session/APIRequestID）→ lifespan 初始化（日志→DB→Redis→SAQ→prober/reconcile 后台任务）→ 路由（`main/routers.py` 聚合 `/api/v1`、`main/web_routers.py` 聚合 `/web_api`）→ 业务域 API → Service → base CRUD → extensions 会话。
- 业务失败统一抛 `app.exception` 分层异常，由 `main/exceptions.py` 全局 handler 渲染成 `BaseResponse` 结构（非 API 请求回 HTML、SPA 深路由回 index.html）。

## Integration
- 子 map 索引：
  - `server/`：server 管理端业务域（node=节点/实例=工作负载/allocator=分配决策），见 `server/codemap.md`。
  - `worker/`：:8100 管控节点（ServerClient 注册/心跳/对账、lifecycle 启停、collector 采集），见 `worker/codemap.md`。
  - `base/`：模型/CRUD/枚举/响应/请求样板基座，见 `base/codemap.md`。
  - `extensions/`：database/redis/saq/logging 懒加载扩展，见 `extensions/codemap.md`。
  - `main/`：FastAPI 应用装配（app/lifespan/routers/exceptions/静态文件），见 `main/codemap.md`。
  - `utils/`：并发/格式化/worker 转发工具，见 `utils/codemap.md`。
- 配置：`app_config` 承载全部 server 运行参数（含 NODE_*/INSTANCE_*/WORKER_DEFAULT_PORT 等业务配置）；`DISABLED_EXTENSIONS`（db/redis/saq）控制 lifespan 初始化。

## 导航提示
- 入口脚本：`work_worker.py`（:8100）、`work_saq.py`（队列任务）、`main/__init__.py` 的 `app`（:8000 server）。
- 基础设施改动先读 `base/`/`extensions/` 两个 codemap；装配与路由聚合读 `main/codemap.md`；业务域新增子域参照 `server/node/` 的 model/schema/service/api 结构。
