# gpustack/gateway/client/

## Responsibility
Higress / Istio CRD 的轻量异步客户端层：用 `kubernetes_asyncio` 的 `CustomObjectsApi` 封装 GPUStack 需要打交道的三类自定义资源（McpBridge、WasmPlugin、EnvoyFilter）的增删改查，避免引入完整 client-gen 生成代码。

## Design
- 三个模块各对应一个 K8s group/version，均以 Pydantic `BaseModel` 定义资源结构 + 同名异步 API 类：
  - `networking_higress_io_v1_api.py`：`McpBridge`（networking.higress.io/v1，`McpBridgeSpec`/`McpBridgeRegistry`/`McpBridgeProxy`），`McpBridgeRegistry.get_service_name()` 生成 `{name}.{type}` 服务名；`NetworkingHigressIoV1Api` 提供 create/edit/get/list/patch/delete_mcpbridge。
  - `extensions_higress_io_v1_api.py`：`WasmPlugin`（extensions.higress.io/v1alpha1，`WasmPluginSpec` 含 phase/priority/url/failStrategy/defaultConfig 等字段）；`ExtensionsHigressIoV1Api` 对应 CRUD。
  - `networking_istio_io_v1alpha3_api.py`：`EnvoyFilter`（networking.istio.io/v1alpha3）及其 `EnvoyConfigObjectMatch`/`EnvoyConfigPatchObject` 结构；`NetworkingIstioIoV1Alpha3Api` 对应 CRUD。
- 资源体统一 `model_dump(by_alias=True, exclude_none=True)` 序列化后交给 CustomObjectsApi。
- 另附两个 EnvoyFilter 工厂：`get_4xx_5xx_fallback_value`（构造 4xx/5xx 响应重定向的 typed_per_filter_config）与 `get_ingress_fallback_envoyfilter`（为指定 ingress 路由 MERGE 该 fallback 配置，用于模型路由主备降级）。
- `__init__.py` 集中 re-export 全部资源模型与 API 类。

## Flow
上层（`gateway/__init__.py` 的 `initialize_gateway`、`server/controllers.py` 的模型路由控制器）持有 `k8s_client.ApiClient` → 构造对应 API 类 → 以 Pydantic 模型为 body 调用 CRUD；404 由调用方捕获后视为"尚未创建"走 create 分支。

## Integration
- 消费方：`gpustack/gateway/__init__.py`（`ensure_mcp_resources`、`ensure_wasm_plugin` 等）、`server/controllers.py`（模型路由/fallback EnvoyFilter 维护）、`server/server.py`。
- 依赖：`kubernetes_asyncio`（仅 `client.CustomObjectsApi`）、`pydantic`；无业务逻辑，纯协议层。
