# vllm_manager

vLLM 推理服务集群管理平台：**server 管理端（Web + REST，:8000）+ worker 管控节点（:8100）双进程架构**，server 按显存（GPU/CPU）做分配决策，经 worker 以 docker 拉起/停止/监控 **vLLM 推理实例**，实例生命周期（启动/健康检查/退避重启/失联联动）闭环于两端的契约状态机。

> **状态**：server（node/instance/allocator 业务域）+ worker（通信闭环/生命周期）已实现，并通过**本机 colima/CPU** 与**真实 GPU 靶场（A800 2×80GB）**全链路集成验证（284 passed）。前端为样板骨架（登录/首页占位）。

## 目录结构

```
vllm_manager/
├── backend/                  # FastAPI 后端（Poetry 工程，in-project venv）
│   ├── pyproject.toml        # 依赖（fastapi/sqlalchemy-asyncpg/redis/saq/uuid-utils）
│   ├── .env.example          # 配置模板（应用/数据库/Redis/日志/Worker CPU 段）
│   ├── .env                  # 本机配置（不入库）
│   ├── app/
│   │   ├── config.py         # AppConfig 单例（pydantic-settings，frozen）
│   │   ├── exception.py      # 分层异常体系（Http/Business/ExternalService）
│   │   ├── main/             # 入口包：app 单例/lifespan/routers/web_routers/static_files/全局异常 handler
│   │   ├── work_saq.py       # SAQ worker 入口（-q/-c/--check）
│   │   ├── work_worker.py    # worker 入口（uvicorn factory，:8100，--check）
│   │   ├── base/             # base_config/base_crud/base_enum/base_model/响应契约
│   │   ├── extensions/       # database/redis/saq/logging（懒加载，import 零连接）
│   │   ├── utils/            # request_to_worker（server→worker 统一转发）
│   │   ├── server/           # 业务域：node←instance←allocator 单向依赖
│   │   │   ├── node/         # 节点登记/状态派生/主动探测/删除守卫
│   │   │   ├── instance/     # vLLM 实例生命周期/对账闭环/失联联动
│   │   │   └── allocator/    # 显存分配决策（纯函数，GPU/CPU 双路径）
│   │   └── worker/           # worker 域：GPU 机器实例管控（docker 拉起，状态全内存）
│   └── tests/                # 契约测试（284 passed）
├── frontend/                 # React 19 + Vite + Tailwind + shadcn/ui（bun）
│   ├── package.json / vite.config.ts   # base=/vllm_manager/frontend/
│   └── src/                  # 路由/布局/登录占位（样板示例页）
├── docs/                     # 架构/契约/测试/集成文档（见「文档索引」）
├── .slim/clonedeps/repos/gpustack__gpustack/   # 参考源码快照（v2.2.3，只读）
├── docker-compose.yml        # PG + Redis（本机已有环境时可跳过）
├── codemap.md                # Repository Atlas（各层级 codemap 入口）
└── AGENTS.md
```

## 能力与验证状态

| 能力 | 实现 | 验证 |
|---|---|---|
| 节点管理 | 幂等注册（machine_id→hostname）、状态派生（pending/ready/offline/unreachable）、主动 /healthz 探测、删除=机器退役（存活守卫 + 级联删 stopped 实例） | CPU 本地 + A800 GPU 真机 E2E |
| 资源分配 | 显存记账 first-fit（单卡/多卡 TP、系统预留、CPU 分支 fail-closed）、权重广播估算、并发 create advisory lock 串行、指定 node_id 防 fallthrough | 分配/拒绝路径/并发/缺口实证全部通过 |
| 编排下发 | create → docker 拉起（GPU `--gpus device=` + `--network host` / CPU `-p` 映射）→ 健康检查 → running → 对账收敛 | CPU 与 GPU（tp=2 双卡）均跑通 |
| 生命周期 | stop/delete/error 清理、退避重启自愈、worker 掉线→实例 unreachable→恢复 restore、stop 悬挂限次重下发 | 全场景 E2E + 284 passed |
| GPU 监控 | pynvml 采集（优雅降级：无 GPU → 空列表不抛异常）、accelerator 显式声明 | A800 真机 gpu_devices 实测 |

