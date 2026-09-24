# gpustack/api/

## Responsibility
API 基础设施层（横切关注点，非业务 Controller）：

- 统一认证依赖（`auth.py`）、多租户上下文解析（`tenant.py`）、
  异常体系与错误响应格式（`exceptions.py`）、
  流式响应包装（`responses.py`）、请求中间件（`middlewares.py`）。

被 `gpustack/routes/` 所有模块依赖。

## Design
- **认证（`auth.py`）**：
  - `get_current_user` 支持四种凭证（Basic / Bearer / `X-API-Key` 头 / session Cookie），
    经 `authenticate_request` 统一解析，结果缓存于 `request.state.user`。
  - `get_admin_user`、`get_worker_principal`、`get_cluster_principal`
    （SYSTEM principal 校验，`is_server_token_principal` 兼容 legacy server token）。
  - `management_scope`/`inference_scope` 基于 API key 的 `PermissionScope`。
  - `worker_auth` 供 worker 端点校验（含经 server `/token-auth` 回查）；
    `BearerTokenAuthenticator` 供 WebSocket 代理使用。
- **多租户（`tenant.py`）**：
  - `get_tenant_context` 解析出 `TenantContext`（user、is_platform_admin、
    current_principal_id、org_role、accessible_cluster_ids、scoped_cluster_id 等），
    缓存于 `request.state.tenant_context`，一次请求只解析一次。
  - 核心可见性函数：`bypass_tenant_filter`（SYSTEM principal 与管理员跨租户豁免）、
    `cluster_scoped_system`（集群绑定的 SYSTEM 账号收窄到本集群）、
    `tenant_list_conditions`（列表 SQL 过滤）、`assert_resource_visible`
    （单资源 404 防探测）、`assert_org_owned_writable`（写权限门禁）、
    `require_org_role`（角色依赖工厂）。
- **异常（`exceptions.py`）**：
  - `http_exception_factory` 动态生成 `AlreadyExistsException`/`NotFoundException`/
    `ForbiddenException`/`InvalidException` 等。
  - 统一响应体 `ErrorResponse{code, reason, message}`；
    `OpenAIAPIException` 输出 OpenAI 格式错误。
  - `register_handlers(app)` 注册全局 handler（含 `RequestValidationError` → 422）。
- **响应（`responses.py`）**：`StreamingResponseWithStatusCode`
  允许生成器首个 chunk 决定 HTTP 状态码（用于流式中途失败返回 500/503）。
- **中间件（`middlewares.py`）**：
  - `RequestTimeMiddleware`（记录 start_time）。
  - `ModelUsageMiddleware`（拦截 `/v1*/chat/completions` 等推理端点，
    解析 OpenAI 响应提取 usage → `record_model_usage`，
    含 SSE 分块解析与速率指标注入）。
  - `RefreshTokenMiddleware`（JWT 临近过期自动续期 cookie）。

## Flow
HTTP 请求 → 中间件链（RequestTime → ModelUsage → RefreshToken）
→ 路由依赖 `get_current_user`（认证）→ `get_tenant_context`（租户解析）
→ 业务 handler
→ 抛异常时由 `register_handlers` 统一序列化为 `ErrorResponse`/OpenAI 错误体。

## Integration
- 依赖方：`gpustack/routes/*`（全部路由 import 认证与租户工具）、
  `gpustack/schemas/common.py`（引用 `InvalidException`）、
  `gpustack/schemas/principals.py`（`OrgRole`）、`gpustack/server/services.py`
  （`UserService`/`APIKeyService`）、`gpustack/security.py`（JWT/密钥哈希）、
  `gpustack/server/metrics_collector.py`（usage 聚合）。
- 被依赖方：`gpustack/server/app.py` 注册 middleware 与异常 handlers；
  `routes/routes.py` 中的依赖注入。
- 对外契约：错误响应统一为 `{code, reason, message}`；认证支持
  `Authorization: Bearer <api_key>`、Basic（`system/` 前缀用户）、
  `X-API-Key`、session cookie 四种方式。
  对 manager↔agent 契约可参考其"SYSTEM principal + 长期 API key"的机器身份设计。
