# gpustack/schemas/

## Responsibility
DTO 与数据模型定义层（SQLModel = SQLAlchemy ORM 表模型 + Pydantic 校验合一）。

每个资源一个文件，定义持久化表、请求体（Create/Update）、响应体（Public）
及列表参数。`schemas/__init__.py` 汇总导出全部模型。

是 routes ↔ server 之间的数据契约。

## Design
- **每资源四件套模式**：
  - `XxxBase`（共享字段）→ `Xxx(XxxBase, BaseModelMixin, table=True)`
    （ORM 表，如 `Worker`、`Model`、`ModelInstance`、`Cluster`、`GPUInstance`）
    → `XxxCreate`/`XxxUpdate`（请求 DTO）
    → `XxxPublic`（响应 DTO，追加 `id/created_at/updated_at`）
    + `XxxsPublic = PaginatedList[Xxx]`（列表响应）。
- **通用基建（`common.py`）**：
  - `Pagination{page, perPage, total, totalPage}`、`PaginatedList`。
  - `ListParams`（`page/perPage/watch/sort_by`，`sort_by` 校验 +
    计算属性 `order_by` 解析为 `[(field, dir)]`）。
  - `UTCDateTime`（统一 UTC 存取）、`pydantic_column_type`
    （把 Pydantic 模型序列化为 JSON 列并反序列化还原）、
    `pydantic_camel_case_generator`（响应字段 snake_case→camelCase，
    `created_at` 等保留）。
- **租户字段约定**：几乎所有业务表带 `owner_principal_id`
  （外键 `principals.id`），支撑 `api/tenant.py` 的行级租户过滤；
  cluster/worker 等还带 `cluster_id`。
- **状态机定义在 schema 层**：
  - `WorkerStateEnum`（NOT_READY/READY/UNREACHABLE/MAINTENANCE…）
    + `Worker.compute_state()` 基于 `heartbeat_time` 与
    `envs.WORKER_HEARTBEAT_GRACE_PERIOD` 判定离线。
  - `ModelInstanceStateEnum`（PENDING→ANALYZING→SCHEDULED→INITIALIZING→
    DOWNLOADING→STARTING→RUNNING/ERROR/UNREACHABLE）。
  - `WorkerStatusStored` 是 worker 上报的状态载荷
    （hostname/ip/port/metrics_port/system_reserved/status/worker_uuid）。
- **复杂 JSON 结构**：
  - `WorkerStatus`（CPU/Memory/Swap/GPUDeviceStatus/文件系统）。
  - `ComputedResourceClaim`（调度资源声明：vram/tensor_split/offload_layers）。
  - `DistributedServers`（分布式实例主从协调）。
  - `ModelInstanceLogOptions` 系列（日志 restart 分布）。
  - `GPUDevice` 是基于 `workers` 表的只读 SQL 视图模型（`gpu_devices_view`）。

## Flow
请求体（Create/Update DTO）→ 路由 handler 校验
→ `BaseModelMixin.create/update` 持久化
（JSON 字段经 `pydantic_column_type` 编码入库）
→ 查询时 `paginated_by_query`/`one_by_id` 取回
→ 序列化为 `XxxPublic`（camelCase）返回。

worker 侧：`POST /v2/worker-status` 上报 `WorkerStatusStored`
→ 缓冲批量写入 `workers.status` 列。

## Integration
- 依赖方：`gpustack/mixins`（`BaseModelMixin`）、`gpustack/schemas/common.py`、
  `gpustack/envs.py`、`gpustack/utils/network.py`（离线判定）、
  `gpustack/api/exceptions.py`（`InvalidException`）。
- 被依赖方：`gpustack/routes/*`（handler 的 request/response_model）、
  `gpustack/server/*`（services/scheduler 直接读表）、
  `gpustack/api/*`（`Principal`/`OrgRole` 等）、`gpustack/worker/*`
  （`WorkerStatusStored`、`ServeLogOptionsResponse`）、
  `gpustack/schemas/__init__.py` 作为统一导出面。
- 参考价值：对 manager↔agent 契约最有借鉴意义的是 `WorkerStatusStored`
  （agent 心跳状态载荷）与 `WorkerRegistrationPublic`（注册响应含 token + worker_config）
  ——字段结构（ip/ifname/port/metrics_port/GPU devices）
  可直接映射本项目 agent 的心跳上报体。
