# frontend/src/components/ui/

## Responsibility（职责）

shadcn/ui 生成的基础 UI 组件库（shadcn 风格，`components.json` 配置）。**非手写业务代码**：由 `npx shadcn add` 生成，业务上禁止直接改动，缺组件时用同命令补充。

## Design（设计模式与抽象）

- **Radix primitives 包装**：多数组件基于 `@radix-ui/react-*`（dialog/popover/dropdown-menu/select/tabs/tooltip/switch/checkbox/avatar/label/separator/slot），无头逻辑 + 本项目 Tailwind 样式。
- **cva 变体模式**：`button` 用 `class-variance-authority` 定义 `variant`（default/destructive/outline/secondary/ghost/link）与 `size`（default/sm/lg/icon），导出 `buttonVariants` 供 `asChild` 场景复用。
- **`cn` 合并**：全部组件经 `@/lib/utils` 的 `cn`（clsx + tailwind-merge）合并 className。
- **组件清单（17 个）**：avatar、badge、button、card、checkbox、dialog、dropdown-menu、input、label、popover、select、separator、skeleton、sonner（toast 容器，`<Toaster />`）、switch、tabs、tooltip。
- **主题**：颜色全部走 `index.css` CSS 变量（`bg-primary`/`text-muted-foreground` 等），亮暗色自动切换，无硬编码色值。

## Flow（数据与控制流）

业务代码导入组件 → 传入 props/className → 组件内部 `cn(变体 + className)` 组装样式；受控交互（dialog/select 等）由 Radix 内部状态机管理，业务通过 onOpenChange/onValueChange 等回调接收。

## Integration（集成点）

- **生成配置**：`components.json`（style default、baseColor neutral、cssVariables、aliases `@/components`、iconLibrary lucide）。
- **依赖**：`@/lib/utils`（cn）、`tailwind.config.js` 色板映射、`index.css` 变量与动画、`tailwindcss-animate` 插件。
- **被消费方**：`pages/`（Card/Button/Input/Label 等）、`layout/AppLayout`（Button）、`App.tsx`（Toaster）。
