# backend/app/extensions/

## Responsibility
外部基础设施扩展层（懒加载连接管理）：封装 database（PostgreSQL/asyncpg）、redis（单机/哨兵）、saq（基于 Redis 的任务队列）、logging（异步非阻塞日志）四类单例资源。**import 阶段零连接**——engine/redis client/queue 均在首次 `init_*()` 后可用。

## Design
- **单例 + init/get/dispose 三段式**：模块级私有全局（`_engine`、`_redis_client`）+ 公开 `init_*(创建+连通性测试)/get_*(获取,未初始化抛 RuntimeError)/dispose_*(关闭)`；`init_*` 幂等（重复调用仅告警跳过）。
- **独立 Config 子类**：`DatabaseConfig`/`RedisConfig`/`LoggingConfig` 均继承 `AppBaseConfig`，按 `DB_*`/`REDIS_*`/`LOG_*` 分组读 `.env`；`SQLALCHEMY_DATABASE_URI`/`SQLALCHEMY_CONNECT_ARGS` 为 `computed_field` 动态构建（含 `search_path=DB_PG_SCHEMA`、`idle_in_transaction_session_timeout`、`statement_cache_size` 等 asyncpg 专属参数）。
- **DB 会话**：`get_session`（FastAPI 依赖，退出自动 commit，异常按 `HTTPException`/`RequestValidationError`/`VllmManagerException`/其他 分类 rollback，未知异常包装成 `DatabaseException` 重抛）与 `get_session_context`（asynccontextmanager 等价语义）；`init_db(create_tables=False)` 可选 `Base.metadata.create_all`（开发环境建表），另附 `create_tables`/`drop_tables`/`schema_diff_check` 工具。
- **Redis 降级**：`redis_fallback(default_return)` 装饰器——RedisError 时返回默认值不抛异常（优雅降级，供监控类读取使用）。
- **SAQ 代理**：`SaqQueueProxy` 延迟解析单例（`queue` 全局代理，`init_saq()` 注入 redis client 后可用）；队列命名 `vllm_manager:queue:{name}`，默认 `default`。
- **异步日志**：QueueHandler + QueueListener 后台线程写 RotatingFileHandler + stdout，消除文件 I/O 对 asyncio 事件循环的阻塞；支持 `LOG_TZ` 时区（pytz）与 `{pid}`/`{host_name}` 占位符；`atexit` 注册关闭。

## Flow
- 初始化顺序（lifespan 与 work_saq 共用）：`init_logging()`（同步）→ `init_db()` → `init_redis()` → `init_saq()`（依赖已就绪的 redis client）→ 业务任务/后台循环。
- 请求会话流：`get_session` yield AsyncSession → 业务 CRUD（默认不 commit）→ yield 后 commit；异常路径按分层 rollback 或包装重抛。
- 关闭顺序（lifespan shutdown）：取消后台任务 → `close_redis()` → `dispose_db()` → `shutdown_logging()`。

## Integration
- 初始化编排在 `app/main/lifespan.py`（server）与 `app/work_saq.py`（SAQ worker）驱动，按 `app_config.DISABLED_EXTENSIONS`（逗号分隔 db/redis/saq，logging 不可禁用）跳过对应初始化。
- 被消费方：`base_crud` 依赖 AsyncSession 类型；`app/worker/` 域**不依赖本层**（worker 不连 PG，仅用自身 config）。
- 聚合导出：`extensions/__init__.py` 统一 re-export（`db_config`/`get_session`/`init_db`/`init_redis`/`init_saq`/`queue` 等），上层从 `app.extensions` 导入。
- 配置依赖：`DB_*`/`REDIS_*`/`LOG_*` 环境变量（见 `.env.example`）；`TABLE_NAME_PREFIX` 同时用于表名前缀与连接 `application_name`。

## 导航提示
- 文件→职责对照：`database.py`（engine/session/init_db）、`redis.py`（单机/哨兵 + redis_fallback 降级）、`saq.py`（SaqQueueProxy/queue）、`logging.py`（QueueListener 异步日志）、`__init__.py`（统一 re-export）。
- 排查"连接未初始化"先看是否在 lifespan 或 work_saq 中调了对应 `init_*`；本层 import 零副作用，可被任意模块安全 import。
