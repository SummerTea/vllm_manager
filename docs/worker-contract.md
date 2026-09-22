# worker 契约（server↔worker 字段级规格）

> 本文档是 **worker 契约（开发顺序第 5 步）** 的字段级规格：在 worker 实现前冻结
> server↔worker 双向 REST 端点的路径、请求/响应字段、鉴权、幂等与对账规则。
> 蓝图层口径见 `docs/architecture.md` 五、契约边界；gpustack 实证见
> `docs/gpustack-borrowings.md` 与克隆源码 `.slim/clonedeps/repos/gpustack__gpustack/`。
> 如与 architecture.md 冲突，以 architecture.md 为准。

## 0. 定位与边界

**worker 角色**：GPU 机器上的管控节点，监听 `:8100`（`WORKER_DEFAULT_PORT`）。**不连 PG**
（`DISABLED_EXTENSIONS=db`），状态全部保存在进程内存中；server 的 PG 是唯一权威账本，
worker 心跳/状态上报节点存活与 GPU 状态；实例状态经 `/instances/report` 独立端点对账。

**依赖**：

| 能力 | 说明 | 对标 |
|---|---|---|
| 注册/心跳/状态上报 | worker 启动即向 server 注册，此后按 `heartbeat_interval` 心跳，周期性上报 GPU/系统状态 | `worker_manager` / `collector` |
| 实例生命周期 | start/stop、健康检查（`/v1/models` 1s 200）、内存状态机、指数退避重启 | `serve_manager.py` |
| GPU 监控采集 | pynvml 采集 GPU 设备状态（无 GPU/不可用时优雅降级：空列表 + 告警一次，绝不抛异常） | `collector` |
| 权重统计 | 本地扫描模型目录 `.safetensors/.bin/.pt/.pth` 求和 | `policies/utils.py:get_local_model_weight_size` |

**启动方式**：`DISABLED_EXTENSIONS=db`（不初始化 PG 扩展），uvicorn 监听端口
`WORKER_DEFAULT_PORT`（8100）。worker 具体模块路径（`app/worker/` 域）由实现时定义。

**对账闭环总览**：用户操作（创建/启停）→ server 写 `target_state` + 经
`request_to_worker` 转发指令 → worker 在 GPU 机上执行（启动/停止/健康检查/退避重启）
→ worker 心跳/状态上报（实例状态经 `/instances/report` 独立对账）→ server 按对账规则
收敛 `state`、达成后清回 `target_state=none`。

```
用户 ──创建/start/stop──▶ server(PG: target=starting/stopping)
                           │ request_to_worker(Bearer+15s)
                           ▼
                        worker(:8100, 内存态)
                           │ ① 显存校验 ② 端口分配 ③ 参数组装 ④ env 注入
                           │ ⑤ Popen(start_new_session) ⑥ 健康检查(/v1/models 1s 200)
                           ▼
                        vLLM 进程（--port --served-model-name ...）
                           │
worker ──/instances/report 上报实际 state/port/restart_count──▶ server
                           │ 对账（apply_report）：更新 state、达成清 target、
                           │ stopping 且上报消失→stopped、其余消失不动
                           ▼
                     target_state 清回 none（闭环完成）
```

## 1. worker→server 端点

路径前缀统一为 `/vllm_manager/api/v1`（`BASE_URL_PATH` + `/api/v1`）。响应统一
`BaseResponse{code, message, data}`（`code=0` 成功）。

### 1.1 `POST /vllm_manager/api/v1/nodes/register`

- **鉴权**：公开（无鉴权）。
- **语义**：节点注册（幂等）。`machine_id` 非空按 `machine_id` 匹配，否则按 `hostname`
  匹配（幂等回退键）。已存在节点：**复用旧 token 不轮换**，刷新可变字段
  （hostname/ip/advertise_address/worker_port），不写 `heartbeat_time`（liveness 归心跳端点）。
  新节点：`pending` 状态 + 下发新 token（`secrets.token_hex(32)`，DB 列 String(64)）。
  并发重复注册触发 `uix_node_machine_id` 唯一索引时回滚后按原幂等键重查返回已存在节点。

