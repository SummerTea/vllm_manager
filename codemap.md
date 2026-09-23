# Repository Atlas: vllm_manager

## Project Responsibility

vLLM 推理服务集群管理平台：一个 server 管理端（Web + REST，:8000）通过各 GPU 机器上的 worker 管控节点（:8100）拉起/停止/监控 **vLLM 推理实例**，并按**显存**做分配决策。server 持有权威账本（PostgreSQL），worker 状态全内存经 report 对账收敛，实例生命周期（启动/健康检查/退避重启/失联联动）闭环于两端的契约状态机。

## System Entry Points

| 入口 | 位置 | 端口 | 说明 |
|---|---|---|---|
| backend server | `backend/app/main:app`（uvicorn :8000） | 8000 | Web + REST 管理端；lifespan：init_logging→db→redis→saq→prober 后台任务（probe_loop，回调注入失联联动） |
| backend worker | `backend/app/work_worker.py`（`uvicorn app.worker.main:create_app --factory --host 0.0.0.0 --port 8100`） | 8100 | GPU 机器管控节点；不连 PG，状态全内存，3 后台循环（status/report/sync） |
| backend SAQ worker | `backend/app/work_saq.py`（`python app/work_saq.py -q default`） | — | 异步任务队列 |
| frontend SPA | `frontend/`（bun run dev :5178） | 5178 | 管理页面，访问路径 `/vllm_manager/frontend/` |
| 依赖服务 | `docker-compose.yml` | — | PostgreSQL + Redis |

## Core Architecture Contracts

- **依赖单向**：`node ← instance ← allocator`（server 业务域）；instance 只读引用 node 的 model/schema，禁止 service 互调；allocator 纯函数零 DB IO
- **worker 业务指令只从 instance 域发出**（经 `utils/request_to_worker` 统一 Bearer+超时转发）；node 域仅 /healthz 探测与接收上报
- **对账闭环**：用户操作→server 写 target_state+转发→worker 执行→report 对账（apply_report：M2 守卫/B1 target 保留/stopping 消失收敛/stopped 终态）→清 target
- **契约文档**：`docs/architecture.md`（蓝图层）、`docs/worker-contract.md`（worker 字段级规格）、`docs/gpustack-borrowings.md`（借鉴调研）

## Repository Directory Map