## 快速开始

```bash
# backend（工作目录 backend/）
poetry install
cp .env.example .env          # 按需修改（本机默认配置已写好 backend/.env）
poetry run uvicorn app.main:app --port 8000     # server 入口（需 PG + Redis）
poetry run python app/work_worker.py            # worker 管控节点（:8100，WORKER_LISTEN_PORT）
poetry run python app/work_saq.py -q default    # SAQ worker
poetry run pytest                              # 契约测试（284 passed）
poetry run ruff check app                       # lint

# frontend（工作目录 frontend/）
bun install
bun run dev                   # 开发 :5178，SPA 访问 /vllm_manager/frontend/
bun run build                 # 构建
```

本机 CPU 集成测试（colima + CPU 版 vLLM）与 A800 真机集成见「文档索引」。

## 文档索引

| 文档 | 内容 |
|---|---|
| `docs/architecture.md` | 架构蓝图/边界总纲（node←instance←allocator 单向依赖、契约、开发顺序） |
| `docs/worker-contract.md` | server↔worker 字段级契约（§1 端点到对账语义、§2 模板占位符、§5 配置） |
| `docs/local-testing.md` | 本机 CPU 集成测试 runbook（§7 场景矩阵 + §8 A800 GPU 结论） |
| `docs/a800-worker-deploy.md` | A800 集成 runbook（地基绿/部署/连通性/故障排查，指导后续集成） |
| `docs/a800-environment.md` | A800 主机环境台账（硬件/软件/服务/部署物/变更日志） |
| `docs/gpustack-borrowings.md` | gpustack 借鉴调研（生命周期/显存分配落地规格） |
| `codemap.md` + 各级 `codemap.md` | Repository Atlas（目录职责/入口/数据流） |

## 关键机制（写业务前必读）

- **分层依赖单向**：业务域 → `base/` → `extensions/`；`node ← instance ← allocator`（allocator 纯函数零 DB IO）；worker 业务指令只从 instance 域经 `request_to_worker` 发出。
- **路径前缀**：`BASE_URL_PATH`（默认 `/vllm_manager`）为所有 REST 端点与前端静态资源前缀。
- **统一响应**：`BaseResponse{code, message, data}`；业务失败抛 `app.exception` 分层异常，全局 handler 渲染（worker 错误码透传）。
- **对账闭环**：用户操作 → server 写 target_state + 转发 → worker 执行 → report 对账（M2 守卫/B1 target 保留/stopping 消失收敛/stopped 终态）→ 清 target；stop 悬挂限次重下发 → 人工介入。
- **分配 fail-closed**：仅 `accelerator=="cpu"` 走 CPU 分支（`gpu_indexes=[]`、tp>1 拒绝）；GPU/未声明/空 `gpu_devices` → 拒绝「无可用 GPU」；记账 `available = total − Σallocated − reserved`（**不消费外部占用 memory_used**，外部占用用 `WORKER_SYSTEM_RESERVED_VRAM` 配置预留缓解）。
- **GPU 模板契约（A1）**：官方镜像 `ENTRYPOINT=["vllm","serve"]`，默认模板不含 `{vllm_bin} serve`；多卡用内引号 `--gpus '"device=X,Y"'`（tp>1 必需）；CPU 用 `-p {port}:{port}` + 用户 args 透传（`--enforce-eager --dtype float32 --max-model-len 2048 --gpu-memory-utilization 0.65`）。
- **节点删除 = 机器退役**：READY/UNREACHABLE（心跳宽限 30s 内）→ 409；仅 PENDING/OFFLINE 可删，删除时级联删本节点 stopped 实例（防孤儿）。
- **懒加载零副作用**：`extensions/` 连接首次调用才创建，import 零连接；lifespan 按 `DISABLED_EXTENSIONS` 控制初始化。
- **GPU 监控优雅降级**：pynvml 不可用/无 GPU → 空列表 + 告警一次，绝不抛异常。

详细参考：`backend/tests/`（契约测试样板）、`docs/worker-contract.md`（契约规格）、`docs/local-testing.md`（集成测试）。
