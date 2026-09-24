# gpustack/server/

## Responsibility
GPUStack 服务端核心（对应本项目 vllm_manager 的 `manager/` 管理端），职责：

- **启动编排**：`Server`（`server.py`）执行 Alembic 迁移、`init_db`、初始化默认集群/管理员/注册 token，创建 FastAPI 应用（`create_app`，`app.py`），拉起 scheduler、controllers、后台收集任务与子进程（`gpustack-operator`、worker 进程）。
- **控制面协调**：以 controllers（`controllers.py`）为核心，对 Model / ModelInstance / Worker / WorkerPool / Cluster / ModelRoute 等资源做声明式状态协调（controller-runtime 风格）。
- **worker 管理**：心跳接收与批量落库（`worker_status_buffer.py`）、可达性检查（`worker_syncer.py` + `worker_request.py`）、离线实例清理（`worker_instance_cleaner.py`）、向 worker 转发 REST/流式请求（`worker_request.py`）。
- **业务服务层与缓存**：`services.py` 封装各资源 CRUD 并负责缓存失效；`cache.py` 提供 `@locked_cached` 分布式缓存。
- **事件分发**：`bus.py` 全局 `EventBus`（topic 订阅/发布、UPDATED 合并、背压），跨实例经 `coordinator/` 分发。
- **计量/归档**：用量指标缓冲落库（`metrics_collector.py`）、资源事件计量与归档（`resource_event_logger.py`、`resource_usage_collector.py`、`storage_usage_collector.py`、`usage_archiver.py`、`usage_details_archiver.py`）、系统负载（`system_load.py`）。

## Design
- **事件驱动 reconcile**：DB 写入（ActiveRecordMixin）经 `event_bus.publish(topic, Event(type, data, changed_fields))` 广播，各 `*Controller.start()` 通过 `Model.subscribe()` / `ModelInstance.subscribe()` 等消费并 `_reconcile(event)`；控制器为 leader-only 任务。
- **声明式目标状态 vs 实际状态**：`Model.replicas`（目标）→ `sync_replicas()` 创建/删除 `ModelInstance`；`sync_ready_replicas()` 统计 RUNNING 实例回写 `Model.ready_replicas`（实际）；`ModelInstance.state` 状态机由控制器推进。
- **服务层缓存**：`*Service`（`WorkerService`、`ModelInstanceService`、`ModelRouteService` 等）用 `@locked_cached` 做读缓存，写操作后 `delete_cache_by_key`（可传 `sync_coordinator=False` 跳过跨实例广播，用于高频非安全数据）。
- **worker 通信**：管理端不直接触碰 GPU 机器，经 `worker_request.py` 的 `request_to_worker` / `stream_to_worker`（aiohttp + `Bearer worker.token`，代理/直连双 ClientSession）转发；另有 WebSocket 代理（`MessageServerHandler` / `HTTPSProxyServer`）。
- **通用工作队列**：`workqueue.py` 提供 client-go 风格 `WorkQueue`（per-key 串行、DELETED 优先、延迟堆、指数退避），供控制器重观察避免自激轮询。
- **worker 分配缓存**：`worker_allocated_cache.py` 仿 K8s NodeInfo 缓存，`get_worker_allocated(worker_id)` 由 ModelInstance 绑定聚合，实例变更时 `invalidate_workers_allocated`。

## Flow
- **模型/实例生命周期**：创建 Model → `ModelController._reconcile` → `sync_replicas` 补齐 PENDING 的 `ModelInstance` → Scheduler（`gpustack/scheduler`）选 worker 并回写 → `ModelFileController` / `ensure_instance_model_file` 保证模型文件下载，READY 后 `_promote_to_starting_if_complete` 置 STARTING → worker 启动进程、心跳上报 → 实例 RUNNING → `sync_ready_replicas` 回写 `ready_replicas` → `notify_model_route_target` 重算路由 target 状态。
- **worker 心跳/状态**：agent 心跳 → `flush_worker_status_to_db()`（5s 周期）批量写 `heartbeat_time` 与状态字段 → `WorkerSyncer`（15s 周期）经 `is_worker_reachable` 探测 `/healthz`、`filter_state_change_workers` 计算状态迁移 → `WorkerController._reconcile` 将不可达 worker 上的 RUNNING 实例/子 worker 置 UNREACHABLE → `WorkerInstanceCleaner` 超宽限期后 `batch_delete` 实例触发重调度。
- **路由/网关**：`ModelRouteController._sync_targets` 统计 active targets，`sync_gateway` 经 `gpustack.gateway`（Higress/Istio K8s CRD）reconcile Ingress / WasmPlugin；`ModelRouteTargetController._sync_state` 依 `model.ready_replicas > 0` 决定 target `ACTIVE/UNAVAILABLE`。
- **指标**：网关报告 → `accumulate_gateway_metrics` 缓冲 → `flush_gateway_metrics_to_db` 落 `ModelUsage` / `ModelUsageDetails`。

## Integration
- **被依赖**：`gpustack.scheduler`（调度实例）、`gpustack.routes`（REST/OpenAI API 注入 `SessionDep` 等依赖，见 `deps.py`）、`gpustack.api`（认证中间件/异常）、`gpustack.gateway`、`gpustack.gpu_instances`。
- **依赖**：`gpustack.schemas`（ORM 模型与状态枚举）、`gpustack.policies`（打分/评分链）、`gpustack.security`（JWT/密码哈希）、`gpustack.cloud_providers`（worker 池供给）。
- **worker 通信方式**：HTTP REST 转发（`worker_request.py`，Bearer token）+ worker 主动注册/心跳（`/healthz`、heartbeat 上报）+ WebSocket 代理；管理端 API 端点定义在 `gpustack/routes/`。
- **多实例协调**：`bus.py` / `cache.py` 通过 `set_coordinator()` 接入 `coordinator/` 实现跨实例事件分发与缓存失效广播；`Server._init_coordinator` 优先取扩展插件的 Coordinator，否则回退 `LocalCoordinator`。
