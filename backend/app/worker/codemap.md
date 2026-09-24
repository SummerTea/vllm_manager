# backend/app/worker/

## Responsibility（职责）

GPU 机器上的管控节点（独立 FastAPI 进程，:8100，**不连 PG、不初始化任何扩展**，
状态全部在进程内存）。角色是 **vLLM 实例生命周期管理者**：受理 server 下发的
start/stop/weight 指令，拉起/终止 vLLM 子进程，健康检查驱动内存态状态机收敛，
周期性上报节点状态与实例快照。与 server 分工：**server 的 PG 是唯一权威账本**，
worker 只持内存态，经 register/status/report 三端点上报，
由 server `apply_report` 对账收敛状态并清回 `target_state`（闭环收敛）。

模块划分（11 文件 + 入口）：
- `config.py`：`WorkerConfig`（继承 `AppBaseConfig`，独立读 `WORKER_*`，模块级
  `worker_config` 单例 import 即读、frozen 不可写；**不读 `DISABLED_EXTENSIONS`**）
- `enum.py`：`WorkerInstanceStateEnum`：pending/starting/running/stopping/error；
  **STOPPING 为本地过渡态，report 时跳过**（server 枚举无此值会 422）
- `schema.py`：`VllmSpec` + `StartRequest`（+gpu_indexes/vram_claim/+template
  docker 模板快照）+ `StopRequest` + `WeightRequest`/`WeightResponse`；worker
  端点响应不要求 BaseResponse 包装
- `server_client.py`：`ServerClient`（httpx.AsyncClient timeout=15 复用）；`collector.py`：
  `WorkerStatusCollector`（无状态现采，pynvml + psutil；status 载荷上报
  `accelerator`——`WORKER_ACCELERATOR` 配置值 gpu|cpu，供 server 判定 CPU 分配）；
   `exceptions.py`：独立 JSON-only
   异常处理器；`process_utils.py`：端口分配/docker 模板渲染（无 shell）/容器生命周期
   （stop_container/inspect_container）/路径/命令/env 片段（默认模板无
   `{vllm_bin} serve`——镜像 ENTRYPOINT 承担 `vllm serve`）
- `lifecycle.py`：`InstanceLifecycleManager`（核心，内存态状态机）
- `api.py`：`worker_router`（裸路径）+ `require_worker_token`；`main.py`：`create_app`/
  `worker_lifespan`/3 后台循环；入口 `backend/app/work_worker.py`（argparse
  `--host 0.0.0.0`/`--check`，uvicorn factory，**无 `--port`**，端口由
  `WORKER_LISTEN_PORT` 决定）

## Design（设计模式与抽象）

- **内存态状态机**：`WorkerInstanceRecord`（dataclass）持单实例全部内存态
  （state/port/gpu_indexes/spec/restart_count/backoff_count/pid/process/fail_count/
  retryable），`_instances` 为单进程内权威；状态迁移 `pending → starting →
  running / error`，`stopping` 仅过渡不上报。
- **对账闭环**：worker 只上报不拉取——快照经 `/instances/report` 上抛，server
  `apply_report` 收敛（M2 stopped 防复活守卫 / B1 防 target 被清竞态 / stopping 且
  上报消失→stopped 终态），worker 侧配合：stopped 为终态不自动恢复。
- **幂等受理**：start/stop 重复下发一律 200 accepted（STARTING/RUNNING/STOPPING
  不重建，ERROR record 允许重建以修复拒启实例）；2xx = 指令已受理而非实例已运行。
- **退避重启**：error 且 `retryable=True` → 延迟 `min(10·2^(n-1), 300)`s（首次
  delay=0）置回 starting；**拒启类**（模型缺失/显存不足/Popen 失败/docker CLI 缺失）
  置 `retryable=False` 不自动重启（防绕过显存双保险）；同名容器清理并入
  `_launch_container` 入口（拉起前无条件 stop_container，替代原 pre-kill）。
- **优雅降级**：pynvml 不可用 / 初始化失败 / 无 GPU → `gpu_devices=[]` 且各场景
  仅告警一次，绝不抛异常（`collector.py` 模块级 `_warned_*` 标志）。
- **独立异常处理器**：`exceptions.py` 零 `app.main`/`app.config` import（避免拉入
  server app_config 的 DISABLED_EXTENSIONS 耦合），裸路径端点不走 server HTML 渲染器，
  故独立注册 JSON-only handler。