**请求体**（`NodeRegisterRequest`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `machine_id` | string | 否 | 机器唯一标识（幂等注册主键，max 64） |
| `hostname` | string | 是 | 主机名（幂等注册回退键，1–255） |
| `ip` | string | 是 | IP 地址（max 64） |
| `advertise_address` | string | 否 | 对外可达地址（`host:port` 或纯 host；含端口时端口优先，纯 host 自动补 `worker_port`） |
| `worker_port` | int | 是 | worker 端口（默认 `WORKER_DEFAULT_PORT=8100`，1–65535） |

**响应 data**（`NodeRegisterResponse`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `node_id` | string | 节点 ID（UUID7） |
| `token` | string | Bearer 鉴权令牌（重注册返回原 token，不轮换） |
| `worker_port` | int | worker 端口 |
| `heartbeat_interval` | int | 建议心跳间隔（秒，= `NODE_HEARTBEAT_INTERVAL`=10） |

### 1.2 `POST /vllm_manager/api/v1/nodes/{node_id}/heartbeat`

- **鉴权**：Bearer（`get_current_node`：按 token 查节点 + `secrets.compare_digest`
  恒时比对；`current_node.id != node_id` 抛 403 Forbidden）。
- **语义**：空 body；刷新 `heartbeat_time` 并派生节点状态（心跳超时→offline）。
- **响应**：`BaseResponse{code=0, message="success", data=null}`。

### 1.3 `POST /vllm_manager/api/v1/nodes/{node_id}/status`

- **鉴权**：Bearer（同 1.2，token 需匹配 `node_id`）。
- **语义**：全量节点状态上报，落 `system_reserved`/`status` 字段并刷新心跳与状态。
  **GPU 显存单位统一 Bytes**（展示层负责换算 GiB）。

**请求体**（`NodeStatusReportRequest`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `system_reserved` | object | 否 | 系统预留资源 |
| `system_reserved.ram` | int | 否 | 预留内存（Bytes） |
| `system_reserved.vram` | int | 否 | 预留显存（Bytes，整机级，每卡都扣） |
| `status` | object | 否 | 节点状态载荷 |

**`status` 载荷**（`NodeStatus`，`extra="allow"` 容扩展）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `cpu` | object | 否 | CPU 状态 |
| `cpu.total` | int | 否 | CPU 核数 |
| `cpu.utilization_rate` | float | 否 | 利用率（0-100） |
| `memory` | object | 否 | 内存状态 |
| `memory.total` | int | 否 | 内存总量（Bytes） |
| `memory.used` | int | 否 | 已用内存（Bytes） |
| `memory.utilization_rate` | float | 否 | 利用率（0-100） |
| `swap` | object | 否 | 交换分区状态 |
| `swap.total` | int | 否 | 交换分区总量（Bytes） |
| `swap.used` | int | 否 | 已用交换分区（Bytes） |
| `gpu_devices` | array | 否 | GPU 设备列表（无 GPU 时传空数组） |
| `gpu_devices[]` | object | 否 | 单卡状态（见下表） |
| `filesystem` | array | 否 | 文件系统状态 `list[dict]` |
| `os` | dict | 否 | 操作系统信息 |
| `kernel` | dict | 否 | 内核信息 |
| `uptime` | dict | 否 | 运行时长信息 |

**`gpu_devices[]`**（`GPUDeviceStatus`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `index` | int | 否 | GPU 索引（分配依赖，必须保留） |
| `name` | string | 是 | GPU 名称（型号判据，映射 allocator 的 `gpu_type`） |
| `vendor` | string | 否 | 厂商 |
| `uuid` | string | 否 | GPU UUID |
| `memory_total` | int | 否 | 显存总量（Bytes） |
| `memory_used` | int | 否 | 已用显存（Bytes） |
| `utilization_rate` | float | 否 | 利用率（0-100） |
| `temperature` | float | 否 | 温度（摄氏度） |
| `power` | float | 否 | 功耗（瓦） |

