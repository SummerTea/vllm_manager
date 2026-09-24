# frontend/src/types/

## Responsibility（职责）

TypeScript 类型契约层：与后端 Schema 对齐的共享类型定义。当前为 `api.ts`（响应/分页结构），业务类型（节点、实例等）后续按域新增独立文件（如 `types/vllm.ts`）。

## Design（设计模式与抽象）

- **`ApiResponse<T>`**：标准响应包装 `{ code, message, data? }`，与后端 `BaseResponse` 一一对齐——`code` 业务码、`message` 提示（axios 拦截器也用它透出错误）、`data` 泛型负载。所有 API 函数的返回类型。
- **`PageMeta`**：分页元数据 `{ page, page_size, total, total_pages, has_next, has_prev }`，对应后端 `ListResponse` 分页契约。
- **`PageData<T>`**：分页数据包装 `{ items: T[], meta: PageMeta }`，列表接口的通用返回形状。
- **约定**：与后端 Schema 命名/字段对齐（蛇形字段名，如 `page_size`）；响应体统一 `ApiResponse<T>` 包裹，禁止把后端字段私自改名。

## Flow（数据与控制流）

后端 Schema（Pydantic）→ REST JSON → `lib/api/client` 的 `apiXxx<T>` 以 `ApiResponse<T>` 泛型解包 → 页面/Context 消费 `data` 字段；新增业务端点时先在此补类型（或建 `types/<域>.ts`），保证前后端字段同步。

## Integration（集成点）

- 被 `lib/api/client.ts` 引用（`ApiResponse` 泛型约束）；被 `contexts/UserContext` 及后续业务 API 封装/页面消费。
- 后端对应物：`backend/app/base/base_model.py` 的 `BaseResponse`/`ListResponse` 契约（改动需双向同步，见 AGENTS.md 契约约定）。