## Flow（数据与控制流）

- **主循环**（`main.py:worker_lifespan`）：注册（失败重试耗尽 → 启动失败退出）→
  restore running 实例（先于 report）→ 3 个后台任务并行（异常不退出、
  shutdown cancel）：`status_loop`（采集上报，`WORKER_STATUS_INTERVAL`，承担
  存活语义）/ `report_loop`（实例对账，`WORKER_REPORT_INTERVAL`，**空快照也
  必须上报**——server 依赖「上报消失」分支收敛 target=stopping 实例，跳过则
  单实例 stop 后消失信号丢失）/ `sync_loop`（状态机权威判定，5s）
- **实例生命周期**（`lifecycle.py:start` 受理 7 步，契约 §2.2）：
  ① 模型路径解析（`resolve_model_path`：绝对路径直用 / 相对拼 `WORKER_MODEL_ROOT`
     **（含子目录式**，如 Qwen/Qwen3-0.6B → {root}/Qwen/Qwen3-0.6B）；目录不存在 →
     ERROR + retryable=False；根外绝对路径容器挂载不可见 → 拒启）
  ② 显存校验（`_check_vram`，目标卡空闲 ≥ vram_claim，无 GPU 不拦截；不足拒启）
  ③ 端口分配（`get_free_port` 排除 `_assigned_ports`，幂等防复用）
  ④ 参数组装（`build_vllm_command`：用户 args 优先，`_has_arg` 去重——args 已含
     `--key`/`--key=value` 则不注入；缺省补 `--port`/`--served-model-name`/GMU/
     `--tensor-parallel-size`（tp>1 才补）；task 映射：embedding→`--task embed`、
     rerank→`--task score`、auto/llm 不注入）
   ⑤ env 注入（`build_env_args` 生成 `-e` 片段：OMP_NUM_THREADS/SAFETENSORS_FAST_GPU/
      **仅 `gpu_indexes` 非空（GPU 实例）注入**——CPU-only 注入会把推理锁单线程；
      VLLM_CACHE_ROOT GPU/CPU 都保留）+ 挂载片段（`-v` 模型根/缓存同路径）
   ⑥ 模板渲染（`render_docker_command`：`format_map` → `shlex.split` → **argv 直传
     Popen 无 shell**，docker run 前台 + stdout 日志重定向；缺省 template 用内置默认
     兜底）+ 写 meta json（restore 用）；**默认模板无 `{vllm_bin} serve`**——镜像
     ENTRYPOINT 承担 `vllm serve`（A1 整改，叠加会实执行 vllm serve vllm serve），
     `{vllm_bin}` 键仅存量模板兼容保留（build_context 仍填充）；**结构性差异片段
     `{net_args}`/`{gpus_args}` 按 `gpu_indexes` 空否派生**：GPU → `--network host`
     + **内引号版 `--gpus '"device=0,1"'`**（A800 实测支撑：shlex.split 后 argv 值
     含字面双引号，nvidia 官方多卡 workaround，tp>1 双卡识别，单卡行为不变；**无
     CUDA_VISIBLE_DEVICES**），CPU → `-p {port}:{port}` + 空串（CPU 三参数
     --enforce-eager 等走用户 args 透传）；
      ⑦ 失败 try/except 显式落 ERROR +
      state_message（docker CLI 缺失 → retryable=False）
   → `sync_loop` 周期判定：docker CLI 已退出（poll 非 None，容器必然结束）→ 直接
   error + 退出码（code∈{2,125,126,127} → retryable=False 永久化：2=CLI 参数契约
   错误，vLLM argparse 恒 exit 2；125=docker daemon 错误/镜像缺失；126/127 不可
   执行/命令不存在；其余业务退出码保持 retryable=True 可退避恢复）；健康检查（host
   网络 `127.0.0.1:{port}` 直连 `/v1/models` 1s 200）：starting 连续失败 ≥
   `WORKER_STARTUP_FAIL_THRESHOLD`（默认 2，5s 周期×N≈10s 快速失败）或总时长超
   `WORKER_STARTUP_TIMEOUT_SECONDS`（默认 30s）→ error「启动超时」；running 失活
   连续 ≥2 → error「进程失活/健康检查失败」；error → 退避重启（拉起前入口无条件
   stop_container 清理同名残留）。
  → `report_loop` 快照上报（跳过 stopping）→ server `apply_report` 对账收敛
  （M2 守卫 / B1 竞态 / stopping+消失 → stopped 终态）。
