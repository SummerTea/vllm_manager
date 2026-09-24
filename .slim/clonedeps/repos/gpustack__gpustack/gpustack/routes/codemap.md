# gpustack/routes/

## Responsibility
REST API 路由层（REST Controllers / HTTP 入口）。

每个资源一个模块文件，定义 `APIRouter` 与 handler，
将 HTTP 请求翻译为对 `gpustack.schemas` 中 SQLModel 表的 CRUD 操作。

`routes/routes.py` 是唯一的装配中心，把所有子路由按认证级别、
租户可见性组装成最终挂载的 `api_router`。

## Design
- **路由装配分层**（`routes/routes.py`）：
  - `api_router`（根）→ `management_router`（`management_scope` 依赖，含全部管理面）。
  - 其下再分 `v1_base_router`（`get_current_user`，常规用户/系统 principal 共用）、
    `worker_client_router`（`get_worker_principal`，worker 回调专用）、
    `cluster_client_router`（`get_cluster_principal`）、`v1_admin_router`（`get_admin_user`）。
  - 注册顺序即匹配优先级——base 在前，worker/cluster 独有的端点自然落到各自 router。
- **版本前缀**：管理面统一挂 `/v2`（`versioned_prefix`）；
  OpenAI 兼容推理面挂 `/v1` 与 `/v1-openai`（`inference_router`，`inference_scope` 依赖）。
- **每资源一文件的 Controller 模式**：如 `models.py`、`model_instances.py`、
  `workers.py`、`gpu_devices.py`、`clusters.py` 等，
  handler 通过 `SessionDep`/`TenantContextDep`/`ListParamsDep` 等 FastAPI 依赖注入上下文。
- **统一响应 DTO**：列表端点 `response_model=XxxsPublic`（`PaginatedList[T]`），
  详情端点 `XxxPublic`；错误统一 `ErrorResponse{code, reason, message}`
  （见 `gpustack/api/exceptions.py`）。
- **watch 模式**：列表端点支持 `?watch=true`，返回 `StreamingResponse`
  （`text/event-stream`），基于 DB 变更事件实时推送（如 `ModelInstance.streaming()`），
  可带 `filter_func` 做租户/集群行级过滤。
- **租户与权限**：handler 内用 `api/tenant.py` 的 `assert_resource_visible`
  （404 防探测）、`tenant_list_conditions`（SQL 过滤）、`assert_org_owned_writable`；
  管理资源批量挂 `_org_owner_only = [Depends(require_org_role(OrgRole.OWNER))]`。

## Flow
请求 → `server/app.py` include 的 `api_router` → 认证依赖（`get_current_user` 等）
→ `get_tenant_context` 解析租户 → 路由 handler
→ `ModelXxx.one_by_id/paginated_by_query/create/update/delete`（`BaseModelMixin`）
→ 返回 Pydantic 模型序列化。

- worker 回调：`POST /v2/workers`（注册，返回 `WorkerRegistrationPublic` 含 token）、
  `POST /v2/worker-heartbeat`、`POST /v2/worker-status`
  （均返回 204，先入 flush buffer 批量落库）。
- 实例日志：`GET /v2/model-instances/{id}/logs` 与 `/log-options` 不直接读文件，
  而是经 `request_to_worker`/`stream_to_worker` 转发到 worker 的
  `serveLogs/{id}`/`serveLogOptions/{id}`。
- 推理：`/v1/chat/completions` 等经 `openai.py` 按模型名解析实例端口后代理到 worker 的 `/proxy`。

## Integration
- 依赖方：`gpustack/schemas`（DTO）、`gpustack/api/auth.py`（认证依赖）、
  `gpustack/api/tenant.py`（租户上下文）、`gpustack/api/exceptions.py`、
  `gpustack/server/services.py`、`gpustack/server/db.py`、`gpustack/server/worker_request.py`。
- 被依赖方：`gpustack/server/app.py` 唯一入口 `app.include_router(api_router)`；
  `routes/routes.py` 还挂载 `websocket_proxy` 消息路由、
  `gateway_metrics`（`/v2/usage/gateway-metrics`）、Higress 插件路由。
- 与 worker 通信：server→worker 方向通过 `server/worker_request.py` 的
  `request_to_worker`/`stream_to_worker`（HTTP 到 `worker.ip:port`），目标端点为
  `gpustack/routes/worker/` 下的 `/serveLogs`、`/serveLogOptions`、`/proxy`、`/files/*`、`/cluster-proxy/*`；
  worker→server 方向为 `POST /v2/worker-heartbeat`、`POST /v2/worker-status`、`POST /v2/workers`。