> 参考 gpustack `worker/backends/base.py:_get_selected_gpu_devices`：GPU 状态由
> collector 采集、按 index 过滤；`index` 是 server allocator 选卡/记账的依赖键。

- **响应**：`BaseResponse{code=0, message="success", data=null}`。

### 1.4 `POST /vllm_manager/api/v1/instances/report`

- **鉴权**：Bearer（worker 节点 token，`get_current_node`；**仅处理本节点实例**，
  `inst.node_id != node_id` 的条目跳过）。
- **语义**：实例状态对账（独立于 worker-status 载荷；gpustack 实证：实例状态独立上报）。
  合并上报到本节点实例，并处理上报消失的实例。
- **响应**：`BaseResponse{code=0, message="对账 {count} 条", data=null}`。

**请求体**（`InstanceReportRequest`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `items` | array | 是 | 实例状态条目列表 |

**`items[]`**（`InstanceReportItem`）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 实例 ID（server 下发，worker 原样回传） |
| `state` | string | 是 | 实例状态枚举（见下表；非法值 FastAPI 直接 422） |
| `state_message` | string | 否 | 状态说明/失败原因 |
| `port` | int | 否 | 服务端口（worker 分配后经此回填 server） |
| `restart_count` | int | 否 | 重启次数 |

**`state` 合法值**（`InstanceStateEnum`，worker 上报校验即此枚举）：

`pending` / `starting` / `running` / `stopped` / `error` / `unreachable`

（注意：不含 `stopping`——server 侧无此状态，停止意图由 `target_state` 承载；worker
内存态可有 stopping 过渡，但上报时必须收敛到合法枚举。）

**对账规则**（与 `instance/base.py:apply_report` + `service/instance.py:apply_report` 一致）：

1. **上报存在**：`state` 取上报值——但受 **M2 stopped 防复活守卫**拦截：无目标意图
   （`target=none`）且当前 `stopped` 时，不接受任何非 `stopped` 上报（防 stop 收敛后
   晚到的在途旧 running/error 上报复活实例与占账）；`target=starting/stopping` 期间
   **不拦截**（start 从 stopped 发起即依赖此路径）；`target_state` 仅当
   `is_target_achieved(reported_state, target)` 成立时清回 `none`（防 stop/start 在途
   期间被仍带旧 state 的上报清掉 target → 实例卡死不再收敛，B1 竞态）；
   `state_message`/`port`/`restart_count` **仅上报非 None 时更新**，否则保留。
2. **上报消失**：`target_state == stopping` 的实例收敛为 `stopped` + 清 target；
   **其余消失不动**（容忍 worker 重启窗口——重启期间 worker 内存态清空、无法上报，
   不能据此误判已停止）。
3. **未知实例 id**：忽略（记 debug 日志），不影响其他条目。

`is_target_achieved`：`target=starting` → `running` 或 `error` 视为达成（启动失败也是
终态）；`target=stopping` → `stopped` 视为达成；`target=none` 恒达成。

## 2. server→worker 端点

路径**不带** `/vllm_manager/api/v1` 前缀（`build_worker_url` 直接拼
`http://{advertise_address|ip}:{worker_port}/{path}`）。`advertise_address` 含 `":"`
原样使用、纯 host 补 `:worker_port`、未配置时 `ip:worker_port`。

### 2.1 `GET /healthz`

- **鉴权**：Bearer **豁免**（liveness 探针，暴露给 LB/监控，与 gpustack 一致）。
  注：server `prober._probe_node` 探测时仍携带 Bearer token 作兼容双保险，worker 端
  对无 token 的 `/healthz` 应直接 200。
- **语义**：存活探针；200 = worker 存活（不检查业务就绪）。
- 响应：`200 OK`（无 body 契约）。

### 2.2 `POST /instances/{instance_id}/start`

- **鉴权**：Bearer（server 用 node token 统一注入）。
- **语义**：拉起 vLLM 实例。**响应语义：2xx = 指令已受理（非实例已运行）**；
  实例最终状态由后续 `/instances/report` 对账收敛。