- **停止/恢复/权重**（`lifecycle.py`）：
  - `stop`：幂等（不存在 → accepted）；`stop_container`（`docker stop -t 3` → 超时
    `docker kill` → `docker rm`，**不 killpg docker CLI**——杀 CLI 不杀容器致孤儿）→
    清记录/端口/meta → report 消失 → server 收敛 stopped；**docker 不可用绝不谎报
    停止**——stop_container False（命令缺失/超时/daemon 不可达，停止结果未知）→
    恢复原状态 + 保留记录/meta/端口 + 抛 ExternalServiceException（500，server
    感知后对账限次重下发 stop），防实例从对账消失被收敛 stopped → 显存账本漂移。
  - `restore`：扫描 `{log_dir}/instances/*.json`，**仅恢复 running**（`docker inspect`
    `State.Status == running` 复用原端口；探针异常保留 meta + 告警，容器不存在/非
    running/损坏 meta → 删文件，stopped 不恢复——唯一拉起入口是 server start 指令）。
  - `weight`：`resolve_model_path` + `_sum_weight_files`（os.walk 递归
    `.safetensors/.bin/.pt/.pth` 求和）→ `{weight_bytes}`；目录不存在 →
    `NotFoundException` 404（server 广播当该节点失败跳过）。

## Integration（集成点）

- **与 server 双向契约**（`docs/worker-contract.md`）：
  - worker→server（`server_client.py`，路径带 `/vllm_manager/api/v1`，响应
    `BaseResponse{code,message,data}`）：§1.1 `POST /nodes/register`（tenacity
    `stop_after_attempt(10) × wait_fixed(3)` 仅重试 `httpx.TransportError`/
    `TimeoutException`，HTTPStatusError 立即抛——4xx/5xx 不浪费 30s 窗口）；
    §1.2 `/nodes/{id}/status`、§1.3 `/instances/report` 长驻容错（失败仅 warning）。
  - server→worker（`api.py`，**裸路径无前缀**，`build_worker_url` 直接拼
    `http://{advertise_address|ip}:{worker_port}/{path}`）：§2.1 `GET /healthz`
    （Bearer 豁免）；§2.2 `POST /instances/{id}/start`、§2.3 `POST /instances/{id}/stop`、
    §2.4 `POST /models/weight`（除 healthz 外全部注入 `Depends(require_worker_token)`，
    `secrets.compare_digest` 恒时比对 `app.state.node_token` → 401）。
- **依赖**：仅 `app.worker` 内部 + `app.exception`（process_utils 抛
  `NotFoundException`）+ `app.base.base_config`/`base_enum`；**无 server 域 import、
  无 DB/Redis/SAQ 扩展**（懒加载扩展一个不初始化，无需 `DISABLED_EXTENSIONS`）。
- **配置**：`WorkerConfig`（`.env` 读 `WORKER_*`）：`SERVER_URL`(http://127.0.0.1:8000)/
  `WORKER_LISTEN_PORT`(8100)/`WORKER_MACHINE_ID`/`WORKER_ADVERTISE_ADDRESS`/
  `WORKER_MODEL_ROOT`/`WORKER_VLLM_BIN`(vllm，**历史保留字段 A1**：不再参与默认
  模板渲染，镜像 ENTRYPOINT 承担 vllm serve；build_vllm_command 仍读)/`WORKER_VLLM_IMAGE`(vllm/vllm-openai:latest)/
  `WORKER_VLLM_SHM_SIZE_GIB`(10.0)/`WORKER_LOG_DIR`/`WORKER_CACHE_DIR`/
  `WORKER_STATUS_INTERVAL`(15)/`WORKER_REPORT_INTERVAL`(5)/
  `INSTANCE_DEFAULT_GMU`(0.9)/`WORKER_SYSTEM_RESERVED_RAM|VRAM`/
  `WORKER_ACCELERATOR`(gpu，显式声明 gpu|cpu)/`WORKER_STARTUP_FAIL_THRESHOLD`(2)/
  `WORKER_STARTUP_TIMEOUT_SECONDS`(30，CPU 需调大)。
- **参考实现**：gpustack `serve_manager.py`（生命周期/退避/端口/日志编号）、
  `backends/vllm.py`（命令/env 组装）——容器路径 psutil 递归树/`terminate_process_tree`
  已移除（停止走 docker stop/kill/rm）。
