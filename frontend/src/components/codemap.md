# frontend/src/components/

## Responsibility（职责）

业务组件层：路由守卫、全局错误边界、页面布局骨架与 shadcn/ui 生成组件库。不含页面级业务逻辑（页面在 `src/pages/`）。

## Design（设计模式与抽象）

- **`ProtectedRoute.tsx`**：路由守卫组件——`useUser()` 取登录态；`loading` 时渲染全屏 spinner；未登录 `<Navigate to="/login" state={{ from: location }} replace>` 携带来源地址；已登录渲染 `<Outlet />`。以路由元素身份（`<Route element={<ProtectedRoute />}>`）包裹受保护子路由。
- **`ErrorBoundary.tsx`**：class 组件，`getDerivedStateFromError` 捕获渲染期异常 → 兜底错误页（含 message + 刷新按钮），避免白屏；`componentDidCatch` 打日志。
- **`layout/AppLayout.tsx`**：受保护页面布局容器（侧栏导航 + 顶栏 + 内容 Outlet），详情见 `layout/codemap.md`。
- **`ui/`**：shadcn/ui 生成组件（Radix primitives 包装 + cva 变体 + Tailwind），**非手写业务代码，勿改**，详见 `ui/codemap.md`。

## Flow（数据与控制流）

路由层（`App.tsx`）→ `ErrorBoundary`（最外层兜底）→ `ProtectedRoute`（登录态门禁）→ `AppLayout`（布局 + `<Outlet />` 渲染子路由页面）；页面内使用 `ui/` 组件组装 UI。

## Integration（集成点）

- 被 `App.tsx` 直接引用：`ErrorBoundary`、`ProtectedRoute`、`AppLayout`。
- `ProtectedRoute`/`AppLayout` 依赖 `contexts/UserContext`（`useUser`）的 `user/loading/logout`。
- `AppLayout` 与 `ui/` 组件依赖 `lib/utils` 的 `cn` 与主题变量（sidebar 色板）。