- **非 2xx**：server 侧 `request_to_worker` 抛 `HttpClientException`（携带
  `url`/`status_code`）。

**请求体**（`creation.py:_build_start_payload` 生成，创建与手动启动复用）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `instance_type` | string | 是 | 固定 `"vllm"`（多类型预留） |
| `spec` | object | 是 | 实例规格 |
| `spec.model_name` | string | 是 | 模型名称/路径 |
| `spec.task` | string | 是 | 产品层任务分类（`auto`/`llm`/`embedding`/`rerank`），worker 按映射表组装 `--task` |
| `spec.gpu_memory_utilization` | float | 否 | GMU（0-1）；创建路径恒有值（allocator 产出，默认 0.9），为 null 时 worker 以默认 GMU 兜底；用户 args 未含 `--gpu-memory-utilization` 时补注入 |
| `spec.tensor_parallel_size` | int | 是 | 张量并行度（≥1） |
| `spec.args` | array | 否 | vLLM 启动附加参数（`list[str]`，用户参数优先） |
| `gpu_indexes` | array | 是 | 分配的 GPU 索引列表（`list[int]`） |
| `vram_claim` | int | 是 | 显存需求（Bytes） |

**task → `--task` 映射表**（产品层分类 → vLLM 实际枚举）：

| 产品层 `task` | vLLM `--task` | 说明 |
|---|---|---|
| `auto` | 不注入 | vLLM 自动推断（缺省 generate） |
| `llm` | 不注入 | 产品层显式 LLM 标记，行为等价 auto（不注入） |
| `embedding` | `--task embed` | **勿用弃用别名 `embedding`** |
| `rerank` | `--task score` | cross-encoder 重排；vLLM 版本演进（如 0.21+ 移除 score）时按实际枚举映射 |

`args` 已含 `--task` 时不重复注入（用户优先）。

**worker 执行流程**（步骤化；参考 gpustack `serve_manager.py:_start_model_instance`）：

1. **启动前显存校验**：pynvml 读取目标卡空闲显存 ≥ `vram_claim`，不满足**拒启**并回
   `state=error` + 可读 `state_message`（防超卖双保险——server allocator 已记账，此处兜底）。
2. **端口分配**：socket 探测空闲端口 + 内存集合幂等（已分配直接复用；参考
   `serve_manager.py:_assign_ports`：`_port_lock` + `_assigned_ports` 集合）。
3. **参数组装（用户优先）**：扫用户 `args`，缺什么补什么——`--port`（分配的端口）、
   `--served-model-name`（= `model_name`）、`--gpu-memory-utilization`（缺省补 GMU）；
   `CUDA_VISIBLE_DEVICES=gpu_indexes`；`tensor_parallel_size > 1` 补
   `--tensor-parallel-size`（参考 `vllm.py:extend_args_no_exist` + `get_auto_parallelism_arguments`）。
4. **env 注入**：`OMP_NUM_THREADS=1`、`SAFETENSORS_FAST_GPU=1`、
   `VLLM_CACHE_ROOT`（worker 持久目录，可选；参考 `vllm.py:_get_configured_env`）。
5. **Popen(start_new_session=True)**：新会话启动 vLLM 子进程。
6. **健康检查**：`GET /v1/models` 单次 1s 超时 200 → `running`；starting 超时（连续
   失败 N 次，默认 10 次 ≈10s；或总时长超 30s）→ `error`
   （参考 `serve_manager.py:is_ready`；总预算默认值同 §3）。
7. **启动失败**：try/except 显式落 `error` + `state_message`（可读原因；参考
   `serve_manager.py:_start_model_instance` except 分支写 ERROR + state_message）。

### 2.3 `POST /instances/{instance_id}/stop`

- **鉴权**：Bearer。
- **请求体**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `instance_type` | string | 是 | 固定 `"vllm"` |

