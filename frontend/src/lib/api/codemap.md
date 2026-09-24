# frontend/src/lib/api/

## Responsibility（职责）

HTTP 客户端层：axios 实例的统一封装，所有前端→后端请求的唯一通道。当前为 `client.ts`。

## Design（设计模式与抽象）

- **axios 实例 `apiClient`**：`baseURL: ''`（相对路径，靠 vite proxy 转发）；`timeout: 30000`；JSON 头；`withCredentials: true`（携带 cookie 会话）；`paramsSerializer.indexes: null`（数组参数不带 `[]` 后缀）。
- **响应拦截器（统一错误透出）**：后端失败体为 `BaseResponse{message}`（FastAPI 原生/Hub 直返为 `{detail}`）→ 拦截器把 `message`/`detail` 字符串写入 `error.message`，调用点可直接 `err.message` 拿到后端真实原因，避免只见 "Request failed with status code N"。
- **401 自动跳登录**：非登录页收到 401 → 拼 `redirect` 参数（当前 path/search/hash，去 frontend 前缀）→ `window.location.href` 跳 `/vllm_manager/frontend/login?redirect=...`（登录后回跳，见 Login 页防开放重定向逻辑）。
- **类型化 API 函数**：`apiGet<T>`/`apiPost<T>`/`apiPut<T>`/`apiPatch<T>`/`apiDelete<T>` 返回 `Promise<ApiResponse<T>>`（解包 `response.data`）；`apiDownload` 返回 `Blob`（responseType: 'blob'）；均透传 axios config。
- **契约类型**：`ApiResponse<T>` 对齐后端 `BaseResponse{code, message, data}`（见 `types/api.ts`）。

## Flow（数据与控制流）

页面/Context 调 `apiXxx<T>(url, ...)` → axios 请求（相对路径，dev 走 vite proxy 到 8000）→ 响应拦截器：成功透传、失败透出 message 并处理 401 → 调用点拿到 `ApiResponse<T>` 或 catch `err.message`。

## Integration（集成点）

- **路径前缀**：调用方显式传全路径，如 `/vllm_manager/api/v1/...`（公共）与 `/vllm_manager/web_api/...`（内部，UserContext 使用）；dev 期由 `vite.config.ts` proxy 转发到 `VITE_API_TARGET`（默认 `http://localhost:8000`）。
- **消费者**：`contexts/UserContext`（login/logout/refresh）、后续业务 API 封装均走此处；错误 message 由拦截器透出到页面 toast（Login 页）。
