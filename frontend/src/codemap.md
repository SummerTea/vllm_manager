# frontend/src/

## Responsibility（职责）

前端应用源码根：React 挂载入口、路由注册、全局样式与主题变量，以及各功能子目录（components/contexts/lib/pages/types）的聚合层。

## Design（设计模式与抽象）

- **挂载入口 `main.tsx`**：`ReactDOM.createRoot` + `React.StrictMode` 渲染 `<App />`；`document.title` 由 `VITE_APP_NAME` 覆盖。
- **路由注册 `App.tsx`**：三层嵌套——`ErrorBoundary`（全局渲染异常兜底）→ `BrowserRouter basename="/vllm_manager/frontend/"` → `UserProvider`（登录态）；内部 `Suspense`（fallback `PageLoading` 转圈）+ `Routes`。
- **懒加载路由**：`lazy(() => import('@/pages/...'))` 加载 Login/Home/NotFound，首屏只下载当前页面 chunk。
- **路由守卫布局**：`/login` 公开；其余路由包在 `ProtectedRoute`（未登录重定向）→ `AppLayout`（侧栏+顶栏+Outlet）嵌套下，`*` 通配到 NotFoundPage。
- **全局 UI**：`Toaster`（sonner toast，top-center + richColors）常驻；`index.css` 定义 Tailwind 三层指令 + CSS 变量主题（`:root` 亮色 / `.dark` 暗色，含 sidebar 全套色板）+ accordion/overlay 动画 keyframes。
- **测试**：`App.test.tsx`（vitest + testing-library）+ `test-setup.ts`（jest-dom matchers），被 vite.config test.include 收集。

## Flow（数据与控制流）

`index.html` → `main.tsx` 挂载 → `App.tsx` 组装 ErrorBoundary/Router/UserProvider/Suspense → 按 URL 懒加载页面 chunk → ProtectedRoute 校验登录态 → AppLayout 渲染 Outlet 内容。

## Integration（集成点）

- **子目录**：`components/`（布局/守卫/UI 组件）、`contexts/`（UserContext）、`lib/`（工具 + API 客户端）、`pages/`（懒加载页面）、`types/`（后端契约类型）——各自见对应 codemap.md。
- **主题**：`tailwind.config.js` 将 CSS 变量映射为 Tailwind 色板（background/foreground/sidebar 等）；`darkMode: 'class'`，AppLayout 切换 `<html class="dark">`。
- **后端**：Router basename 与 vite `base` 一致（`/vllm_manager/frontend/`），BrowserRouter 需后端回退到 index.html（SPA 路由）。
