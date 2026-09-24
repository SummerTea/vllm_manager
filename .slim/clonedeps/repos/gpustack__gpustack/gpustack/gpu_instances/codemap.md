# gpustack/gpu_instances/

## Responsibility
Kubernetes provider 集群上的 GPU 实例（`GPUInstance`）生命周期控制：通过"gpustack-operator"把 GPU 实例作为 `worker.gpustack.ai/v1` CRD（Instance/InstanceType/InstancePersistentVolume(Type)/InstanceSSHPublicKey）投射到 K8s 集群，并持续对账（reconcile）数据库行与集群 CR 状态。vllm_manager 不涉及 GPU 实例管理，仅参考。

## Design
- `controllers.py`（55KB，核心）：
  - `GPUInstanceController`：DB 事件总线（`GPUInstance.subscribe`）驱动 + 下游 operator watch 回流的工作队列（`WorkQueue`，per-keys 串行、DELETED 粘性 coalesce、指数退避重试、`_requeue_after` 自重入）。`_reconcile_instance` 按 DB phase 分支状态机：`_reconcile_creating`（创建 SSH key/PV/PVType/Instance CR 资产，失败写 `*CreateFailed` phase）/`_reconcile_starting`（清 `spec.stop`，CR 丢失则重建）/`_reconcile_stopping`（反复下发 stop 直到 worker 报告 Stopped）/`_reconcile_deleting`（拆 worker 侧后删行）/`_reconcile_observe`（读 CR merge 回 DB）。下游事件经 `_resolve_instance_id`（优先 label `gpustack.ai/instance-id`，兜底 namespace→owner 反查）映射回上游。
  - `_PersistentVolumeFinalizeController` 及 PV / PVType 两个子类：finalize 时向集群探测并删除对应 CR，`_blocked_reason` 处理删除阻塞。
- `cluster_apis.py`：`ClusterOps` —— `worker.gpustack.ai/v1` CRD 原始客户端（`kubernetes_asyncio` CustomObjectsApi），封装 Instance/InstanceType/PersistentVolume(Type)/SSHPublicKey 的 CRUD，经 `get_k8s_client` 走 `http://localhost:{port}/v2/clusters/{id}/proxy`（登记 token 鉴权）。
- `cluster_apis_util.py`：命名/转换工具——SQLModel 行 → CRD spec dict（`_hoist_meta` 抬升 display_name/description、NFS subDirectory 与 S3 prefix 注入 principal 隔离后缀）；`principal_namespace_identifier`/`get_namespace_name`/`get_persistent_volume_type_name` 及其 parse 对应。
- `gateway_client.py`：`gpustack-operator` worker gateway 的 HTTP 客户端——aiohttp 经 TLS-on-unix-socket（`_UnixTLSConnector` 自定义 connector 挂 SSL、关闭证书校验）访问 `/apis/workers|instancetypes|instances`；`watch_*` 消费 NDJSON watch 流（去掉总超时、`sock_connect=30`）并重帧为 `<json>\n\n`。
- `gateway.py`：非 leader 实例也运行的订阅协程，监听 Cluster 事件对 K8s provider 集群 `subscribe_worker`/`unsubscribe_worker`（订阅 InstanceType/Instance GVK）。
- `templates.py`/`validation.py`：内置 GPU 实例模板（从 assets YAML 同步入库，不覆盖用户模板）、K8s 对象名合法性校验（`validate_k8s_object_name`）。

## Flow
DB 行创建/变更 → bus 事件入队 → 状态机按 phase 调 `ClusterOps` 写 CR（创建/启动/停止/删除）→ operator 在集群内调度实例 → watch 流把 CR 变化（含外部删除）回流队列形成闭环；实例状态 merge 回 `GPUInstanceStatus`（phase/namespace/phase_message）。

## Integration
- 消费方：`server/server.py`（启动 `GPUInstanceController` 与订阅协程）、集群 K8s provider 管理、`k8s/`（manifest 渲染复用其命名工具）。
- 依赖：`kubernetes_asyncio`、`aiohttp`、gpustack-operator（外部 Go 组件，unix socket 边界为信任边界）、`gpustack.server.bus`（事件总线）、`gpustack.schemas.clusters`。