- **执行流程**：`killpg` SIGTERM → 3s → SIGKILL + psutil 递归树兜底（vLLM 多进程可能
  脱离进程组；参考 `utils/process.py:terminate_process_tree`：psutil 递归 children →
  SIGTERM → wait 3s → SIGKILL）；随后清 worker 内存状态（进程/端口/退避计数）。
- **幂等**：重复 stop（实例不存在/已停止）应返回 200 幂等，不报错。
- 响应语义：2xx = 指令已受理。

### 2.4 `POST /models/weight`

- **鉴权**：Bearer。
- **语义**：权重广播查询（server 并发向所有候选节点发起，**取首个成功**；
  全部失败抛 `ExternalServiceException`；请求覆盖 > 广播）。
- **请求体**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `model_name` | string | 是 | 模型名称 |

- **响应 data**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `weight_bytes` | int | 模型权重大小（Bytes） |

- **实现**：本地扫描模型目录 `.safetensors/.bin/.pt/.pth` 求和
  （参考 `scheduler/calculator.py:calculate_llm_model_weight_size` 与
  `policies/utils.py:get_local_model_weight_size`）。**路径解析策略为 worker 实现细节
  占位**：`model_name` 作为本地路径或约定目录（见 §6）。
- 找不到模型目录/无权重文件：返回 0 或明确错误，由 worker 实现选择（见 §6）。

## 3. 实例生命周期（worker 侧）

- **状态集合**：`pending → starting → running / error`（+ `stopping` 过渡，仅内存态）；
  server 侧 `state` **不含 stopping**（由 `target_state` 承载）。
- **stopped 为终态（worker 侧硬约束）**：worker 自身重启后**不 restore stopped 实例**
  （pid 文件仅用于恢复 running）、**不自动重启 stopped 实例**；从 stopped 重新拉起的
  唯一入口是 server start 指令（`target=starting`）——与 server 侧 M2 对账守卫长期一致
  （worker 若自作主张恢复/重启 stopped 实例并报 running，server 在无 target 意图时会
  静默忽略其非 stopped 上报，产生状态分叉）。
- **健康检查**：进程存活 && `GET /v1/models` 200（单次 1s 超时）→ `running`；
  **starting 超时总预算**：连续健康检查失败 N 次（默认 N=10，约 10s）或自启动起总时长
  超 30s 判 `error`（默认值 worker 实现可调，需 ≥ 模型加载通常耗时）
  （参考 `serve_manager.py:is_ready`）。
- **指数退避重启**：**首次失败立即重试（delay=0）**，之后
  `delay = min(10·2^(n-1), 300s)`（n 为已退避次数，封顶 300s），`restart_count` 随上报
  递增（server 记录）；error 且允许重启 → 延迟后置回 `starting`
  （参考 `serve_manager.py:_restart_error_model_instance`：`min(10·2^(n-1), 300)`）。
  **重启「允许」条件**：无次数上限；收到 stop 指令后清退避计数；与 server 手动 start
  并发触发时依赖 start 幂等 200 兜底防双启动。
- **日志**：`{log_dir}/instances/{instance_id}.{restart_count}.log` 按重启次数轮转，
  崩溃可回看上一轮（参考 `serve_manager.py:_get_numbered_log_path`：`{id}.{restart_count}.log`）。
- **端口回填**：worker 分配端口后经 `/instances/report` 的 `port` 字段回传 server
  （server 端 `apply_report` 仅上报非 None 时更新 port）。
- **与 server 对账联动**：worker 无主动拉取，只上报；server 侧节点失联时对
  `running` 实例置 `unreachable`（不删除，人工介入），恢复后对账拉回。

## 4. 鉴权与错误处理

- **鉴权范围**：除 `/healthz`（豁免）与 `/nodes/register`（公开）外，**所有端点 Bearer**。
  server 侧统一 `request_to_worker(node, method, path)` 注入
  `Authorization: Bearer {node.token}` + 统一超时
  `INSTANCE_REQUEST_TIMEOUT=15s`（可显式覆盖，如权重查询用 `INSTANCE_WEIGHT_TIMEOUT`）。
  （参考 gpustack `server/worker_request.py:request_to_worker`：Bearer + 15s 超时。）
