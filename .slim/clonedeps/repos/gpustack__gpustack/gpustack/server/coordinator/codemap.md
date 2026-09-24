# gpustack/server/coordinator/

## Responsibility
- 多实例协调抽象层：为 GPUStack server 提供 **leader 选举**（active-passive）与 **跨实例 pub/sub 事件分发** 的最小接口。
- 单机部署使用自带 `LocalCoordinator`；多实例（HA）部署由扩展插件贡献分布式实现（如 Redis / PostgreSQL 后端）。

## Design
- **`Coordinator` ABC**（`base.py`）：抽象方法 `start` / `stop` / `acquire_leadership(ttl)` / `renew_leadership(ttl)` / `release_leadership` / `publish(channel, event)`。
- `subscribe` / `unsubscribe` 维护本地回调表；实现须保证 callback 在主 asyncio 事件循环上调用。leader 选举 TTL 默认 30s、续约间隔 10s。
- **`Event` / `EventType`**（`base.py`）：`EventType` 枚举 `CREATED/UPDATED/DELETED/UNKNOWN/HEARTBEAT`。
- `Event.to_dict()` 跨实例传输时**只携带对象 ID**（规避序列化问题与 NOTIFY 载荷限制）；`changed_fields` 不跨实例传输，由对端用本地缓存检测。
- **`LocalCoordinator`**（`local.py`）：单节点默认实现，恒为 leader；`publish` 直接 `_notify_local_subscribers`，其余方法近似 no-op。
- **`ChangeDetector`**（`cache.py`）：按实体类型的 LRU 缓存，`detect_changes(old, new)` 对比 `model_fields` 输出 `{field: (old, new)}`。
- 列表/关系字段输出 `(removed, added)` 增量，与本地 `active_record.py` 的 `find_history` 形状一致；`EventCacheManager` 单例管理各实体 detector。
- `preload_cache()` 启动时全量预载，确保首个跨实例事件能算出正确 `changed_fields`。
- **`_ModelRegistry`**（`models.py`）：topic → SQLModel 类的延迟加载注册表（如 `'worker' → gpustack.schemas.workers.Worker`、`'modelinstance' → ModelInstance`）。
- 单独成模块以避免 `bus.py → models → active_record.py → bus.py` 循环导入。

## Flow
- **事件发布**：本地 DB 提交后 `event_bus.publish(topic, event)`（`bus.py`）→ 若挂有 Coordinator 则 `await coordinator.publish(topic, event)`；失败时回退本地 `_route_event`。
- **跨实例消费**：对端 Coordinator 回调 → `EventBus._on_coordinator_event` → `_process_coordinator_event`：识别 ID-only 事件 → `get_model_for_topic(topic)` 取模型类。
- 继续：`get_change_detector(topic).get(id)` 取旧值 → 从 DB 拉全量对象 → `detect_changes` 算 `changed_fields` → 重构完整 `Event` 路由给本地订阅者。
- DELETED 事件用缓存旧对象作 data，并在事后 `remove(id)`。
- **缓存失效广播**：`server/cache.py` 的 `delete_cache_by_key`（`sync_coordinator=True`）向 `"cache"` topic 发布 `Event(DELETED, {"key": ...})`。
- 对端 `_handle_cache_invalidate` 仅本地删除该 key（不回广播）。
- **leader 选举**：`Server._start_leader_only_tasks`（`server.py`）在分布式模式下启动 `_leader_election_loop`，周期 `acquire_leadership` / `renew_leadership`。
- 当选 leader 才启动 scheduler、controllers 等 leader-only 任务；`LocalCoordinator` 下直接启动。
- **启动预载**：非 Local 模式时 `Server._preload_change_detector_cache` 对 `worker` / `model` / `modelinstance` / `modelroute` 等 topic 逐一调 `preload_cache`。

## Integration
- **被依赖方**：`gpustack/server/bus.py`（`set_coordinator` / 事件富化）、`gpustack/server/cache.py`（缓存失效广播）、`gpustack/server/server.py`（`_init_coordinator`、`_leader_election_loop`、`_preload_change_detector_cache`）。
- **插件扩展点**：扩展插件在 `__init__(app, cfg)` 中把分布式 `Coordinator` 挂到 `plugin.coordinator`。
- `Server._init_coordinator` 扫描 `app.state.extension_plugins` 取第一个非空实现，否则回退 `LocalCoordinator`。
- **topic 契约**：channel/topic 名与 `_ModelRegistry` 注册表对应（`worker`、`model`、`modelinstance`、`modelfile`、`modelroute`、`modelroutetarget`、`cluster`、`workerpool`、`user`、`inferencebackend` 等）。
- 事件经 `bus.py` 再分发给各 Controller 与 watch stream（`source="streaming"`）。
