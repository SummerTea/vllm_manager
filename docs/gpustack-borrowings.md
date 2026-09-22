# gpustack 借鉴调研报告（vllm_manager 精简版）

> 调研范围：gpustack v2.2.3 克隆源码（`.slim/clonedeps/repos/gpustack__gpustack/`），三个领域分别由架构顾问深读源码产出。
> 项目定位：**只管理 vLLM + 基于显卡资源（显存）分配实例**。gpustack 的复杂部分（K8s/网关/多租户/多机分布式/多后端/计费）一律排除。
> 详细机制可回看各 `codemap.md` 或直接读源码。

---

## 〇、最重要的三个结论

1. **KV cache 不精算**：gpustack `scheduler/calculator.py` 的 1200+ 行 GGUF 精算对 vLLM 完全不适用——vLLM 的 KV cache 由 `--gpu-memory-utilization`（GMU）启动时自分配。调度只做**容量判定**，不估算、不注入 GMU。
2. **声明式显存记账**（K8s Node.Allocated 模式）：谁被分配了什么，由**管理端 DB 说了算**（`allocated = Σ 实例声明`），不信 agent 自报。`available = total - allocated - reserved`，心跳的 `used` 只做展示。
3. **实际状态由最接近真相的一方写入**：worker（agent）直接回写实例实际状态，manager 只做对账与节点失联时的主动干预。

---

## 一、可借鉴项（按落地价值分组）

### Tier 1 — 直接采纳（低成本高收益）

| # | 借鉴项 | gpustack 机制（源码位置） | 我们怎么落地 |
|---|---|---|---|
| 1 | **显存分配在调度层，不在命令注入层** | vllm 后端不注入 GMU（vllm.py 无此代码）；`vram_claim` 由 server 算（schemas/models.py:423），worker 只消费 | agent 启动前用 pynvml 校验：目标 GPU 空闲显存 ≥ 实例声明需求，不满足拒绝启动（防多实例超卖双保险） |
| 2 | **实例需求估算** | `estimate_model_vram()`（policies/utils.py:384）：`claim = weight × 1.2 + 2GiB(LLM)`；权重来源：本地目录扫描 `.safetensors/.bin/.pt/.pth` 求和 | manager 创建实例时让 **agent 本地统计权重大小**（免 HF API 依赖）；`vram_claim = weight×1.2 + 2GiB` |
| 3 | **选卡判定（first-fit）** | `vllm_resource_fit_selector.py:401`：逐卡判 `① available/total ≥ GMU ② vram_claim ≤ total×GMU`；多卡 TP 时按 allocatable 率降序累加 + `num_attention_heads % tp == 0` 整除校验 | 单卡/多卡 TP 直接照抄三条件；多卡时 agent 本地读 `config.json` 校验 TP 整除 |
| 4 | **决策结果 → 启动参数注入** | 调度结果回写 `gpu_indexes`；worker `_get_selected_gpu_devices()` 按卡过滤，`get_auto_parallelism_arguments()` 自动补 `--tensor-parallel-size N`（vllm.py:1028） | 决策结果 `{node_id, gpu_indexes, gmu}` 随启停指令下发；agent 注入 `CUDA_VISIBLE_DEVICES="0,1"` + 多卡补 `--tensor-parallel-size`，GMU 用户参数透传 |
| 5 | **健康检查 = HTTP 而非进程存活** | `is_ready()`（serve_manager.py:1741）：`GET /v1/models` 1s 超时 200 即 running | `running` 判定 = 进程存活 && `/v1/models` 200（1s 超时）；同时是启动超时/失败判定的标准答案 |
| 6 | **启动失败显式落 ERROR + state_message** | `_start_model_instance()` except 分支（serve_manager.py:1392）写 ERROR + 可读原因 | agent 启动 try/except，失败信息写入 `state_message` 随心跳上报，manager 直接展示 |
| 7 | **psutil 递归树终止兜底 killpg** | `terminate_process_tree()`（utils/process.py:70）：psutil 递归 children → SIGTERM → 3s → SIGKILL | 保留现有 killpg（3s 节奏），补充 psutil 递归树兜底（vllm 多进程可能脱离进程组） |
| 8 | **注册幂等下发 token + 配置** | `POST /v2/workers`（routes/workers.py:613）：一次返回 `token + worker_config`；按 uuid/name 重注册**复用旧 token 不轮换**（幂等） | manager node 表加 `token` 列（`secrets.token_hex(24)`）；注册按 hostname 幂等，重注册返回原 token |
| 9 | **心跳与状态上报分离** | `/worker-heartbeat`（空 body 只记 ID）vs `/worker-status`（全量载荷）；5s 缓冲批量落库（worker_status_buffer.py） | 保留两端点分离；manager 低并发直接落库，可选照抄 20 行批量 UPDATE |
| 10 | **离线判定收敛为 compute_state()** | `Worker.compute_state()`（schemas/workers.py:379）：心跳超时→NOT_READY + 可读文案；主动探测失败→UNREACHABLE | Node 模型加 `compute_state()`（30s 心跳超时→offline + state_message）；保留 `/healthz` 主动探测（15 行）区分「agent 死 vs 断网」 |
| 11 | **实例 target_state + state 双字段** | gpustack 单 state 多方写（worker PUT 回写）；停止=删实例 | 我们需显式停止 → `target_state ∈ {none, starting, stopping}`（manager 写）+ `state ∈ {pending, starting, running, stopping, stopped, error, unreachable}`（心跳实例列表驱动对齐） |
| 12 | **实例对账 = 心跳实例列表** | worker 直接 PUT 实例状态，server 不猜 | 心跳载荷内嵌实例列表，manager 与 DB 逐实例对账（新增/消失/迁移），达成目标态后清 target_state |
| 13 | **env 优化注入** | `_get_configured_env()`（vllm.py:356）：`OMP_NUM_THREADS=1`、`SAFETENSORS_FAST_GPU=1`、`VLLM_CACHE_ROOT` 持久目录 | 直接采纳前两个；VLLM_CACHE_ROOT 指向 agent 持久目录（可选） |
| 14 | **日志按 restart_count 编号轮转** | `{log_dir}/serve/{id}.{restart_count}.log`（serve_manager.py:890） | `logs/{instance_id}.{n}.log` 轮转，崩溃可回看上一轮 |
| 15 | **周期对账 + 指数退避重启** | `sync_model_instances_state()` 周期权威判定；`_restart_error_model_instance()`：`delay = min(10·2ⁿ, 300s)` | agent `sync_loop()`（3-5s）：starting 超时→error；error 且允许重启→退避置回 pending；单次异常不终止线程 |
| 16 | **端口分配全局锁 + 幂等** | `_assign_ports()`（serve_manager.py:1414）：`_port_lock` + `_assigned_ports` 集合，已分配直接返回 | agent 启动前 socket 探测端口 + 内存集合记录已分配端口（对应"端口冲突拒绝"约定） |
| 17 | **参数注入用户优先** | `extend_args_no_exist()`（utils/command.py:137）：用户已传同名参数则不注入 | agent 组装命令时先扫用户参数，缺什么补什么（尤其 `--port`/`--served-model-name`/GMU） |
| 18 | **agent 端点统一 Bearer 鉴权** | `worker_auth`（api/auth.py:401）：agent 端点只认 worker token；server→agent 统一走 `request_to_worker`（worker_request.py:153，统一注入 token + 15s 超时） | manager 一个 `request_to_agent(node, method, path)` 工具函数统一注入 Bearer；agent 除注册外所有端点挂 token 鉴权依赖 |

