# worker 契约（server↔worker 字段级规格）

> 本文档是 **worker 契约（开发顺序第 5 步）** 的字段级规格：在 worker 实现前冻结
> server↔worker 双向 REST 端点的路径、请求/响应字段、鉴权、幂等与对账规则。
> 蓝图层口径见 `docs/architecture.md` 五、契约边界；gpustack 实证见
> `docs/gpustack-borrowings.md` 与克隆源码 `.slim/clonedeps/repos/gpustack__gpustack/`。
> 如与 architecture.md 冲突，以 architecture.md 为准。

## 0. 定位与边界

**worker 角色**：GPU 机器上的管控节点，监听 `:8100`（`WORKER_DEFAULT_PORT`）。**不连 PG、
不初始化任何扩展**（无 DB/Redis 依赖，无需 `DISABLED_EXTENSIONS`），状态全部保存在
进程内存中；server 的 PG 是唯一权威账本，worker 状态上报节点存活与 GPU 状态；
实例状态经 `/instances/report` 独立端点对账。

**依赖**：

| 能力 | 说明 | 对标 |
|---|---|---|
| 注册/状态上报 | worker 启动即向 server 注册，此后周期上报 GPU/系统状态（status_loop 15s，承担存活） | `worker_manager` / `collector` |
| 实例生命周期 | start/stop、健康检查（`/v1/models` 1s 200）、内存状态机、指数退避重启 | `serve_manager.py` |
| GPU 监控采集 | pynvml 采集 GPU 设备状态（无 GPU/不可用时优雅降级：空列表 + 告警一次，绝不抛异常） | `collector` |
| 权重统计 | 本地扫描模型目录 `.safetensors/.bin/.pt/.pth` 求和 | `policies/utils.py:get_local_model_weight_size` |

**启动方式**：worker 不初始化任何扩展（database/redis/saq 均不加载，**无需
`DISABLED_EXTENSIONS` 环境变量**）；监听 `WORKER_DEFAULT_PORT`（8100）。启动命令：

```bash
uvicorn app.worker.main:create_app --factory --host 0.0.0.0
```

（`--host 0.0.0.0` 为硬约束：server 需经 `advertise_address`/`ip` 跨机回连 worker，
绑定 127.0.0.1 会导致回连失败。）

**对账闭环总览**：用户操作（创建/启停）→ server 写 `target_state` + 经
`request_to_worker` 转发指令 → worker 在 GPU 机上执行（启动/停止/健康检查/退避重启）
→ worker 状态上报（实例状态经 `/instances/report` 独立对账）→ server 按对账规则
收敛 `state`、达成后清回 `target_state=none`。