| Directory | Responsibility | Detailed Map |
|---|---|---|
| `backend/` | backend 工程根：双进程架构（server/worker/SAQ）、Poetry 依赖与入口 | [View Map](backend/codemap.md) |
| `backend/app/` | app 层索引：分层异常体系（Http/Business/ExternalService）、AppConfig frozen、入口脚本、六域子 map | [View Map](backend/app/codemap.md) |
| `backend/app/base/` | 样板基座：IdMixin+BaseModel、TypeDecorator（UniversalJSON/UniversalText）、泛型 BaseCrudService[T]、响应契约（BaseResponse/PageResponse）、枚举/配置基类 | [View Map](backend/app/base/codemap.md) |
| `backend/app/extensions/` | 懒加载扩展：database/redis/saq/logging 单例（import 零连接）、get_session 自动 commit/rollback、DISABLED_EXTENSIONS 控制 | [View Map](backend/app/extensions/codemap.md) |
| `backend/app/utils/` | 纯工具层：request_to_worker（server→worker 统一转发，Bearer+超时+异常映射） | [View Map](backend/app/utils/codemap.md) |
| `backend/app/main/` | 应用装配层：app 单例（CORS/Session/load_dotenv）、lifespan 初始化顺序、routers/web_routers 聚合、SafeStaticFiles SPA、全局异常 handler | [View Map](backend/app/main/codemap.md) |
| `backend/app/server/` | server 业务域总览：node←instance←allocator 单向依赖、对账闭环、路由/后台任务挂载 | [View Map](backend/app/server/codemap.md) |
| `backend/app/server/node/` | GPU 节点域：Node.compute_state 派生（pending/ready/offline/unreachable）、幂等注册、/healthz 主动探测、Bearer 鉴权 | [View Map](backend/app/server/node/codemap.md) |
| `backend/app/server/allocator/` | 显存分配决策（纯函数）：first_fit 单卡/多卡 TP、记账口径 available=total−Σallocated−reserved | [View Map](backend/app/server/allocator/codemap.md) |
| `backend/app/server/instance/` | vLLM 实例域：InstanceLifecycleMixin+状态机纯函数、VllmInstance 表、/instances 管理端点 + /report 对账端点、失联联动（reconcile_node_loss/reconcile_lost_nodes，prober 回调驱动） | [View Map](backend/app/server/instance/codemap.md) |
| `backend/app/server/instance/service/` | 实例服务层（目录化）：creation.py 8 步 create 编排（候选→权重广播→allocator→建记录→转发 start）、VllmInstanceService 生命周期方法 | [View Map](backend/app/server/instance/service/codemap.md) |
| `backend/app/worker/` | worker 域：GPU 机器 vLLM 实例生命周期管控节点（内存态状态机+退避重启+restore 仅 running+report 对账闭环） | [View Map](backend/app/worker/codemap.md) |
| `frontend/` | frontend 工程根：bun/react19/vite/tailwind/shadcn 构建链、base=/vllm_manager/frontend/、dev proxy | [View Map](frontend/codemap.md) |
| `frontend/src/` | src 根：main.tsx 挂载、App.tsx 三层嵌套路由（ErrorBoundary→Router→UserProvider→Suspense）、懒加载页面 | [View Map](frontend/src/codemap.md) |
| `frontend/src/components/` | 业务组件：AppLayout、ProtectedRoute 路由守卫、ErrorBoundary、ui/ 索引 | [View Map](frontend/src/components/codemap.md) |
| `frontend/src/components/layout/` | AppLayout 布局：侧栏/顶栏/Outlet、NavLink 激活态、主题切换 | [View Map](frontend/src/components/layout/codemap.md) |
| `frontend/src/components/ui/` | shadcn/ui 生成组件（17 个，Radix+cva+cn 模式，禁改约束） | [View Map](frontend/src/components/ui/codemap.md) |
| `frontend/src/contexts/` | UserContext：登录态（挂载即 refreshUser，失败静默置 null）+ 与路由/守卫联动 | [View Map](frontend/src/contexts/codemap.md) |
| `frontend/src/lib/` | 工具：cn 合并、Radix bug 修复（clearBodyPointerEventsNoneDeferred） | [View Map](frontend/src/lib/codemap.md) |
| `frontend/src/lib/api/` | axios 封装 client：/vllm_manager/api/v1 前缀、message 透出、401 跳登录、类型化 API 函数 | [View Map](frontend/src/lib/api/codemap.md) |
| `frontend/src/pages/` | 页面：Home/Login/NotFound 懒加载约定、登录回跳防开放重定向 | [View Map](frontend/src/pages/codemap.md) |
| `frontend/src/types/` | 类型：ApiResponse/PageMeta/PageData 对齐后端 BaseResponse/ListResponse | [View Map](frontend/src/types/codemap.md) |

## Key Flow: Instance Lifecycle (end-to-end)

```
用户 ──POST /instances（create）──▶ server/instance/service/creation.py
  候选 ready 节点（只读 node model）→ 权重广播 /models/weight（并发取首个成功）
  → AllocatorService.first_fit（显存记账）→ 建 VllmInstance（pending/target=starting）
  → request_to_worker 转发 start ──▶ worker/lifecycle.start（受理即 200）
worker：显存校验→端口→参数组装（task 映射/env 注入）→Popen→sync 健康检查(/v1/models 1s 200)
  → report 上报 state ──▶ server apply_report 对账（M2/B1/stopped 终态）→ target 清回 none
失联：node offline/unreachable → prober 回调实例域 reconcile_lost_nodes，仅 running 实例置 unreachable（不删除，人工介入）
```