### Tier 2 — 可选/进阶（按需）

| 借鉴项 | 说明 | 何时做 |
|---|---|---|
| 推理级健康检查 | POST `/v1/chat/completions`（max_tokens=1）探真推理；有真实流量则跳过 | 需更高可用性时，可配置项（阈值固定 3 次） |
| injected_backend_parameters 回写 | 平台自动注入的参数回写展示（最终命令 - 用户参数 = 注入项） | UI 想展示"平台加了什么参数"时，成本极低 |
| GPU 采集字段标准化 | `{index, total, used, util, temperature, power}`，单位统一 Bytes，展示层换算 GiB | agent 上报结构对齐，`index` 必须保留（分配依赖） |
| 拒绝原因可解释 | 失败只给一条字符串，如「卡 1 可用 20GiB < 需求 26GiB」 | allocator 返回首个失败原因即可 |

---

## 二、排除清单（明确不做）

### 领域级（整个功能域不要）

| 排除项 | 理由 |
|---|---|
| GGUF/llama.cpp 估算链 | calculator 主体、gguf-parser 二进制、offload 分档——只跑 vLLM 全作废 |
| 多节点分布式 | Ray/MP 跨机、`subordinate_workers`、`distributed_servers`、多机拓扑——单实例单机 |
| 调度队列/重调度/评分链 | `AsyncUniqueQueue`、ANALYZING 门控、BINPACK/SPREAD 评分、ModelFileLocality——first-fit 足够 |
| 多后端 | SGLang/MindIE/VoxBox/Custom 分派——backend 固定 vLLM |
| 容器 WorkloadPlan | gpustack-runtime create_workload 等——直接 subprocess |
| 模型文件下载管理 | ModelFileManager、HF/GGUF 解析——vllm serve 自管模型加载 |
| 多租户 | TenantContext/principals/api_keys 全套——单管理端单租户 |
| 多实例 HA | leader 选举、coordinator、LocalCoordinator——单管理端进程 |
| 事件总线/控制器订阅 | bus.py/workqueue/watch SSE——10s 轮询 + SQLite 直读即可 |
| 计量计费 | metrics_collector/usage_archiver——无按量计费诉求 |
| 网关/K8s/云/隧道 | gateway(Higress)、k8s、cloud_providers、gpu_instances、websocket_proxy——无此需求 |
| benchmark 压测 | BenchmarkManager/runner——需求外 |
| 会话安全体系 | argon2/JWT/cookie——token 明文比对即可（本地 SQLite） |

