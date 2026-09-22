# vllm_manager frontend

前后端分离样板工程的前端骨架（占位符命名为 `vllm_manager`）。只包含可运行的工程骨架与基础 UI 组件，不含业务页面。

## 常用命令

```bash
bun install          # 安装依赖
bun run dev          # 启动 Vite dev server（端口 5178）
bun run build        # 类型检查 + 生产构建
bun run lint         # ESLint 检查
bunx vitest run      # 运行测试
```

## 约定

- 路由 base 路径：`/vllm_manager/frontend/`
- 后端 API 前缀：`/vllm_manager/api/v1`（公共）、`/vllm_manager/web_api`（内部）
- 代码导入一律使用 `@/` 别名（指向 `src/`）
