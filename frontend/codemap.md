# frontend/

## Responsibility（职责）

vllm_manager 前端工程根：React 19 + TypeScript + Vite + Tailwind + shadcn/ui 的 SPA 管理页面（bun 构建链）。当前为骨架样板（登录/首页/404 + 登录态 + 布局），业务页面（节点 GPU 视图、实例管理）为后续规划。产物挂载于 `/vllm_manager/frontend/`。

## Design（设计模式与抽象）

- **构建链**：`package.json` scripts——`dev`（`vite --port 5178`）、`build`（`tsc -b && vite build`，类型检查先行）、`lint`（eslint）、`test`（`vitest run`，happy-dom 环境）、`preview`。
- **依赖分层**：运行时 = react/react-dom + react-router-dom（v7 路由）+ axios（HTTP）+ lucide-react（图标）+ sonner（toast）；UI = shadcn/ui（Radix primitives + cva 变体 + tailwind-merge）；样式 = tailwindcss 3 + tailwindcss-animate。
- **路径约定**：`base: '/vllm_manager/frontend/'`；导入统一 `@/` 别名（vite alias + tsconfig paths 双配置，指向 `src/`）。
- **环境配置**：`.env`（`VITE_API_TARGET` 后端地址、`VITE_APP_NAME` 应用名），dev 期由 proxy 转发。
- **shadcn 配置**：`components.json` 声明组件生成规格（baseColor neutral、cssVariables、aliases `@/components`、iconLibrary lucide）。

## Flow（数据与控制流）

源码（`src/`）→ vite build 打包 → 静态资源挂到后端 `/vllm_manager/frontend/`；dev 期 vite server（:5178）服务并代理 `/vllm_manager/api`、`/vllm_manager/web_api` → `VITE_API_TARGET`（默认 `http://localhost:8000`）→ 后端 FastAPI。

## Integration（集成点）

- **vite.config.ts**：`base` 路径、dev proxy（`/vllm_manager/web_api` + `/vllm_manager/api` → 8000，changeOrigin + ws）、`@` 别名、vitest 配置（globals/happy-dom/setupFiles）。
- **后端**：公共 API `/vllm_manager/api/v1`、内部 API `/vllm_manager/web_api`（见 `src/lib/api` 与 `src/contexts`）。
- **目录索引**：`src/`（应用源码，见 `src/codemap.md`）；`tailwind.config.js`/`index.css` 承载主题变量。