```
用户 ──创建/start/stop──▶ server(PG: target=starting/stopping)
                           │ request_to_worker(Bearer+15s)
                           ▼
                        worker(:8100, 内存态)
                           │ ① 显存校验 ② 端口分配 ③ 参数组装 ④ env 注入
                           │ ⑤ Popen docker run（模板渲染，--network host）⑥ 健康检查(/v1/models 1s 200)
                           ▼
                        vLLM 容器（--port --served-model-name ...，stdout 重定向日志）
                           │
worker ──/instances/report 上报实际 state/port/restart_count──▶ server
                           │ 对账（apply_report）：更新 state、达成清 target、
                           │ stopping 且上报消失→stopped、其余消失不动；
                           │ stopping 且上报仍非 stopped → 限次重下发 stop（≤3）
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
   （hostname/ip/advertise_address/worker_port），不写 `heartbeat_time`（liveness 归状态上报端点）。
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

> **部署注记（lockstep）**：本契约端点/字段按**同步升级**管理——删字段（如
> `heartbeat_interval`，A3 精简）后，**旧版 worker 对新版 server 注册会因响应缺字段
> 校验失败、新版 worker 对旧版 server 亦同**；升级须 server/worker 同批次发布，
> 无版本漂移策略。

### 1.2 `POST /vllm_manager/api/v1/nodes/{node_id}/status`

- **鉴权**：Bearer（`get_current_node`：按 token 查节点 + `secrets.compare_digest`
  恒时比对；`current_node.id != node_id` 抛 403 Forbidden）。
- **语义**：全量节点状态上报，落 `system_reserved`/`status` 字段并刷新存活时间
  （`heartbeat_time`）与状态。
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
| `accelerator` | string | 否 | 加速器类型（`gpu`/`cpu`，worker `WORKER_ACCELERATOR` 配置值）；server 据此判定 CPU 分配，**缺省/未知 → fail-closed 按 GPU 处理** |
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

> **`unreachable` 标志语义（prober 独占）**：`unreachable` 是 **server→worker 回连方向**的
> 失败记录（`prober._probe_node` 探测 `/healthz` 非 200/网络失败时置 True，成功时置 False），
> **status 上报不清除**——status 是 worker→server 方向，上报成功 ≠ 回连可达（反例：advertise
> address 配置错误/单向防火墙，worker 照常上报但指令转发全失败）。因此 worker 重启后首轮
> 状态轮询窗口内节点可能短暂显示 `unreachable`（心跳已刷新但上一轮探测失败标志未清），
> **≤ 一个探测周期（`NODE_PROBE_INTERVAL`，默认 15s）自愈回 `ready`**——属契约行为，非故障。

### 1.3 `POST /vllm_manager/api/v1/instances/report`

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
3. **停止指令未生效（悬挂收敛，server 侧限次重下发）**：`target_state == stopping`
   但上报状态仍非 `stopped`（如 worker 崩溃恢复后容器仍 running、或 worker 停止
   未确认）→ server **不**自动收敛 stopped，而是经 `request_to_worker` **重新下发
   stop**（每轮对账至多 3 次，实例列 `target_retry_count` 计数）；超限后写
   `state_message`「停止指令多次未生效，需人工介入」并保持 `target=stopping`
   （绝不自动收敛——容器是否真正停止只能由 worker 确认）。
4. **未知实例 id**：忽略（记 debug 日志），不影响其他条目。

`is_target_achieved`：`target=starting` → `running` 或 `error` 视为达成（启动失败也是
终态）；`target=stopping` → `stopped` 视为达成；`target=none` 恒达成。

> **worker 侧配套（不谎报停止）**：worker `stop()` 在 `docker` 命令不可用（停止结果
> 未知）时**不得**清除内存记录/meta 并静默消失——恢复调用前状态、保留 meta、返回
> 非 2xx（server 透传失败信号），供上述对账重下发兜底；仅确认容器已停止/清理才清
> 记录（`process_utils.stop_container` 返回 bool）。

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
  `url`/`status_code`）；server 全局 handler 透传该 `status_code`（worker 401/403
  映射为 502——避免前端把 server↔worker 鉴权失败误判为自身登录失效；其余 4xx/5xx
  原样透出）。

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
| `template` | string | 是 | docker 启动模板（含 `{var}` 占位）；**create 直落 server 内置 `DEFAULT_VLLM_RUN_TEMPLATE` 常量快照**（`creation.py`，无模板表/CRUD/template_key）；worker 渲染 port/model_path/gpu_indexes 后 docker run；存量实例（template 为空）由 worker 用内置默认模板兜底 |

**模板变量占位符表**（`template` 内可用的占位符，worker 渲染时替换）：

| 占位符 | 含义 | 取值说明 |
|---|---|---|
| `{name}` | 容器名 | `vllm-{instance_id}` |
| `{image}` | 容器镜像 | `WORKER_VLLM_IMAGE` |
| `{vllm_bin}` | 容器内命令 | **存量模板兼容保留，默认模板不再使用**——`vllm serve` 由镜像 ENTRYPOINT 承担（官方 vllm-openai GPU/CPU 镜像 `ENTRYPOINT=["vllm","serve"]`）；仅旧格式模板渲染该键（`build_context` 仍填充，不 KeyError） |
| `{model_path}` | 模型路径 | `resolve_model_path` 解析后的路径 |
| `{port}` | 服务端口 | worker 分配的端口 |
| `{gpu_indexes}` | GPU 索引 | 逗号分隔字符串；**存量模板兼容保留**（默认模板经 `{gpus_args}` 使用，不再直接拼 `--gpus device=`） |
| `{net_args}` | 结构性网络片段 | 按 `gpu_indexes` 是否为空派生：GPU → `--network host`；CPU → `-p {port}:{port}`（colima 端口映射，宿主机 `127.0.0.1:{port}` 可达） |
| `{gpus_args}` | 结构性 GPU 片段 | GPU → `--gpus device={gpu_indexes}`；CPU → 空串 |
| `{shm_size}` | 共享内存 | `WORKER_VLLM_SHM_SIZE_GIB` + `g`（如 `16g`） |
| `{args}` | vLLM serve 参数片段 | `--task` 映射 / 缺省补全后的启动参数 |
| `{env_args}` | 环境变量片段 | `-e` 注入（OMP_NUM_THREADS / SAFETENSORS_FAST_GPU / VLLM_CACHE_ROOT；OMP/SAFETENSORS 仅 GPU 实例注入） |
| `{mount_args}` | 挂载片段 | `-v` 注入（模型根 + 缓存同路径） |

> **防漂移注记**：server 端 `DEFAULT_VLLM_RUN_TEMPLATE` 常量（`creation.py`）与
> worker 内置默认模板**须保持一致**——create 直落该常量快照下发（无模板表兜底
> 路径）。Phase B 落 worker 侧 docker 渲染后核对两边字符串，后续改动须同步
> （防漂移测试 `test_default_template_matches_worker` 回归锁定）。

> **信任边界（S11）**：模板为**内置常量快照**（server 端 `DEFAULT_VLLM_RUN_TEMPLATE`
> 常量直落下发），**非用户输入**——用户只填 `spec` 字段（model_name/task/args 等），
> 不直接触碰模板；server↔worker 双向 Bearer 鉴权保护下发链路。模板含 docker 任意
> flag 能力（如 `--privileged`/`-v` 任意路径），改动需代码评审同步 server/worker
> 两侧常量；worker 渲染仅做 `shlex` 安全切词（防注入），不对模板内容做白名单约束。

> **模板约束（S1）**：**禁用 `-d`/`--detach`**——worker 以 docker CLI 进程存活判定容器
> 存活（快速失败逻辑依赖前台 attach：CLI 退出 = 容器结束）；detach 后 CLI 立即退出会
> 被误判 error，且容器脱离 worker 管控。同理避免 `--restart` 系列（容器自重启绕过
> worker 退避计数与对账）。

**CPU 模式（显式加速器声明）**：

- `accelerator=cpu` 节点（worker `WORKER_ACCELERATOR=cpu` 上报）→ allocator 分配
  `gpu_indexes=[]`、`allocated_vram={}`（tp>1 拒绝）；**GPU / 未声明 / `gpu_devices`
  为空一律 fail-closed 拒绝**——不按空 `gpu_devices` 推断 CPU（GPU 节点采集降级也会
  上报空列表，推断会 fail-open 误分配）。
- worker 渲染：`gpu_indexes` 为空 → `{net_args}="-p {port}:{port}"`（colima 下宿主机
  `127.0.0.1:{port}` 可达）、`{gpus_args}=""`（无 `--gpus`）；健康检查不变
  （`GET /v1/models` 1s 200）。
- **CPU 启动参数**（`--enforce-eager --dtype float32 --max-model-len 4096`）**经用户
  `args` 透传**，模板不内置（arm64 CPU 实测必要参数：跳过 torch.compile、避开 bf16
  oneDNN 卡死、控制 KV cache 内存，见 `docs/local-testing.md` §4）。
- **镜像需 CPU 版**（`WORKER_VLLM_IMAGE`，如 `vllm/vllm-openai-cpu:latest-arm64`）；
  CPU 实例**不注入** `OMP_NUM_THREADS=1`/`SAFETENSORS_FAST_GPU=1`（注入会把推理锁单
  线程，性能极差；GPU 行为不变）。

**task → `--task` 映射表**（产品层分类 → vLLM 实际枚举）：

| 产品层 `task` | vLLM `--task` | 说明 |
|---|---|---|
| `auto` | 不注入 | vLLM 自动推断（缺省 generate） |
| `llm` | 不注入 | 产品层显式 LLM 标记，行为等价 auto（不注入） |
| `embedding` | `--task embed` | **勿用弃用别名 `embedding`** |
| `rerank` | `--task score` | cross-encoder 重排；vLLM 版本演进（如 0.21+ 移除 score）时按实际枚举映射 |

`args` 已含 `--task` 时不重复注入（用户优先）。

**worker 执行流程**（步骤化；参考 gpustack `serve_manager.py:_start_model_instance`，
Phase B 容器化后走 docker run 模板渲染）：

1. **启动前显存校验**：pynvml 读取目标卡空闲显存 ≥ `vram_claim`，不满足**拒启**并回
   `state=error` + 可读 `state_message`（防超卖双保险——server allocator 已记账，此处兜底）。
2. **端口分配**：socket 探测空闲端口 + 内存集合幂等（已分配直接复用；参考
   `serve_manager.py:_assign_ports`：`_port_lock` + `_assigned_ports` 集合）。
3. **参数组装（用户优先）**：扫用户 `args`，缺什么补什么——`--port`（分配的端口）、
   `--served-model-name`（= `model_name`）、`--gpu-memory-utilization`（缺省补 GMU）；
   `tensor_parallel_size > 1` 补 `--tensor-parallel-size`（参考
   `vllm.py:extend_args_no_exist` + `get_auto_parallelism_arguments`）。
   **GPU 透传**：经模板 `{gpus_args}` 渲染为 `--gpus device={gpu_indexes}`（GPU 节点；
   CPU 节点该片段为空串）——容器内 CUDA 索引 = 宿主索引，**不再注入
   `CUDA_VISIBLE_DEVICES`**（渲染值见上方占位符表）。
4. **env 注入**：`OMP_NUM_THREADS=1`、`SAFETENSORS_FAST_GPU=1`、
   `VLLM_CACHE_ROOT`（worker 持久目录）——经模板 `{env_args}` 占位渲染为 `-e` 参数
   （参考 `vllm.py:_get_configured_env`）。
5. **Popen(docker run, 无 shell)**：`template.format_map(context)` 渲染 → `shlex.split`
   切 argv（**list 直传 Popen，不经 shell**）；docker run 前台 + `stdout` 重定向实例日志；
   模板缺省（template=None，含存量实例）用 worker 内置默认模板兜底。
6. **健康检查**：`GET /v1/models` 单次 1s 超时 200 → `running`（**GPU host 网络 /
   CPU `-p` 端口映射均以 `127.0.0.1:{port}` 直连容器**——CPU 模式 colima 端口映射后
   宿主机可达）；docker CLI 已退出（`poll() != None`，容器必然
   结束——镜像缺失/拉取失败等立即退出）→ 直接落 `error` + 退出码诊断；starting 超时
   （连续失败 ≥`WORKER_STARTUP_FAIL_THRESHOLD` 次，默认 2，5s sync 周期 ×2 ≈10s；
   或总时长超 `WORKER_STARTUP_TIMEOUT_SECONDS`，默认 30s；**CPU 模型加载 30-60s
   需调大，本机实测 12 / 90**）→ `error`
   （参考 `serve_manager.py:is_ready`；阈值默认值同 §3）。
7. **启动失败**：try/except 显式落 `error` + `state_message`（可读原因；docker CLI 缺失/
   Popen 即抛 → `retryable=False` 永久性错误；参考
   `serve_manager.py:_start_model_instance` except 分支写 ERROR + state_message）。

### 2.3 `POST /instances/{instance_id}/stop`

- **鉴权**：Bearer。
- **请求体**：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `instance_type` | string | 是 | 固定 `"vllm"` |

- **执行流程**：`docker stop -t 3`（优雅终止，3s 宽限）→ 超时/失败 `docker kill` →
  `docker rm` 清理容器；**绝不 killpg docker CLI 进程**（killpg 只杀 CLI 不杀容器，
  容器变孤儿）；随后清 worker 内存状态（容器/端口/退避计数）。幂等覆盖「容器不存在」
  场景（stop/kill/rm 对不存在容器均容错忽略）。
- **docker 不可用不谎报停止**：`stop_container` 返回 `bool`——`docker` 命令无法执行
  （缺失/超时/daemon 不可达）时停止结果未知，`stop()` **恢复调用前状态、保留记录与
  meta、返回非 2xx**（500），绝不静默清除记录让实例从上报中消失（否则 server 误判
  stopped → 容器成孤儿 + 显存账本漂移）；由 server 对账限次重下发兜底。
- **幂等**：重复 stop（实例不存在/已停止/容器不存在）应返回 200 幂等，不报错。
- 响应语义：2xx = 指令已受理；非 2xx = 停止未确认（server 对账重下发兜底）。

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
  （仅恢复 running 容器）、**不自动重启 stopped 实例**；从 stopped 重新拉起的
  唯一入口是 server start 指令（`target=starting`）——与 server 侧 M2 对账守卫长期一致
  （worker 若自作主张恢复/重启 stopped 实例并报 running，server 在无 target 意图时会
  静默忽略其非 stopped 上报，产生状态分叉）。
- **恢复（restore）**：`docker inspect` 容器状态判定——`State.Status == running`
  才恢复（复用 meta 中的端口/GPU/模板快照），**仅恢复 running**；探针异常（docker
  命令缺失/daemon 不可达/超时/输出损坏）≠ 容器消失，**保留 meta + 告警**待下次重试
  （防 docker 短暂不可用时误删 meta → 实例被对账 stopped → 显存超卖）；容器不存在/
  Exited/dead → 删 meta（不恢复）。
- **健康检查**：进程存活（docker CLI pid）&& `GET /v1/models` 200（单次 1s 超时）→
  `running`（GPU host 网络 / CPU `-p` 端口映射均 `127.0.0.1:{port}` 直连容器）；
  docker CLI 已退出 → 容器必然结束，直接判 error；**starting 超时总预算**：连续健康
  检查失败 ≥`WORKER_STARTUP_FAIL_THRESHOLD` 次（默认 2，5s sync 周期 ×2 ≈10s 快速
  失败）或自启动起总时长超 `WORKER_STARTUP_TIMEOUT_SECONDS`（默认 30s，兜底模型加载
  超长时）判 `error`（**CPU 模型加载 30-60s 需调大，本机实测 12 / 90**；默认值需 ≥
  模型加载通常耗时）（参考 `serve_manager.py:is_ready`）。
- **指数退避重启**：**首次失败立即重试（delay=0）**，之后
  `delay = min(10·2^(n-1), 300s)`（n 为已退避次数，封顶 300s），`restart_count` 随上报
  递增（server 记录）；error 且允许重启 → 延迟后置回 `starting`
  （参考 `serve_manager.py:_restart_error_model_instance`：`min(10·2^(n-1), 300)`）。
  **重启「允许」条件**：无次数上限；收到 stop 指令后清退避计数；与 server 手动 start
  并发触发时依赖 start 幂等 200 兜底防双启动；**docker CLI 缺失（Popen 抛
  FileNotFoundError）→ `retryable=False`**（永久性，不再自动重启）；
  **拉起前 `_launch_container` 入口无条件 `stop_container` 清理同名残留容器**
  （docker CLI 存活 ≠ 容器存活：容器 crash 后 CLI 退出、残留 Exited 容器 → 同名
  `docker run` 冲突无限循环；统一先 stop → rm 再拉起，同时覆盖原 pre-kill）。
- **日志**：`{log_dir}/instances/{instance_id}.{restart_count}.log` 按重启次数轮转，
  崩溃可回看上一轮（参考 `serve_manager.py:_get_numbered_log_path`：`{id}.{restart_count}.log`）。
- **端口回填**：worker 分配端口后经 `/instances/report` 的 `port` 字段回传 server
  （server 端 `apply_report` 仅上报非 None 时更新 port）。
- **与 server 对账联动**：worker 无主动拉取，只上报；server 侧节点失联时对
  `running` 实例置 `unreachable`（不删除，人工介入），恢复后对账拉回；
  `target=stopping` 但上报仍非 stopped → server 限次重下发 stop（见 §1.3 规则 3）。

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
  - `stop` 幂等：重复 stop 返回 200，不报错（**含容器不存在**——stop/kill/rm 对
    不存在容器均容错忽略）。

## 5. 配置项（server 侧下发/约束）

| 配置 | 默认值 | 说明 | worker 实现是否需要 |
|---|---|---|---|
| `WORKER_DEFAULT_PORT` | 8100 | worker 默认监听端口（注册可覆盖） | 是（默认端口） |
| `NODE_HEARTBEAT_GRACE_PERIOD` | 30s | server 存活超时阈值（→ offline） | 否（server 侧判定） |
| `NODE_PROBE_INTERVAL` | 15s | server 主动 `/healthz` 探测周期 | 否（server 侧任务） |
| `NODE_PROBE_TIMEOUT` | 3s | `/healthz` 探测超时 | 否（server 侧任务） |
| `INSTANCE_REQUEST_TIMEOUT` | 15s | server→worker 指令转发超时 | 否（server 侧转发） |
| `INSTANCE_WEIGHT_TIMEOUT` | 15s | 权重广播查询超时 | 否（server 侧转发） |
| `INSTANCE_DEFAULT_GMU` | 0.9 | 实例默认显存利用率 GMU（0-1） | **是（缺省补 GMU 参数）** |
| `WORKER_VLLM_BIN` | `vllm` | **历史保留字段**：不再参与默认模板渲染（A1 整改——官方镜像 `ENTRYPOINT=["vllm","serve"]` 承担 `vllm serve`）；`build_vllm_command` 仍读取该值，自定义镜像场景本期不支持 | **是（build_vllm_command）** |
| `WORKER_VLLM_IMAGE` | `vllm/vllm-openai:latest` | vLLM 容器镜像（渲染 `{image}`） | **是（docker run 镜像）** |
| `WORKER_VLLM_SHM_SIZE_GIB` | 10.0 | 共享内存 GiB（渲染 `--shm-size {shm_size}`，vLLM 大模型加载需要） | **是（渲染 {shm_size}）** |
| `WORKER_ACCELERATOR` | `gpu` | 加速器类型（`gpu`/`cpu`）；`cpu` 为 CPU 集成测试显式声明，默认 `gpu` 保持 GPU 主战场 fail-closed | **是（status 载荷上报 `accelerator`）** |
| `WORKER_STARTUP_FAIL_THRESHOLD` | 2 | starting 连续健康检查失败阈值（5s sync 周期 ×N ≈ 快速失败） | **是（starting 判 error）** |
| `WORKER_STARTUP_TIMEOUT_SECONDS` | 30 | starting 总预算秒数（模型加载超长兜底；CPU 需调大，实测 90） | **是（starting 判 error）** |

worker 实现时需要感知：`WORKER_DEFAULT_PORT`（默认监听端口）、`INSTANCE_DEFAULT_GMU`
（启动参数组装缺省 GMU）、`WORKER_VLLM_IMAGE`（容器镜像）、`WORKER_VLLM_SHM_SIZE_GIB`
（共享内存）、`WORKER_VLLM_BIN`（**历史保留字段（A1）**：不再参与默认模板渲染——
官方镜像 ENTRYPOINT 承担 `vllm serve`；`build_vllm_command` 仍读取该值作为
`[vllm_bin, serve, model_path]` 前缀，自定义镜像场景本期不支持）。其余为 server 侧
约束/任务，worker 只需遵守端点契约。
## 6. 定案回写（worker 实现已落地）+ 真待定项

### 已定案（worker 实现落地，见 backend/app/worker/）

| 项 | 定案 | 实现位置 |
|---|---|---|
| 模型路径解析 | `WORKER_MODEL_ROOT/{model_name}`（绝对路径/含分隔符直用）；找不到目录 → `/models/weight` 返 404（server 广播跳过该节点）、start 前校验失败 → ERROR record；**根外绝对路径（容器挂载不可见）→ 拒启** | `process_utils.py:resolve_model_path`、`lifecycle.py:start`/`_launch_container` |
| start 重复下发幂等 | **200 幂等**（已存在 record 直接返回 accepted，不重建） | `lifecycle.py:start` |
| stopping 过渡期上报 | worker 内存态有 STOPPING 但 `snapshot_for_report` 跳过；stop 完成清内存记录 → report 消失 → server 收敛 stopped | `lifecycle.py:snapshot_for_report` / `stop` |
| GPU 优雅降级 | pynvml 不可用/无 GPU → 空 `gpu_devices` + 告警一次，绝不抛异常 | `collector.py:_collect_gpu_devices` |
| docker 启动渲染 | 模板 `format_map` → `shlex.split` → argv 直传 Popen（**无 shell**）；`{args}`/`{env_args}`/`{mount_args}` 片段经 shlex.join 预 quote；用户可控占位值经 shlex.quote 防御；`{net_args}`/`{gpus_args}` 为结构性差异片段（按 `gpu_indexes` 派生，见下） | `process_utils.py:render_docker_command`/`build_context` |
| ENTRYPOINT 契约（A1） | 官方 vllm-openai 镜像（GPU/CPU）`ENTRYPOINT=["vllm","serve"]`；默认模板去 `{vllm_bin} serve`（`docker run ... {image} {model_path} {args}`，由镜像 ENTRYPOINT 承担），防叠加实执行 `vllm serve vllm serve <path>` → exit 2；`{vllm_bin}` 键仅存量模板兼容保留；worker `_launch_container` args 片段从 model_path 之后定位；CLI 退出码 2（argparse 契约错误）→ retryable=False | `process_utils.py:DEFAULT_VLLM_RUN_TEMPLATE`/`build_context`、`creation.py:DEFAULT_VLLM_RUN_TEMPLATE`、`lifecycle.py:_launch_container`/`sync` |
| 容器停止 | `docker stop -t 3` → 超时/失败 `docker kill` → `docker rm`；**绝不 killpg docker CLI**（杀 CLI 不杀容器致孤儿）；stop/rm 对不存在容器容错；`stop_container` 返回 `bool`，docker 命令不可用（停止结果未知）→ `stop()` 恢复调用前状态、保留记录/meta、返回 500 不谎报停止（server 对账重下发兜底） | `process_utils.py:stop_container` / `lifecycle.py:stop` |
| 停止悬挂收敛 | server `apply_report`：`target=stopping` 且上报非 stopped → 限次重下发 stop（`target_retry_count` ≤3，超限 `state_message` 告警、保持 target 不自动收敛）；start/stop 重置计数 | `service/instance.py:apply_report`、`base.py:InstanceLifecycleMixin.target_retry_count` |
| 转发错误码透传 | server 全局 handler 透传 worker 非 2xx 的 `status_code`（401/403→502 防前端误判）；start/create 转发处先 `except HttpClientException: raise` 再包装传输错误 | `main/exceptions.py:http_client_exception_handler`、`service/instance.py:start`、`service/creation.py` |
| restore 端口策略 | meta json（port/gpu_indexes/spec/model_path）+ `docker inspect`（State.Status==running）判定，**仅恢复 running** 复用原端口；探针异常保留 meta + 告警，容器不存在/非 running 删 meta | `lifecycle.py:restore` |
| 加速器显式声明 | `WORKER_ACCELERATOR`（`gpu`/`cpu`）经 status 载荷 `accelerator` 字段上报（`collector.py`）；server `NodeStatus.accelerator`/`WorkerResource.accelerator` 显式字段，allocator **仅 `accelerator == "cpu"` 走 CPU 分配**（`gpu_indexes=[]`、`allocated_vram={}`、tp>1 拒绝）；GPU/未声明/`gpu_devices` 空 → fail-closed 拒绝 | `collector.py:collect`、`node/schema.py:NodeStatus`、`allocator/schema.py:WorkerResource`、`allocator/service.py:_try_worker`、`service/creation.py` |
| CPU 模板变体（net_args/gpus_args） | `DEFAULT_VLLM_RUN_TEMPLATE` 含 `{net_args}`/`{gpus_args}` 结构性片段：`gpu_indexes` 非空 → `--network host` + `--gpus device={gpu_indexes}`；空 → `-p {port}:{port}` + 空串（colima 端口映射宿主机可达，健康检查不变）；`{gpu_indexes}`/`{port}` 键仍保留（存量模板快照兼容） | `process_utils.py:build_context`、`lifecycle.py:_launch_container`、`creation.py:DEFAULT_VLLM_RUN_TEMPLATE` |
| CPU 不注入 OMP/SAFETENSORS | `gpu_indexes` 为空（CPU 实例）**不注入** `OMP_NUM_THREADS=1`/`SAFETENSORS_FAST_GPU=1`（注入会把推理锁单线程，性能极差）；GPU 行为不变；`VLLM_CACHE_ROOT` GPU/CPU 都保留 | `lifecycle.py:_launch_container` |
| 模板快照 | start 指令 `StartRequest.template`（server `_build_start_payload` 顶层下发）→ worker 存入 `spec["template"]` 随 meta 持久化；退避重启/restore 复用同一模板；**create 直落 server 内置 `DEFAULT_VLLM_RUN_TEMPLATE` 常量快照（`creation.py`，含 `{net_args}`/`{gpus_args}` 结构性片段，无模板表/CRUD/template_key）**，存量实例缺省用 worker 内置默认模板兜底 | `service/creation.py`、`lifecycle.py:start`/`_launch_container` |
| starting 超时双阈值配置化 | `WORKER_STARTUP_FAIL_THRESHOLD`（默认 2）/`WORKER_STARTUP_TIMEOUT_SECONDS`（默认 30）升配为 `WorkerConfig` 字段；CPU 模型加载 30-60s 需调大（本机实测 12 / 90） | `worker/config.py`、`lifecycle.py:sync` |
| 退避重启 | 首次 delay=0，之后 `min(10·2^(n-1), 300)`；拒启类（retryable=False）不自动重启；docker CLI 缺失（FileNotFoundError）→ retryable=False 永久化；**拉起前入口无条件 stop_container 清理同名残留容器**（防 Exited 残留 → 同名 docker run 冲突死循环，同时覆盖原 pre-kill） | `lifecycle.py:_maybe_restart`/`_launch_container` |

### 真待定（worker 实现时细化）

- **GPU 上报 dict 细节**：`filesystem`/`os`/`kernel`/`uptime` 已简化实现
  （`collector.py`），结构可按需增补（当前：filesystem=[{name,mount_point,total,used}]、
  os={name,version}、kernel={release,version}、uptime={seconds}）。

> **升级注意（server 侧 schema）**：`vllm_manager_instance` 表新增 `target_retry_count`
> 列（P0 整改）。项目无迁移链（`create_all` 仅建新表，sqlite 测试自动建表无影响）；
> **升级需重建该表**——否则 PG 上实例域所有查询（含 list/详情）会因 column does not
> exist 报错，且错误信息有误导性。
