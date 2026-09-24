# backend/app/utils/

## Responsibility
无状态纯工具层（基础设施工具）：为业务域提供 **server→worker 指令统一转发**能力（`request_to_worker`，当前层唯一实现）。不含任何全局状态或业务逻辑，被 server 域按需直接 import。`__init__.py` 仅声明工具模块（docstring），不做 re-export——`request_to_worker` 按模块路径显式导入。

## Design
- **worker 统一转发**（request_to_worker.py，instance 域专用）：
  - `build_worker_url(node, path)`：按 node 字段拼 `http://host:port/path`——`advertise_address` 含 `":"` 原样使用、纯 host 补 `worker_port`、未配置时 `ip:worker_port`（与 `node/prober.py` 探测规则一致）。
  - `request_to_worker(node, method, path, *, json, params, timeout=None)`：注入 `Authorization: Bearer {node.token}`，默认超时 `app_config.INSTANCE_REQUEST_TIMEOUT`。
  - **异常映射**（httpx → 分层业务异常）：非 2xx `HTTPStatusError` → `HttpClientException`（带 url/status_code）；网络/超时等 `HTTPError` → `ExternalServiceException(service_name="worker")`；成功返回已 `raise_for_status` 的响应。

## Flow
- 转发调用序列：instance 域（`service/creation.py`、`service/instance.py`）→ `request_to_worker(node, "post", "instances/{id}/start", json=...)` → 构建 URL + Bearer → httpx.AsyncClient 请求（超时兜底）→ 2xx 返回 / 非 2xx 抛 `HttpClientException` / 传输错误抛 `ExternalServiceException` → 由 `main/exceptions.py` 全局 handler 渲染为统一错误响应。

## Integration
- 消费者：`server/instance/` 域（实例启停/权重指令转发）。
- 依赖：`app.config.app_config`（超时配置）、`app.exception`（HttpClientException/ExternalServiceException）、httpx。
- 边界：本层不依赖 base/extensions/server/worker 任何实现，保持零副作用可单测。

## 导航提示
- 当前文件清单：`__init__.py`（仅模块 docstring，无 re-export）、`request_to_worker.py`（build_worker_url/request_to_worker，含 Bearer 与超时、异常映射）。
- 已移除文件：`reconciler.py`、`concurrency.py`、`formatters.py` 已从本层删除（职责归入各自业务域），不再描述。
- 新增跨域通用纯函数放本层，按模块路径显式导入；仅业务域内部使用的助手放各自域内。
