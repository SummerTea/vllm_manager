# gpustack/worker/

## Responsibility

管控节点（worker）核心包：对应 vllm_manager 的 agent 端。负责在 GPU 机器上完成**实例生命周期管理（Process Manager）**、**节点状态采集与上报（Status Collector）**、**指标导出（Metric Exporter）**，以及模型文件下载、基准测试、运行时指标聚合等旁路能力。worker 不主动轮询服务器，全部通过 watch 事件驱动 + 周期同步。

## Design

- 组合式编排：`Worker`（worker.py）为顶层门面，`start()` → `start_async()` 组装各 Manager：
  - `WorkerManager`（worker_manager.py）：注册（`register_with_server` → `_register_worker`，token 写入 `data_dir/worker_token`）、状态上报（`sync_worker_status`）、版本兼容检查（`check_server_version`，GET `/version`）。
  - `ServeManager`（serve_manager.py）：模型实例启停的核心 Process Manager。
  - 卫星组件：`BenchmarkManager`、`WorkloadCleaner`、`ModelFileManager`、`InferenceBackendManager`、`ToolsManager`、`RuntimeMetricsAggregator`、`MetricExporter`。
- 事件驱动 + 周期性对账双轨：`_clientset.model_instances.awatch(callback=_handle_model_instance_event)` 处理 CREATED/UPDATED/DELETED；`sync_model_instances_state` 周期轮询做权威对账（reap stale instances、回写 RUNNING/ERROR），并支持 `MODEL_INSTANCE_STATE_RECONCILE_INTERVAL` 强制对账兜底。
- 子进程隔离：模型实例经 `multiprocessing.Process(target=ServeManager._serve_model_instance)` 启动（daemon=False），stdout/stderr 经 `RedirectStdoutStderr` 重定向到 `{log_dir}/serve/{id}.{restart_count}.log`；`_provisioning_processes` 以进程存活与否区分 provisioning/running/failed。
- 端口分配：`_assign_ports` 持全局 `_port_lock`（threading.Lock，规避 pickle 问题），经 `network.get_free_port` 在 `service_port_range` 内取端口，分布式场景额外预留 DP RPC / master / VLLM_PORT / connecting 端口。
- 周期线程统一经 `run_periodically_in_thread` 调度：heartbeat（`WORKER_HEARTBEAT_INTERVAL`）、状态同步（`WORKER_STATUS_SYNC_INTERVAL`）、实例状态同步（`MODEL_INSTANCE_HEALTH_CHECK_INTERVAL`）、推理健康检查（10s 固定循环，按模型 env 逐实例配置）、exporter 启动（15s）、IP 变更检测（15s）、孤儿 workload 清理（120s）、benchmark 状态同步（3s）。

## Flow

1. `Worker.start()` → `start_async()`：`check_server_version` → `_register()`（获取 worker_id / cluster_id / worker_uuid）→ 启动各周期任务与 `_serve_apis()`（uvicorn 起 worker API 服务）。
2. 实例生命周期状态机：服务器置 `SCHEDULED` → worker 收 CREATED 事件 → `_start_model_instance`（`_assign_ports` → 起子进程 → patch 为 `INITIALIZING` + port + pid）→ 子进程内 `VLLMServer.start()` 创建 workload → 子进程退出后由 `sync_model_instances_state` 经 `is_ready()`（HTTP GET 健康检查路径，内置后端默认 `/v1/models`，1s 超时）判定 → `RUNNING`；workload 不存在/不健康 → `ERROR`。
3. ERROR 且 `model.restart_on_error` 的实例缓存在 `_error_model_instances`，由 `watch_model_instances` 循环 `_restart_error_model_instance` 指数退避重启（10s·2^n，上限 300s），重启次数经 `restart_count` + `last_restart_time` 记录。
4. 推理健康检查：`sync_model_instances_inference_health` 对 RUNNING 实例发真实推理请求（`is_inference_ready()`，`/v1/chat/completions` 等），失败达阈值（默认 3 次）置 `ERROR`；近期有成功流量则跳过（`record_successful_inference` 由 worker proxy 回调）。
5. 上报链路：`Worker._heartbeat()` POST `/worker-heartbeat`；`WorkerManager.sync_worker_status()` 将 `WorkerStatusCollector.collect()` 结果 POST 给服务器。

## Integration

- 服务器端依赖：`ClientSet` 全量 REST + watch 接口（models / model_instances / workers / benchmarks / inference_backends）；注册、heartbeat、状态上报走服务器 API。
- worker API 路由（`_serve_apis`）：`route_config`（版本化前缀）、`/debug`、probes、logs、proxy、filesystem、cluster_proxy、`/token-auth`；proxy 中间件经 `app.state.get_instance_port_by_model_instance_id` / `record_successful_inference` 回调 ServeManager。
- `_SERVER_CLASS_MAPPING`（serve_manager.py 顶部）将 `BackendEnum`（VLLM/SGLANG/VOX_BOX/ASCEND_MINDIE）映射到 `worker.backends` 的具体 Server 类，默认 `CustomServer`。
- `MetricExporter`（exporter.py）消费 `WorkerStatusCollector`（`timed_collect`）+ `RuntimeMetricsAggregator` 缓存（`cache["unified"]` / `cache["raw"]`）；`InferenceBackendManager` 缓存被 ServeManager 与各 backend 读取。外部依赖：`gpustack_runtime.deployer`（create_workload/delete_workload/get_workload）、`gpustack_runner`（镜像/版本解析）、`gpustack.detectors`（GPU 检测）。
