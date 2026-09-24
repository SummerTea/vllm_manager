# frontend/src/components/layout/

## Responsibility（职责）

布局组件层。当前唯一成员 `AppLayout.tsx`：受保护页面的全局布局骨架（侧栏导航 + 顶栏 + 内容区）。

## Design（设计模式与抽象）

- **`AppLayout.tsx`**：函数组件，组合 `NavLink`/`Outlet`/`useNavigate`（react-router）+ `lucide-react` 图标 + `ui/button` + `useUser()`。
  - **侧栏**：固定 `w-56`，品牌标题 + `navItems` 数组驱动导航（`{ to, label, icon }`，当前首页 `/` 与设置 `/settings`）；`NavLink` 的 `isActive` 回调 + `cn` 实现激活态高亮。
  - **顶栏**：显示当前用户（`user.display_name ?? user.username`）+ 主题切换（`document.documentElement.classList.toggle('dark')`，配合 `darkMode: 'class'`）+ 退出登录（`logout()` 后 `navigate('/login')`）。
  - **内容区**：`<main>` 内 `<Outlet />` 渲染嵌套子路由页面。
- **布局即嵌套路由**：AppLayout 作为 `<Route element={<AppLayout />}>` 的布局路由，子路由通过 Outlet 填充，切换页面不重载外壳。

## Flow（数据与控制流）

路由命中受保护路径 → AppLayout 渲染外壳 → `<Outlet />` 挂载子路由页面（Home/NotFound）→ 顶栏事件（主题/退出）操作全局状态与路由。

## Integration（集成点）

- 被 `App.tsx` 引用为布局路由（位于 `ProtectedRoute` 之内，保证只有登录态进入）。
- 依赖：`contexts/UserContext`（useUser：user/logout）、`lib/utils`（cn）、`components/ui/button`、主题 CSS 变量（sidebar 色板）、Tailwind 原子类。
- 新增业务页面只需：`pages/` 建页面 + `App.tsx` 注册路由 + 此处 `navItems` 加导航项。
