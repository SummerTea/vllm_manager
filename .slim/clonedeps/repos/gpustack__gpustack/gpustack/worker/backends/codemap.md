# gpustack/worker/backends/

## Responsibility

**Strategy 模式**的推理后端实现层：为每个推理引擎（vLLM / SGLang / Ascend MindIE / vox-box / 自定义命令）提供统一的"启动一个模型实例"抽象。对应 vllm_manager agent 端"启停 vLLM 进程"的直接参考。

## Design

- 抽象基类 `InferenceServer(ABC)`（base.py）定义契约：构造函数统一完成模型获取（`get_model`，含 `{data_dir}` 替换）、worker 获取、模型文件就绪等待、镜像解析与命令参数构建的公共逻辑；子类只需实现 `start()` 与参数构建钩子。
- 子类分发：`ServeManager._SERVER_CLASS_MAPPING` 按 `BackendEnum` 映射到 `VLLMServer`（vllm.py）、`SGLangServer`（sglang.py）、`AscendMindIEServer`（ascend_mindie.py）、`VoxBoxServer`（vox_box.py）、`CustomServer`（custom.py）。
- 容器化部署：所有 backend 经 `gpustack_runtime.deployer.create_workload(WorkloadPlan)` 启动容器（`host_network=True`，shm 默认 10GiB，`ContainerRestartPolicyEnum.NEVER`），GPU 按 `_get_configured_resources` 以 gpu_indexes 注入资源键；`_transform_workload_plan` 对 Docker 部署器做注册表覆盖转换。
- 版本门控参数注入：`_build_command_args` 用 `compare_versions` 按 `backend_version` 决定注入：
  - vLLM `get_cache_report_arguments` ≥0.9.0.1 注入 `--enable-prompt-tokens-details`；`get_access_log_arguments` ≥0.16.0 注入 `--disable-access-log-for-endpoints /metrics`。
  - 注入参数与用户参数经 `_get_injected_backend_parameters` 区分，并回写服务器 `injected_backend_parameters` 字段。
- 分布式执行：vLLM 支持 Ray sidecar 与 mp 多节点两种执行器：
  - Ray 路径：leader 侧附加 `ray-head` sidecar 容器，`_build_ray_configuration` 从 `ray_port_range` 顺序分配 GCS/dashboard/metrics 等 12 个端口。
  - mp 路径：`cal_multinode_topology` 把用户并行度换算为 `dp_only / mp_only / nested` 三种参数形态（`--nnodes/--node-rank/--master-port/--data-parallel-*`），follower 注入 `--headless`。
- 命令参数构建采用模板方法：`_build_command_args` 先构造 `_VLLMArgsContext`（port、is_distributed、executor_backend、topology、is_omni/is_audio 等派生输入），再交给各 `_build_*_arguments` 子步骤，避免重复求值。

## Flow

1. ServeManager 子进程 → `server_cls.start()` → `_start()`：
   - `_get_configured_env()`：注入 `VLLM_HOST_IP`、`NCCL_SOCKET_IFNAME`/`GLOO_SOCKET_IFNAME`、Ascend `HCCL_*`、`VLLM_CACHE_ROOT`（torch compile 缓存）、LMCache `LMCACHE_*`（扩展 KV cache）。
   - `_get_configured_image()`：`_resolve_image` 经 gpustack-runner 按设备 vendor/arch 与 CUDA 版本匹配镜像。
   - `_build_command_args()`：自动并行度 / speculative / LoRA `--lora-modules` / 用户参数 + `--host/--port/--served-model-name`。
   - `_create_workload()`：构造 `Container` + `WorkloadPlan` 并 `create_workload`。
2. `InferenceServer.__init__` 阻塞等待：`_until_model_instance_starting()` 用 `model_instances.watch(stop_condition=_stop_when_starting)` 等到实例进入 `STARTING` 且带 `resolved_path`，从而确定本地模型路径 `_model_path`（也用于 `_get_pretrained_config` 推导 `--max-model-len`）。
3. 失败统一走 `_handle_error()`：主 worker patch 整个实例为 `ERROR`，从属 worker 走 `_update_subordinate_worker_error()` 只 patch `distributed_servers.subordinate_workers[i]`，避免与主 worker 状态管理竞争。

## Integration

- 被 `serve_manager.py` 消费（`_SERVER_CLASS_MAPPING` + `_serve_model_instance`）；`InferenceServer` 通过 `ClientSet` 直接读写服务器（models / model_instances）。
- 依赖 `gpustack.schemas.models`（`ModelInstance`、`ModelInstanceStateEnum`、`BackendEnum`、`SpeculativeConfig`、`ModelInstanceDeploymentMetadata`）、`gpustack_runtime`（deployer / detector / runner）、`gpustack.utils.vllm_topology`（`validate_multinode_topology`）。
- GPU 信息来源：`_get_selected_gpu_devices` 按 `gpu_indexes` + `gpu_type` 从 `collector.py` 采集的 `worker.status.gpu_devices` 过滤，用于资源注入、镜像选择与 Ascend 环境变量判定。
- `base.py` 工具函数供各 backend 复用：`cal_distributed_parallelism_arguments`（各 worker GPU 不均等时回退 pipeline parallelism）、`read_lora_max_rank`、`is_ascend` / `is_ascend_310p`、`_normalize_param_format`（backend 参数格式归一化）。
