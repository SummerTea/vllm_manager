# gpustack/mixins/

## Responsibility
- 通用 ORM mixin 库：为 `schemas/` 下所有 SQLModel 数据模型提供「活动记录」CRUD 能力与统一时间戳字段。
- 是 server 端所有数据访问的基础抽象——业务代码基本只调用 mixin 方法读写数据库。

## Design
- `active_record.py` 定义 `ActiveRecordMixin`（类方法族 + 实例方法族）：
  - 查询：`first/one_by_id/one_by_field(s)/first_by_field(s)/exist_by_field(s)/all_by_field(s)/count/count_by_field(s)/paginated_by_query`（分页 + 精确/模糊过滤 + 多列排序，支持 JSON 字段点路径排序，如 `memory.utilization_rate`，按 postgres/mysql 方言生成表达式）。
  - 写：`create/create_or_update/save/update/batch_update/delete/delete_all`，带 `auto_commit` 参数；save 失败自动 rollback 并重抛。
  - 软删级联：`delete(soft=True)` 或存在 cascade 关系时置 `deleted_at` 并递归删除关联对象。
  - 缓存：`cached_all`（`@locked_cached` 装饰器 + `class_key("cached_all")`，anyio shield 防取消导致连接池泄漏），写路径 `_invalidate_cached_all()` 失效。
  - **事件发布（核心机制）**：`_publish_event_after_commit` 把 `CommitEvent(type=C+U+D)` 暂存到 `session.info["pending_events"]`；SQLAlchemy 事件钩子 `find_history`（`before_flush`，记录 changed_fields）与 `send_post_commit_events`（`after_commit`，deep-copy 后 `asyncio.create_task(event_bus.publish(topic, event))`）在事务提交后向 `gpustack.server.bus.event_bus` 发布模型事件。
  - 订阅/流式：`subscribe()` 生成器（先重放 `cached_all` 初始快照为 CREATED，再收增量事件，15s HEARTBEAT 心跳）；`streaming()` 按字段过滤、转换 `*Public` 类、输出 SSE 格式 JSON（REST watch 接口的数据源）。
- `timestamp.py` 定义 `TimestampsMixin`：`created_at`/`updated_at`/`deleted_at` 三列，`UTCDateTime` 类型、UTC 时间（存储去 tzinfo），updated_at 带 onupdate。
- `__init__.py` 组合二者为 `BaseModelMixin(ActiveRecordMixin, TimestampsMixin)`——`schemas/` 各模型统一 `class X(XBase, BaseModelMixin, table=True)`。

## Flow
- 写路径：`await Model.create(session, data)` → 记录 pending event → `save` → flush/commit → `after_commit` 钩子发事件到 event_bus → 缓存失效。
- 读路径：REST 接口（`api/` 路由）调用查询类方法返回 `PaginatedList`；watch 接口调 `streaming()` 输出事件流。
- 消费流：`server/` 各 controller/worker 端状态同步通过 `subscribe()` 监听模型变更（如 model_instance 状态变更驱动调度）。

## Integration
- 依赖：`gpustack.server.bus`（event_bus、Event/EventType）、`gpustack.server.cache`（locked_cached）、`gpustack.server.db`（async_session）、`gpustack.schemas.common`（PaginatedList/Pagination/UTCDateTime）。
- 被依赖：`schemas/` 下全部 ORM 模型（ApiKey、Benchmark、Worker、Model、ModelInstance、GPUInstance、Cluster、User 等约 40 个模型）；`api/` 路由、`server/` controller、`scheduler/`、`policies/` 均经 mixin 访问数据。
