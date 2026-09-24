# gpustack/utils/

## Responsibility
gpustack 的通用工具库：为 server（管理端）、worker（agent 端）、scheduler
提供无业务逻辑的纯函数与基础组件。按领域划分：
- 进程与信号管理：`process.py`
- GPU 标识解析：`gpu.py`
- CLI 参数构造：`command.py`
- 硬件/平台探测：`platform.py`
- 网络与端口：`network.py`、`ephemeral_ports.py`
- 模型仓库：`hub.py`、`lora_model_source.py`
- 计量：`resource_usage.py`、`usage_snapshots.py`
- 文件与快照：`file.py`、`snapshot.py`
- 数据库迁移：`sql_enum.py`、`db.py`

## Design
- **`process.py` — 进程树终止基础设施**：基于 psutil 而非 killpg。
  `terminate_process_tree(pid)` 用 `process.children(recursive=True)` 收集
  全部子孙进程，先 `terminate_processes` 终止子进程、再终止父进程。
- 单个进程采用两段式超时策略：先 `process.terminate()`（SIGTERM）→
  `wait(timeout=3)`，超时则 `process.kill()`（SIGKILL）→ 再 `wait(timeout=1)`。
  对应我们 agent 的"SIGTERM → 3s → SIGKILL"约定。
- 模块级 `threading_stop_event` 作为全局停止信号，供周期任务退出循环。
  `add_signal_handlers()` / `add_signal_handlers_in_loop()` 注册 SIGTERM/
  SIGINT（Windows 走同步版，否则用 asyncio loop handler）。
  `shutdown_event_loop` 先 cancel 全部 asyncio task、置位 stop event，
  再走 `handle_termination_signal` 收尾整棵进程树；
  `termination_signal_handled` 标志保证幂等。
- **`gpu.py` — GPU 标识协议**：核心是 `parse_gpu_id` / `make_gpu_id`，
  定义 `worker_name:device:gpu_index` 三元组字符串格式
  （正则 `^(?P<worker_name>.+):(?P<device>[^:]+):(?P<gpu_index>\d+)$`）。
- `group_gpu_ids_by_worker`、`group_gpu_indexes_by_gpu_type_and_worker`
  做调度分组的去重排序；`all_gpu_match` / `any_gpu_match` / `find_one_gpu`
  接收 WorkerBase 或其列表，对 `worker.status.gpu_devices` 施加谓词。
- `compare_compute_capability` 将 "X.Y" 字符串解析为 (major, minor) 后
  数值比较；非法输入按最低版本处理，两个非法视为相等（返回 0）。
- **`command.py` — 后端 CLI 参数工具**：`find_parameter` /
  `find_int_parameter` / `find_bool_parameter` 在扁平 argv 上解析
  `--key=value` 与裸 flag（truthy/falsy 值表 `_TRUTHY_VALUES`）。
- `format_backend_parameters` 把 `--key value` 归一为 `--key=value`，
  多值 flag 保持拆分；`_mask_json_segments` 在 shlex 切分前把 `={...}`
  的 JSON 段替换为 `\x1f` 占位符再还原，防止含空格 JSON 被错误拆分——
  是 vLLM `--config={...}` 等参数安全传递的基础。
- **`platform.py`**：`system()` / `native_arch()` / `arch()` 经
  `_ARCH_ALIAS_MAPPING`（x86_64→amd64、aarch64→arm64）标准化 OS/架构；
  `is_inside_kubernetes()` 通过环境变量 + serviceaccount token 文件判定。
- **`convert.py`**：`safe_int` / `safe_float` / `safe_convert` 兜底类型转换
  （失败返回默认值），被 `detectors/runtime/runtime.py` 用于安全解析 GPU 附录字段。
- **`resource_usage.py`**：k8s quantity 解析（`parse_quantity_to_mib` /
  `parse_quantity_to_millicores`）、GPU 类型从 kueue 队列名解析
  （`parse_gpu_type`）、UTC 日/小时分段（`iter_utc_day_segments` /
  `iter_utc_hour_segments`）等计量纯函数，刻意保持无依赖以便独立单测。
- 其余：`network.py`（`is_port_available`、`get_free_port`）、`task.py`
  （`run_periodically` / `run_periodically_in_thread` / `run_in_thread`）、
  `hub.py`（HuggingFace 模型文件匹配与缓存）、`locks.py`
  （`HeartbeatSoftFileLock`）、`vllm_topology.py`（多节点 TP/PP 拓扑校验）。

## Flow
- 进程终止：`worker/serve_manager.py._stop_model_instance` 或
  `benchmark_manager.py` 拿到 `multiprocessing.Process` 的 pid →
  调 `terminate_process_tree(pid)` → psutil 递归收集 children →
  先子后父逐个 SIGTERM → 3s 未退则 SIGKILL。
- 优雅停机：`server/server.py` / `worker/worker.py` 启动时
  `add_signal_handlers_in_loop()` → 收到 SIGINT/SIGTERM →
  `shutdown_event_loop` cancel asyncio tasks + 置位 `threading_stop_event`
  → `handle_termination_signal` 终止自身及全部子进程。
- GPU 标识：scheduler 构造 `make_gpu_id(worker, device, index)` →
  选卡后 `parse_gpu_id` 拆回三要素 → 按 worker/类型分组后下发给实例。

## Integration
- 被 server、worker、scheduler、api 等几乎所有上层模块导入，无反向依赖
  （仅少量模块依赖 `gpustack.schemas` 的类型，如 `gpu.py`→`WorkerBase`）。
- 与 agent 端最相关的三个模块：
  - `process.py` 被 `worker/serve_manager.py`、`benchmark_manager.py`、
    `worker/worker.py`、`server/server.py` 消费（vLLM 实例启停与进程组清理）；
  - `gpu.py` 被 scheduler 用于 GPU 资源寻址；
  - `command.py` 被 backends（vllm/sglang）用于拼装启动参数。
