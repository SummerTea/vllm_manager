# gpustack/envs/

## Responsibility
- 进程级环境变量常量仓库：集中定义并读取所有 `GPUSTACK_*` 环境变量（含默认值），供全系统代码 import 使用。
- 对应 `config/` 的「启动参数 + YAML」通道之外的第二配置通道，专用于无需重启/不适合放 Config 的调优项。

## Design
- 单文件 `__init__.py`，模块加载时立即解析 `os.getenv` 并转为类型化常量（bool/int/float/str），无类、无函数——纯常量模块模式。
- 按域分组命名，覆盖面广：
  - 数据库：`DB_ECHO`、`DB_POOL_SIZE`、`DB_MAX_OVERFLOW`、`DB_POOL_TIMEOUT`、`DB_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_SECONDS`、`DB_TRACE_SQL_SUBSTR`。
  - Worker：心跳/状态同步间隔（`WORKER_HEARTBEAT_INTERVAL` 30s、`WORKER_STATUS_SYNC_INTERVAL`、`WORKER_HEARTBEAT_GRACE_PERIOD` 150s）、孤儿任务清理宽限、`WORKER_UNREACHABLE_CHECK_MODE`（auto/enabled/disabled，worker>50 自动关闭探测）。
  - 调度器：scale-up/down 打分权重（`SCHEDULER_SCALE_UP_PLACEMENT_MAX_SCORE` 等）。
  - 计量计费：usage 明细/事件/metered_usage 的归档 cron、保留月数、批大小（`USAGE_DETAILS_RETENTION_MONTHS`、`USAGE_DETAILS_ARCHIVE_CRON` 等）、滚动时区 `TIMEZONE`（含 `USING_DEPRECATED_TIMEZONE` 旧名告警）。
  - GPU 实例：`GPU_INSTANCE_TRANSITIONING_REQUEUE_INTERVAL`、`GPU_INSTANCE_READY_SWEEP_INTERVAL`。
  - 其他：JWT 过期（`JWT_TOKEN_EXPIRE_MINUTES`）、代理超时、gateway 端口检查、event bus 队列容量、ephemeral 端口保留开关（`SKIP_RESERVE_EPHEMERAL_PORTS`）、迁移标志（`MIGRATION_DATA_DIR`/`DATA_MIGRATION`）。
- 约定：模块在日志配置之前被 import（注释明确避免在此处写日志），值解析失败直接抛异常（fail-fast）。

## Flow
- import 即生效：任何模块 `from gpustack import envs` 后在初始化/循环中使用常量；值在进程生命周期内固定（不随运行时环境变化）。

## Integration
- 被依赖方：`security.py`（JWT 过期）、`cmd/db_migration.py`（连接池参数）、`worker/`（心跳、孤儿清理）、`scheduler/`（打分权重）、`server/`（缓存 TTL、event bus、usage 归档控制器）、`policies/` 与 metrics 采集（token 估算启发式常量）。
- 与 `config/Config` 的关系：Config 管「启动配置项」，envs 管「进程级调优开关」，两者互补互不覆盖。
