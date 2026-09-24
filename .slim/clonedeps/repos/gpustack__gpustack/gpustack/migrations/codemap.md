# gpustack/migrations/

## Responsibility
- Alembic 数据库迁移环境：将 `schemas/` 下的 SQLModel 模型元数据落地为 SQL 迁移脚本的执行上下文。
- 管理端 server 启动时自动执行（`alembic upgrade head`），保证数据库 schema 与应用代码版本对齐。

## Design
- `env.py`：Alembic 在线/离线迁移入口。`target_metadata = SQLModel.metadata`（所有 `table=True` 模型自动纳入）；在线模式用 `engine_from_config` + `NullPool`，`render_as_batch=True`（SQLite 批量 ALTER 兼容）、`transactional_ddl=True`；URL 取自 alembic 配置的 `sqlalchemy.url` 或 `DATABASE_URL` 环境变量。启动时调用 `patch_pg_version_info()`（utils/db，兼容 openGauss 版本探测）。
- 无独立 `alembic.ini` 固定于本目录：配置（script_location、sqlalchemy.url、`called_by_db_migration` 标记）由调用方（`cmd/db_migration.py` 或 server 启动流程）通过 `AlembicConfig.set_main_option` 动态注入。
- `utils.py`：迁移辅助函数——`is_opengauss(conn)`（openGauss 缺 jsonb_agg 等聚合函数，迁移需回退为 Python 处理）、`column_exists()`/`table_exists()`（幂等迁移常用检查）。
- `script.py.mako`：`alembic revision` 生成新迁移文件的模板（revision/down_revision/branch_labels + upgrade/downgrade 骨架）。
- `versions/` 存放全部历史迁移（见 `versions/codemap.md`）；`cmd/db_migration.py` 与 `cmd/prerun.py` 也以 `migrations/` 为 `script_location` 驱动 alembic。

## Flow
- server 启动 →（`server/` 初始化或 prerun）→ 构造 AlembicConfig（`script_location` 指向本目录、`sqlalchemy.url` 指向目标库）→ `command.upgrade(cfg, "head")` → `env.py` 在线模式执行待应用迁移。
- `gpustack migrate`（cmd/db_migration.py）对 sqlite 与目标库各跑一次 `upgrade` 到指定 revision。

## Integration
- 依赖：`gpustack.schemas`（target_metadata）、`gpustack.utils.db.patch_pg_version_info`、SQLModel/SQLAlchemy/Alembic。
- 被依赖：`server/` 启动流程（自动迁移）、`cmd/db_migration.py`（数据迁移）、`cmd/prerun.py`（postgres 迁移服务触发）；schema 变更流程 = 改 `schemas/` 模型 → `alembic revision --autogenerate` 生成新版本文件。
