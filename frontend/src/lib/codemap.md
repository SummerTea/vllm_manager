# frontend/src/lib/

## Responsibility（职责）

工具函数层：与业务解耦的通用小工具。当前为 `utils.ts`（className 合并 + Radix 已知问题的修复函数）。

## Design（设计模式与抽象）

- **`cn(...inputs)`**：`twMerge(clsx(inputs))`——clsx 合并条件类名，tailwind-merge 去重冲突类（后写覆盖先写）。shadcn 生态标准工具，被 `ui/` 全部组件与业务组件消费。
- **`clearBodyPointerEventsNoneDeferred()`**：Radix UI 已知 bug 修复——DismissableLayer 在层栈切换（如从 DropdownMenu 开 Dialog 再关）时可能残留 body `pointer-events: none` 导致页面点不动。该函数用双层 `requestAnimationFrame` 延后到 Radix 所有 effect/cleanup 之后清理，且先检查是否还有 `[data-state="open"][role="dialog"/"menu"]` 的 open overlay（避免误清合法锁定）。
- **子目录**：`api/`（axios 客户端封装，见 `api/codemap.md`）。

## Flow（数据与控制流）

无状态纯函数层：被组件 import 后按需调用；`cn` 是渲染期工具，`clearBodyPointerEventsNoneDeferred` 由弹层交互场景在回调中触发。

## Integration（集成点）

- `@/` 别名下 `@/lib/utils` 全项目通用（shadcn components.json 亦将 utils 别名指到这里）。
- 被 `components/ui/*`、`components/layout/AppLayout`、`pages/*` 消费。
