# frontend/src/pages/

## Responsibility（职责）

页面层：路由懒加载的目标模块。当前为三个骨架页——Home（首页）、Login（登录）、NotFoundPage（404），业务页面（节点 GPU 视图、实例管理）后续在此新增。

## Design（设计模式与抽象）

- **懒加载约定**：每个页面文件 `export default` 组件，由 `App.tsx` 以 `lazy(() => import('@/pages/X'))` 注册，配合顶层 `Suspense`（fallback 转圈）按需加载 chunk。
- **`Home.tsx`**：受保护首页——`useUser()` 展示欢迎语，Card 布局说明骨架用途与开发指引（新建页面→注册路由→复用 ui/ 组件）。
- **`Login.tsx`**：公开页——Card 表单（Input/Label/Button + sonner toast 报错）；`login()` 成功后导航目标优先级：`?redirect=` 参数（**防开放重定向**：以 `http` 开头一律忽略）> `location.state.from`（ProtectedRoute 携带的来源）> `/`；`navigate(target, { replace: true })`。
- **`NotFoundPage.tsx`**：404 兜底——`*` 通配路由，`<Link to="/">` 回首页。

## Flow（数据与控制流）

URL 命中 → 懒加载对应页面 chunk → 页面挂载；Login 提交表单 → `useUser().login()`（内部走 `lib/api/client`）→ 成功跳转；错误经 axios 拦截器透出的 message 显示 toast。

## Integration（集成点）

- **注册**：`App.tsx` `lazy` import + `Routes`（/login 公开；/ 在 ProtectedRoute→AppLayout 嵌套下；* 兜底 404）。
- **依赖**：`components/ui/*`（Card/Button/Input/Label）、`contexts/UserContext`（login/user）、`lib/api/client`（间接）、sonner（toast）。
- **登录回跳**：依赖 `lib/api/client` 401 拦截器生成的 `?redirect=` 与 ProtectedRoute 的 `state.from`。