- **错误码约定（worker 侧响应）**：
  - `4xx`：请求参数错误/鉴权失败（401 无/错 token、403 token 与目标不匹配、404 实例不存在、422 参数非法）；
  - `5xx`：worker 内部错误；
  - 均以标准 HTTP 状态码表达（无统一 body 契约要求）。
- **server 异常映射**（`request_to_worker`）：
  - 非 2xx（`httpx.HTTPStatusError`）→ `HttpClientException`（携带 `url` + `status_code`）；
  - 网络/超时等传输错误（`httpx.HTTPError` 其余）→ `ExternalServiceException`
    （`service_name="worker"`，details 携带 `url`）。
- **幂等语义**：
  - `start` 重复下发：同 id 已 running 时 **worker 实现选择**——返回 200 幂等（推荐，
    与 gpustack 一致：`serve_manager.py` RUNNING 跳过 start）或明确错误（待定，见 §6）；
  - `stop` 幂等：重复 stop 返回 200，不报错。

## 5. 配置项（server 侧下发/约束）

| 配置 | 默认值 | 说明 | worker 实现是否需要 |
|---|---|---|---|
| `WORKER_DEFAULT_PORT` | 8100 | worker 默认监听端口（注册可覆盖） | 是（默认端口） |
| `NODE_HEARTBEAT_INTERVAL` | 10s | 建议心跳间隔，注册时下发 | **是（心跳周期）** |
| `NODE_HEARTBEAT_GRACE_PERIOD` | 30s | server 心跳超时阈值（→ offline） | 否（server 侧判定） |
| `NODE_PROBE_INTERVAL` | 15s | server 主动 `/healthz` 探测周期 | 否（server 侧任务） |
| `NODE_PROBE_TIMEOUT` | 3s | `/healthz` 探测超时 | 否（server 侧任务） |
| `INSTANCE_REQUEST_TIMEOUT` | 15s | server→worker 指令转发超时 | 否（server 侧转发） |
| `INSTANCE_WEIGHT_TIMEOUT` | 15s | 权重广播查询超时 | 否（server 侧转发） |
| `INSTANCE_RECONCILE_INTERVAL` | 15s | 实例失联对账周期（server 后台） | 否（server 侧任务） |
| `INSTANCE_DEFAULT_GMU` | 0.9 | 实例默认显存利用率 GMU（0-1） | **是（缺省补 GMU 参数）** |

worker 实现时需要感知：`WORKER_DEFAULT_PORT`（默认监听端口）、
`NODE_HEARTBEAT_INTERVAL`（心跳节奏，注册响应下发）、`INSTANCE_DEFAULT_GMU`
（启动参数组装缺省 GMU）。其余为 server 侧约束/任务，worker 只需遵守端点契约。

## 6. 待定/占位（worker 实现时细化）

- **模型路径解析策略**：`model_name` 作为本地绝对路径还是约定目录（如
  `{model_root}/{model_name}`）？找不到目录/无权重文件时的返回（0 vs 错误）？——
  由 worker 实现定义，server 侧按 `weight_bytes` 字段消费。
- **GPU 监控上报字段细节**：pynvml 不可用/无 GPU 时的降级载荷（空 `gpu_devices` +
  告警一次）、`filesystem`/`os`/`kernel`/`uptime` 的具体结构（dict 内容未冻结）。
- **健康检查端口来源**：worker 分配端口后自持，report 回填 `port`；worker 重启
  restore（pid 文件 + psutil）**仅恢复 running 实例，stopped 不恢复**（与 server M2
  对账守卫长期一致，避免 worker 报 running、server 静默忽略的分叉）；恢复期间端口
  复用策略待定。
- **start 重复下发幂等选择**：200 幂等（推荐）vs 明确错误，worker 实现时定。
- **worker 侧 stopping 过渡期上报值**：内存态有 stopping，但上报枚举无此值——worker
  实现时明确过渡期上报 `starting`（停止中继续报原状态）或 `stopped`。
