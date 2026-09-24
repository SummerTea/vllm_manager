# frontend/src/contexts/

## Responsibility（职责）

全局状态管理层（React Context）。当前唯一成员 `UserContext.tsx`：登录态（当前用户 + loading + login/logout/refresh）的提供与消费。

## Design（设计模式与抽象）

- **`UserProvider`**（Provider 组件）：内部 `useState` 持有 `user: User | null` 与 `loading`（初始 `true`——挂载即处于"加载中"，避免 effect 内同步 setState 触发 hooks lint）；挂载时 `useEffect` 调 `refreshUser()` 拉取用户信息。
- **`User` 接口**：`{ id, username, display_name? }`，与后端 account 接口 data 对齐。
- **`useUser()` 自定义 Hook**：`useContext(UserContext)` + 空值守卫（必须在 `<UserProvider>` 内使用，否则抛错）；Provider value 用 `useMemo` 稳定引用。
- **后端契约约定**（样板后端尚未实现时可按注释临时脱离后端运行）：
  - `POST /vllm_manager/web_api/account/login` `{ username, password }` → `data.user`
  - `POST /vllm_manager/web_api/account/logout`
  - `GET /vllm_manager/web_api/account/profile` → `data.user`（失败静默置 null，`finally` 关闭 loading）

## Flow（数据与控制流）

`App.tsx` 挂载 `<UserProvider>`（Router 内）→ 挂载即 `refreshUser()`（loading→false）→ `ProtectedRoute` 消费 `{ user, loading }` 决定放行/重定向；Login 页调 `login()` → 成功 `setUser` + `navigate('/')`；AppLayout 顶栏调 `logout()` → 清空 user + 跳登录页。状态变更经 Context 下发，页面无需感知后端细节。

## Integration（集成点）

- **上游**：`App.tsx` 提供者；`lib/api/client`（apiGet/apiPost）为唯一 HTTP 通道。
- **下游消费**：`ProtectedRoute`（user/loading）、`AppLayout`（user/logout）、`pages/Home`（user）、`pages/Login`（login）。
- **路由联动**：login 成功后 `navigate('/')`（`useNavigate`，故 Provider 须在 BrowserRouter 内）。
