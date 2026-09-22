# vllm_manager 架构蓝图：模块骨架与能力边界

> 本文档是**蓝图层**设计：定义模块骨架、能力边界与依赖方向，守住架构不跑偏。
> 各模块的字段/接口/实现细节在开发该模块时另行细化（instance → allocator → worker 契约逐模块展开）。
> 参考：`docs/gpustack-borrowings.md`（借鉴调研）、gpustack 克隆源码 `.slim/clonedeps/repos/gpustack__gpustack/`。

## 一、定位与术语

**定位**：只做一件事——一个 server 管理端（Web + REST）通过 worker 管控端在 GPU 机器上拉起/停止/监控 **vLLM 推理实例**，并按**显存**做分配决策。

**术语对照**（与 gpustack 对齐）：

| 我们 | gpustack | 含义 |
|---|---|---|
| server（`backend/app/server/`，:8000） | server | 管理端（Web + REST） |
| worker（`backend/app/worker/`，:8100，规划中） | worker | GPU 机器上的管控程序 |
| node（`backend/app/server/node/`） | Worker（表） | GPU 节点记录（保留 node 命名，≈ gpustack Worker 表） |
| vllm_manager（产品名） | — | BASE_URL_PATH/DB/表前缀/异常类，不变 |

## 二、模块总览

| 模块 | 能力 | gpustack 参照 | 状态 |
|---|---|---|---|
| server → `node` 子域 | 节点登记/心跳/状态/主动探测/失联判定 | `worker_manager` + `server/worker_syncer` + `worker_status_buffer` | ✅ 已落地 |
| server → `instance` 子域 | 实例记录、状态机、启停指令转发、心跳对账、失联联动 | `serve_manager`(协调部分) + `server/controllers` | 📋 规划 |
| server → `allocator` 子域 | 显存分配决策（纯函数：需求估算输入、first-fit 选卡、记账口径） | `vllm_resource_fit_selector` + `policies/utils` | ✅ 已落地 |
| server → 横切 `utils/request_to_worker` | server→worker 统一转发（Bearer 注入 + 超时） | `worker_request.py` | 📋 规划 |
| worker（规划） | 注册/心跳/状态上报、实例生命周期、GPU 监控采集、权重统计 | `worker/*`（serve_manager + collector + backends/vllm） | 🚫 骨架无 |
| frontend | 节点 GPU 分配视图、实例状态列表、创建/启停操作 | — | 骨架样板页 |
| 明确不做 | 见「六、不做清单」 | 借鉴文档排除清单 | — |

## 三、server 端子域边界（依赖单向，守住）

```
node（基础设施存在：登记/心跳/状态/存活）
   ↑ 只读引用（node_id 归属、token、status GPU 表、system_reserved）
instance（多类型工作负载生命周期：vllm / llmcache / 亲和路由…；记录/状态机/启停转发/对账/失联联动）
   ↑ 调用纯决策（输入由调用方取好，输出 {node_id, gpu_indexes, gmu} 或拒绝原因）
allocator（分配决策：纯函数，零 DB IO）
```

- **不设 `model` 子域**：模型信息（model_name/权重/vram_claim）作为 instance 的属性；权重缓存与模型列表二期按需再加
- **instance 为多类型实例域**：每种组件一张表（现阶段 `vllm_manager_vllm_instance`，未来 llmcache/亲和路由各建各表）；公共字段用 `InstanceLifecycleMixin`、对账/状态机规则用纯函数（`instance/base.py`）跨类型复用。新增类型 = 新表 + 枚举扩展，编排/对账/allocator 零改动
- **编排层 = InstanceService**（请求内同步编排：查候选节点→权重→allocator 决策→建记录→转发指令；无独立 scheduler/controller 线程——gpustack 的调度队列/事件总线已排除，状态收敛靠 agent report 对账 + `reconcile_loop` 后台任务）
- **allocator 刻意无副作用**：可单测、可复用、不写库；记账由 instance 记录承载（停止/删除自然释放）
- **worker 业务指令（启停/权重查询）只从 instance 域发出**；node 域仅健康探测（/healthz）与接收上报；`request_to_worker` 放 `app/utils/`（仅只读引用 node 域 token/base_url，不引入 service）解耦
- 子域文件约定（依 AGENTS.md）：`model.py`/`schema.py`/`service.py`/`api.py`/`dependencies.py`/`enum.py`，**不为二期预留空占位文件**（按需新建）

## 四、worker 端能力骨架（未来实现时的边界）

