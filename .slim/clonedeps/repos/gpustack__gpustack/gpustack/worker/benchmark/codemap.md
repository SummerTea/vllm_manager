# gpustack/worker/benchmark/

## Responsibility

**Benchmark Runner**：在工作节点上以容器方式执行模型压测（基准测试）。`runner.py` 中的 `BenchmarkRunner` 在 `BenchmarkManager` 派生的子进程中运行，对已运行的模型实例端点发起恒定速率压测并把结果写回服务器。

## Design

- 子进程隔离：`BenchmarkManager._launch_benchmark` 用 `multiprocessing.Process`（daemon=False）启动 `BenchmarkRunner`，日志经 `RedirectStdoutStderr` 写入 `{log_dir}/benchmarks/{id}.log`；与 ServeManager 启动实例的模式一致。
- 基于快照启动：`BenchmarkRunner.__init__` 从 `benchmark.snapshot.instances[model_instance_name]`（`ModelInstanceSnapshot`）读取 `resolved_path`、`worker_ip`、`ports[0]`，拼出 `_model_endpoint = http://{worker_ip}:{ports[0]}` 作为压测目标，与实例生命周期完全解耦。
- 容器化执行：`_create_workload` 用 `config.benchmark_image_repo` 镜像 + `benchmark-runner` 命令构造 `WorkloadPlan`（`host_network=True`，shm 10GiB，privileged=False），挂载模型目录与 `benchmark_dir` 输出目录，`create_workload` 启动。
- 进度回写：压测进程经 `--progress-url {server}/v2/benchmarks/{id}/state` + `--progress-auth`（worker_token）实时 PATCH 上报；`_update_benchmark_state` 复用该端点。
- 队列串行：上游 `BenchmarkManager` 用 `deque` + `asyncio.Lock` 维护基准队列，单活跃 benchmark 串行执行（`_benchmark_queue_worker`），避免多个压测争抢 GPU。

## Flow

1. 事件驱动：`BenchmarkManager.watch_benchmarks_event` 监听 `benchmarks.awatch`：
   - PENDING → 入队并置 `QUEUED`（`_enqueue_benchmark`，deque 去重）。
   - STOPPED → `_dump_benchmark_logs_to_file` + `_stop_benchmark`；DELETED → 停止。
2. `_start_benchmark`：起子进程 → `_provisioning_processes[id]` 记录 → patch `RUNNING` + pid。
3. `BenchmarkRunner.start()` → `_build_command_args()` 构造 `benchmark run` 参数：
   - `--target {model_endpoint}`、`--profile constant`、`--rate {request_rate}`、`--sample-requests 0`。
   - `--processor {model_path}`、`--outputs {id}.dual_json`、`--backend openai_http_error_detail`。
   - `trust-remote-code`、`--max-requests` 按模型参数透传。
4. 数据集：`DATASET_SHAREGPT` 用预置 `BENCHMARK_DATASET_SHAREGPT_PATH`；`DATASET_RANDOM` 用 `prompt_tokens=,output_tokens=` 描述（支持 `--random-seed`）。
5. 结果落盘到 `{benchmark_dir}/{id}.dual_json`，由 `BenchmarkManager` 解析为 `GenerativeBenchmarksReport` 后上传服务器。

## Integration

- 上游调用方：`benchmark_manager.py` 的 `BenchmarkManager`（`_start_benchmark` / `_launch_benchmark` / `sync_benchmark_state`）。
- 消费 `gpustack.schemas.benchmark`（`Benchmark`、`BenchmarkStateEnum`、`ModelInstanceSnapshot`、`BenchmarkDeploymentMetadata`、`DATASET_*` 常量）与 `worker/schemas/benchmark_runner.py` 的报告模型。
- 与模型实例解耦：只依赖运行中的实例快照（IP / 端口 / 模型路径），不管理实例生命周期本身；镜像经 `apply_registry_override_to_image` 支持注册表覆盖。
