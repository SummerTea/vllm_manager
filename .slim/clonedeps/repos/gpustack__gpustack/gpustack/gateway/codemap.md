# gpustack/gateway/

## Responsibility
Higress（K8s Ingress 网关）集成层：负责把 GPUStack 自身的 AI 推理路由（OpenAI/Anthropic 协议、模型代理）映射到 Higress 的 K8s 资源上，并在网关侧装配鉴权、指标统计、模型路由等 Wasm 插件链。vllm_manager 目前不涉及网关领域，仅作参考。

## Design
- `initialize_gateway(cfg, timeout, interval)` 是唯一入口（`cmd/start.py` 启动时调用）：先 `init_async_k8s_config` 建立异步 k8s client（incluster / kubeconfig 两种模式），`wait_for_apiserver_ready` 就绪后按 `prepare()` 装配全部资源。
- 资源装配分四类，全部为幂等 ensure/create-or-update：
  - `ensure_tls_secret`：SSL 证书写入 `kubernetes.io/tls` Secret；
  - `ensure_mcp_resources`：维护 default `McpBridge`（Higress 的服务发现注册表，`McpBridgeRegistry` 按 `GatewayModeEnum` 生成 dns/static 注册项）；
  - `ensure_ingress_resources`：`GATEWAY_MIRROR_INGRESS_NAME` Ingress 将根路径路由到 mcpbridge（仅 embedded/external 模式），用 `managed_labels`（`gpustack.ai/managed`）甄别可接管资源；
  - `ensure_gateway_timeout`：改 `higress-config` ConfigMap 的下行/上行 idleTimeout。
- Wasm 插件链（每个插件一个 `(resource_name, WasmPluginSpec)` 工厂，经 `ensure_wasm_plugin` 应用）：`ext_auth_plugin`（forward_auth 到 `/token-auth`，鉴权失败 FAIL_OPEN）、`ai_statistics_plugin`、`generic_proxy_router_plugin`（替代 Higress 内置 model-router，path/body 双模式解析模型名写入 `x-higress-llm-model`，aliasNameMapping 由控制器增量维护）、`ai_proxy_plugin`、`model_pre_route_plugin`、`model_mapper_plugin`、`transformer_plugin`（header 增删改映射）、`token_usage_plugin`。插件 URL 经 `plugins.py` 从 `gpustack_higress_plugins` 的 manifest.json 解析。
- `utils.py`（60KB）是核心工具库：命名约定（`RoutePrefix`、`model_route_ingress_name`、`cluster_worker_prefix` 等）、注册表/代理构造（`cluster_registry`、`provider_registry`、`model_instance_registry`、`worker_reserve/tunnel_proxy_registry`）、diff 与清理（`diff_registries`、`cleanup_ai_proxy_config`、`cleanup_generic_proxy_router`、`cleanup_ingresses`、`cleanup_fallback_filters`）、负载权重计算（`hamilton_calculate_weight`）以及 `worker_websocket_connect_callback`（worker 隧道接入回调）。
- `ai_proxy_types.py`：ai-proxy 插件的 `AIProxyDefaultConfig`/`CustomConfig`/`FailoverConfig` 等 Pydantic 配置模型。

## Flow
配置（`Config.gateway_mode`/`gateway_namespace`）→ `initialize_gateway` → 异步 k8s client → 依次 ensure Secret/McpBridge/Ingress/ConfigMap/WasmPlugin；运行时由 `server/controllers.py`（模型路由控制器）调用 `utils.ensure_model_ingress`/`ensure_model_mcp_bridge`/`ensure_wasm_plugin` 维护按模型实例的动态路由。

## Integration
- 调用方：`cmd/start.py`（启动装配）、`server/controllers.py`、`server/server.py`、`worker/worker.py`（仅 init k8s config）、`routes/openai.py`、`routes/worker/proxy.py`（读 header/工具函数）。
- 依赖：`gateway/client/`（CRD client）、`kubernetes_asyncio`、`gpustack_higress_plugins`（Wasm 插件分发）、`gpustack.schemas.config.GatewayModeEnum`、`gpustack.api.auth.GATEWAY_AUTH_TOKEN_HEADER`、`security.AUTH_CACHE_HEADER`。
- 被 `cmd/codemap.md` 归为 server 启动阶段的网关装配步骤。