| 能力 | 职责 | 对标 |
|---|---|---|
| 注册/心跳/状态 | 上报节点存活与 GPU/系统状态 | `worker_manager` / `collector` |
| 实例生命周期 | start/stop、健康检查(`/v1/models` 1s)、状态机、指数退避重启、重启 restore | `serve_manager` |
| 显存启动前校验 | pynvml 空闲显存 ≥ 需求，不满足拒启（防超卖双保险） | `_start_model_instance` 前置 |
| 权重统计 | 本地扫描 `.safetensors/.bin/.pt/.pth` 求和 | `get_local_model_weight_size` |
| 端口分配 | 探测空闲端口 + 内存集合幂等 | `_assign_ports` |
| 参数组装 | 用户参数优先，缺省补 `--port/--served-model-name/GMU/--tensor-parallel-size` + env 注入（OMP/SAFETENSORS/VLLM_CACHE_ROOT） | `vllm.py` |
| 鉴权 | 除注册外全部端点 Bearer token | `api/auth.py` |
| 不连 PG | `DISABLED_EXTENSIONS=db`，状态全内存 | — |

## 五、server↔worker 契约边界

| 方向 | 端点 | 说明 |
|---|---|---|
| worker→server | `POST /nodes/register`、`/nodes/{id}/heartbeat`、`/nodes/{id}/status` | ✅ 已定 |
| worker→server | `POST /instances/report` | 实例状态对账（独立端点；gpustack 实证：实例状态独立于 worker-status 载荷） |
| server→worker | `GET /healthz` | 节点健康探测（已落地，Bearer 豁免） |
| server→worker | `POST /instances/{id}/start` | 启动指令（携带 `{instance_type, spec, gpu_indexes, vram_claim}`；vllm spec = `{model_name, gmu, tensor_parallel_size, args}`） |
| server→worker | `POST /instances/{id}/stop` | 停止指令（仅携带 `{instance_type}`） |
| server→worker | `POST /models/weight` | 权重广播查询（并发取首个成功；响应 `{"weight_bytes": int}`） |

**实例状态机**（借鉴 Tier1 #11，显式停止增强）：
- `state`：`pending → starting → running → stopped`；`→ error`（启动/运行失败，state_message 记原因）；`running → unreachable`（节点失联，恢复后对账拉回）
- `target_state ∈ {none, starting, stopping}`：用户操作写入，达成后清回 none
- 对账规则：上报有→更新+清 target；`target=stopping` 且上报消失→`stopped`+清 target；其余消失不动（容 worker 重启窗口）

**关键机制口径**（gpustack 源码实证，实现 allocator/instance 时照此）：
- 记账：`available = total − Σ(未停止实例 allocated_vram) − reserved`；**starting/running/error/unreachable 均占账，stopped 释放**；reserved 整机级，每张卡都扣
- 单卡判定：`available/total ≥ GMU` 且 `vram_claim ≤ total×GMU` → 记账 `total×GMU`
- 多卡 TP：候选卡（available/total > GMU）按可用显存字节降序累加，`Σ(total×GMU) ≥ vram_claim` 命中；**TP 整除校验落 worker 端**（server 无模型 config.json）
- 需求：`vram_claim = weight×1.2 + 2GiB(LLM)`，支持请求内 `vram_claim`/`model_weight_bytes` 覆盖
- 失联联动：节点心跳超时（offline）或主动探测失败（unreachable）时，仅 `running` 实例置 unreachable（不删除，人工介入；节点失联时直接删除实例可能残留 worker 侧孤儿进程——无记账但占显存，需人工介入）

## 六、不做清单（明确排除）

K8s/网关、多租户、模型文件下载管理、多机分布式、调度队列/评分链、多后端、容器 WorkloadPlan、事件总线/控制器订阅、计量计费、压测、推理代理/流量回执、GGUF 估算链、UMA 统一内存、异构 GPU 分组（仅保留同节点同型号 TP 校验一条）。

## 七、开发顺序

1. ✅ 术语对齐（本蓝图前置，已提交）
2. ✅ `allocator`（纯函数，已提交）
3. `utils/request_to_worker`（server→worker 统一转发，供 instance 使用）
4. `instance`（多类型实例域，现阶段 vllm 表：依赖 allocator + request_to_worker；创建/启停/对账/失联）
5. worker 契约文档（start/stop/weight/report 端点细化）
6. worker 实现（生命周期/监控/鉴权）
7. frontend（节点 GPU 分配视图、实例状态与操作）

## 八、术语与命名速查

- 角色：server（管理端）/ worker（管控程序）/ node（节点记录）
- 字段：`worker_port`（原 agent_port）、`WORKER_DEFAULT_PORT`（原 AGENT_DEFAULT_PORT）
- 工具：`request_to_worker(node, method, path)`（原 request_to_agent）
- 产品名 `vllm_manager`/`VllmManagerException` 不变；外部项目 `synapse-agent` 不变