### 细节级（单点不要）

- 容器日志持久化线程 / 孤儿 workload 清理器（pid 文件 + 进程存活即等价对账）
- 版本化 run_command 模板（固定 `vllm serve`）
- 分布式 env 注入（NCCL/GLOO/HCCL/RAY_LOG 系列）
- worker 推理代理与流量回执（manager 直连转发，无代理层）
- `multiprocessing.Process + RedirectStdoutStderr` 包装（Popen + start_new_session 更贴合）
- UMA 统一内存 / extended KV cache RAM claim（NVIDIA 独立显存）
- 异构 GPU 分组聚类（最多保留"同节点同型号才允许 TP 混卡"一条校验）
- 事件记录器/调度消息聚合（只需一条拒绝原因字符串）

---

## 三、最小必要设计建议（三端整合）

### agent 端（vLLM 生命周期）

- **状态集合**：`pending → starting → running → error`（+ `stopping`）；内存维护 `target_state`（manager 下发）与 `actual_state`（实测）
- **关键函数**：
  - `start_instance`：显存校验（pynvml 空闲 ≥ 需求，否则拒绝）→ 端口探测 → 组装命令（用户参数优先补 `--port/--served-model-name/GMU`，env 注入）→ `Popen(start_new_session=True)` → 写 pid 文件 → `starting`
  - `check_health`：进程存活 && `GET /v1/models`（1s 超时）→ `running`，否则计数
  - `stop_instance`：killpg SIGTERM → 3s → SIGKILL + psutil 递归树兜底；清状态
  - `restore_instances`：重启时读 pid 文件 + psutil 存活恢复
  - `sync_loop`（3-5s）：starting 超时→error；error 可重启→指数退避（10·2ⁿ ≤ 300s）置回 pending；running 失联→error；失败写 `state_message`

### manager 端（allocator.py，约 150-250 行）

- **数据**：节点 GPU 表 `{index, total, used, allocated, reserved}`；`available = total - allocated - reserved`
- **需求**：`vram_claim = weight×1.2 + 2GiB`（weight 由 agent 统计 `.safetensors/.bin/.pt/.pth` 求和）；用户填 GMU（默认 0.9）
- **选卡**：遍历节点 → 卡按 `available/total` 降序 → 判定 `① available ≥ total×GMU ② vram_claim ≤ total×GMU`，首张满足即命中；多卡 TP 累加 + `heads % tp == 0`；全失败返回首个失败原因
- **记账**：创建实例扣 `allocated += total×GMU`，删除释放（与心跳 `used` 无关）

### manager↔agent 契约

- **注册**：`POST /api/nodes/register` 幂等（按 hostname 匹配）→ 响应 `{node_id, token, agent_port, heartbeat_interval}`；重注册不轮换 token
- **心跳**：`POST /api/nodes/{id}/heartbeat` 空 body 记时间；`POST /api/nodes/{id}/status` 全量（GPU 状态 + 实例列表）
- **鉴权**：agent 所有管理端点 Bearer `{node.token}`（注册端点例外）；manager 统一 `request_to_agent()` 转发
- **对齐**：用户操作 → 写 target_state + 转发指令 → 心跳实例列表对账 → 达成后清 target_state；节点 offline → 其实例置 unreachable（不自动删除，人工介入）

---

## 四、落地优先级

1. **契约与鉴权**：注册 token、心跳/状态分离、`compute_state()`、`request_to_agent()` 转发
2. **agent 生命周期**：健康检查、状态机、对账 sync_loop、退避重启、显存启动前校验
3. **manager 显存分配**：GPU 记账表、需求估算、first-fit 选卡、参数注入
4. **前端展示**：节点 GPU 分配视图（allocated/available）、实例状态与 state_message
