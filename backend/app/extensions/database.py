"""
数据库扩展模块
提供 FastAPI + SQLAlchemy 异步数据库连接和会话管理

支持数据库:
- PostgreSQL (asyncpg)

使用方式:
    from app.extensions.database import get_session, init_db, dispose_db

    # FastAPI 依赖注入
    @app.get("/users")
    async def get_users(session: AsyncSession = Depends(get_session)):
        result = await session.execute(select(User))
        users = result.scalars().all()
        return users

    # 手动获取会话
    async with get_session_context() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()

    # 应用启动时初始化
    @app.on_event("startup")
    async def startup():
        await init_db()

    # 应用关闭时清理
    @app.on_event("shutdown")
    async def shutdown():
        await dispose_db()
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import Field, NonNegativeInt, PositiveInt, computed_field
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.base.base_config import AppBaseConfig
from app.base.base_model import Base
from app.exception import DatabaseException, VllmManagerException

logger = logging.getLogger(__name__)


# ============ 配置 ============


class DatabaseConfig(AppBaseConfig):
    """数据库配置（PostgreSQL + asyncpg）"""

    # 基础连接配置
    DB_HOST: str = Field(default="localhost", description="数据库服务器地址")
    DB_PORT: PositiveInt = Field(default=5432, description="数据库端口")
    DB_USERNAME: str = Field(default="postgres", description="数据库用户名")
    DB_PASSWORD: str = Field(default="", description="数据库密码")
    DB_DATABASE: str = Field(default="vllm_manager", description="数据库名称")
    DB_EXTRAS: str = Field(default="", description="额外连接参数，如 'keepalives_idle=60&keepalives=1'")
    DB_CONNECT_TIMEOUT: NonNegativeInt = Field(
        default=10, description="连接建立超时（秒），0=驱动默认。asyncpg→timeout")

    # 业务配置
    TABLE_NAME_PREFIX: str = Field(default="vllm_manager", description="表名前缀")

    # PostgreSQL 专属
    DB_PG_SCHEMA: str = Field(default="public", description="PostgreSQL schema 名称")

    # PostgreSQL 专属超时/缓存
    DB_PG_COMMAND_TIMEOUT: NonNegativeInt = Field(
        default=60, description="asyncpg 单条语句执行超时（秒），0=不限制")
    DB_PG_IDLE_IN_TX_TIMEOUT_MS: NonNegativeInt = Field(
        default=60000, description="事务内空闲超时（毫秒），防泄漏事务长期持锁，0=不设置")
    DB_PG_STATEMENT_TIMEOUT_MS: NonNegativeInt = Field(
        default=0, description="服务端语句超时（毫秒），0=不限制")
    DB_PG_STATEMENT_CACHE_SIZE: int = Field(
        default=-1, ge=-1,
        description="asyncpg prepared statement 缓存条数，-1=驱动默认(100)。"
                    "经 PgBouncer transaction 池化时必须设 0")

    # SQLAlchemy 配置
    SQLALCHEMY_ECHO: bool = Field(default=False, description="是否打印 SQL 语句")
    SQLALCHEMY_POOL_ECHO: bool = Field(default=False, description="是否打印连接池日志")

    SQLALCHEMY_POOL_TIMEOUT: NonNegativeInt = Field(default=30, description="获取连接超时时间（秒）")
    SQLALCHEMY_POOL_RECYCLE: NonNegativeInt = Field(default=3600, description="连接回收时间（秒）")
    SQLALCHEMY_POOL_PRE_PING: bool = Field(default=True, description="是否在使用前检测连接可用性")
    SQLALCHEMY_POOL_USE_LIFO: bool = Field(default=True, description="是否使用 LIFO 连接池（复用热连接，高并发场景建议开启）")

    # 连接池容量
    #
    # 单机会起两个进程（API 与 SAQ worker，后者在 work_saq.py 也调 init_db 自建池），
    # 故单机占用 = (POOL_SIZE + MAX_OVERFLOW) × 2。多实例部署时必须同步核对：
    # 单进程上限 × 实例数 ≤ max_connections - 保留数。
    SQLALCHEMY_ASYNC_POOL_SIZE: NonNegativeInt = Field(
        default=20, description="连接池常驻大小（稳态并发查询数）")
    SQLALCHEMY_ASYNC_MAX_OVERFLOW: NonNegativeInt = Field(
        default=10, description="连接池突发溢出数量（超出即等待 POOL_TIMEOUT）")

    CUSTOM_SQLALCHEMY_DATABASE_URI: str | None = Field(
        description="Custom SQLAlchemy Database URI to override default construction", default=None, )

    @computed_field
    @property
    def DB_PASSWORD_DECRYPTED(self) -> str:
        """数据库密码（原样返回，密码应使用明文或由部署层注入）"""
        return self.DB_PASSWORD or ""

    @computed_field
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        """构建异步数据库连接 URI"""
        from urllib.parse import quote_plus

        if self.CUSTOM_SQLALCHEMY_DATABASE_URI:
            return self.CUSTOM_SQLALCHEMY_DATABASE_URI

        uri = (f"postgresql+asyncpg://"
               f"{quote_plus(self.DB_USERNAME)}:{quote_plus(self.DB_PASSWORD_DECRYPTED)}"
               f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_DATABASE}")

        if self.DB_EXTRAS:
            uri += "?" + self.DB_EXTRAS

        return uri

    @computed_field
    @property
    def SQLALCHEMY_CONNECT_ARGS(self) -> dict[str, Any]:
        """数据库连接参数（asyncpg）"""
        # asyncpg server_settings 值必须是字符串
        server_settings: dict[str, str] = {"application_name": self.TABLE_NAME_PREFIX}
        # 把 DB_PG_SCHEMA 作为连接级 search_path 下发，让所有未限定表名
        # 的 SQL 都命中该 schema。asyncpg 将其作为 startup parameter 发送，
        # 相当于每次开连接自动 SET search_path，无需修改 model 或
        # __tablename__。默认 "public" 与 PostgreSQL 原生行为一致。
        if self.DB_PG_SCHEMA:
            server_settings["search_path"] = self.DB_PG_SCHEMA
        if self.DB_PG_IDLE_IN_TX_TIMEOUT_MS:
            server_settings["idle_in_transaction_session_timeout"] = str(self.DB_PG_IDLE_IN_TX_TIMEOUT_MS)
        if self.DB_PG_STATEMENT_TIMEOUT_MS:
            server_settings["statement_timeout"] = str(self.DB_PG_STATEMENT_TIMEOUT_MS)
        args: dict[str, Any] = {"server_settings": server_settings}
        if self.DB_CONNECT_TIMEOUT:
            args["timeout"] = self.DB_CONNECT_TIMEOUT
        if self.DB_PG_COMMAND_TIMEOUT:
            args["command_timeout"] = self.DB_PG_COMMAND_TIMEOUT
        if self.DB_PG_STATEMENT_CACHE_SIZE >= 0:
            args["statement_cache_size"] = self.DB_PG_STATEMENT_CACHE_SIZE
        return args


# ============ 全局实例 ============

db_config = DatabaseConfig()

_engine: AsyncEngine | None = None


# ============ 引擎管理 ============


def get_engine() -> AsyncEngine:
    """
    获取异步数据库引擎（单例模式）

    Returns:
        AsyncEngine 实例

    Raises:
        RuntimeError: 如果引擎未初始化
    """
    global _engine

    if _engine is None:
        raise RuntimeError("数据库引擎未初始化，请先调用 init_db()")

    return _engine


async def create_engine_instance() -> AsyncEngine:
    """
    创建异步数据库引擎实例

    Returns:
        AsyncEngine 实例
    """
    engine = create_async_engine(db_config.SQLALCHEMY_DATABASE_URI, echo=db_config.SQLALCHEMY_ECHO,
        echo_pool=db_config.SQLALCHEMY_POOL_ECHO, pool_size=db_config.SQLALCHEMY_ASYNC_POOL_SIZE,
        max_overflow=db_config.SQLALCHEMY_ASYNC_MAX_OVERFLOW, pool_timeout=db_config.SQLALCHEMY_POOL_TIMEOUT,
        pool_recycle=db_config.SQLALCHEMY_POOL_RECYCLE, pool_pre_ping=db_config.SQLALCHEMY_POOL_PRE_PING,
        pool_use_lifo=db_config.SQLALCHEMY_POOL_USE_LIFO, connect_args=db_config.SQLALCHEMY_CONNECT_ARGS, )

    logger.info(f"数据库引擎创建成功: {db_config.DB_HOST}:{db_config.DB_PORT}/{db_config.DB_DATABASE}")

    return engine


# ============ 会话管理 ============


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI 依赖注入用的异步会话生成器

    使用方式:
        @app.get("/users")
        async def get_users(session: AsyncSession = Depends(get_session)):
            result = await session.execute(select(User))
            users = result.scalars().all()
            return users

    Yields:
        AsyncSession 实例
    """
    async with AsyncSession(get_engine(), expire_on_commit=False) as session:
        try:
            yield session
            await session.commit()
        except HTTPException:
            await session.rollback()
            raise
        except RequestValidationError:
            await session.rollback()
            raise
        except VllmManagerException:
            await session.rollback()
            raise
        except Exception as e:
            await session.rollback()
            logger.error(f"数据库操作异常: {e}", exc_info=True)
            raise DatabaseException(f"数据库操作异常: {e}", details={"original_error": str(e)}) from e


@asynccontextmanager
async def get_session_context() -> AsyncGenerator[AsyncSession, None]:
    """
    上下文管理器方式获取异步会话

    使用方式:
        async with get_session_context() as session:
            result = await session.execute(select(User))
            users = result.scalars().all()

    Yields:
        AsyncSession 实例
    """
    async with AsyncSession(get_engine(), expire_on_commit=False) as session:
        try:
            yield session
            await session.commit()
        except HTTPException:
            await session.rollback()
            raise
        except RequestValidationError:
            await session.rollback()
            raise
        except VllmManagerException:
            await session.rollback()
            raise
        except Exception as e:
            await session.rollback()
            logger.error(f"数据库操作异常: {e}", exc_info=True)
            raise DatabaseException(f"数据库操作异常: {e}", details={"original_error": str(e)}) from e


# ============ 初始化和清理 ============


async def init_db(create_tables: bool = False) -> None:
    """
    初始化数据库引擎

    Args:
        create_tables: 是否自动创建所有表（开发环境建议 True，生产环境使用迁移工具）

    Note:
        如果 create_tables=True，需要先导入所有模型，确保它们注册到 Base.metadata
    """
    global _engine

    if _engine is not None:
        logger.warning("数据库引擎已初始化，跳过重复初始化")
        return

    try:
        _engine = await create_engine_instance()

        # 可选：自动创建表
        if create_tables:
            async with _engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                logger.info("数据库表创建成功")

        # 测试连接
        async with AsyncSession(_engine) as session:
            await session.execute(text("SELECT 1"))
            logger.info("数据库连接测试成功")

    except Exception as e:
        logger.error(f"数据库初始化失败: {e}", exc_info=True)
        raise DatabaseException(f"数据库初始化失败: {e}", details={"original_error": str(e)}) from e


async def dispose_db() -> None:
    """
    关闭数据库连接池

    适用于应用关闭时清理资源
    """
    global _engine

    if _engine is not None:
        await _engine.dispose()
        _engine = None
        logger.info("数据库连接池已关闭")


# ============ 工具函数 ============


async def create_tables() -> None:
    """
    创建所有数据库表

    Note:
        需要先导入所有模型，确保它们注册到 Base.metadata
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("数据库表创建成功")


async def drop_tables() -> None:
    """
    删除所有数据库表（慎用！）

    Warning:
        此操作会删除所有数据，仅用于开发/测试环境
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    logger.warning("数据库表已删除")


async def schema_diff_check() -> dict[str, Any]:
    """
    轻量级 schema 对比：返回 Base.metadata 与 DB 实际表/列的差异（只读，不修改）。

    Returns:
        {"tables_missing": [...], "tables_extra": [...], "columns_missing": {table: [col, ...]}}
    """
    engine = get_engine()

    def _sync_inspect(sync_conn):
        insp = inspect(sync_conn)
        expected = {t.name: {c.name for c in t.columns} for t in Base.metadata.sorted_tables}
        # 显式传 schema，避免依赖 search_path 隐式一致（inspect 会话与
        # 连接级 search_path 可能不一致时，无 schema 查询会命中错误 schema）
        actual_names = set(insp.get_table_names(schema=db_config.DB_PG_SCHEMA))
        columns_missing: dict[str, list[str]] = {}
        for table_name, cols in expected.items():
            if table_name not in actual_names:
                continue
            actual_cols = {c["name"] for c in insp.get_columns(table_name, schema=db_config.DB_PG_SCHEMA)}
            missing = sorted(cols - actual_cols)
            if missing:
                columns_missing[table_name] = missing
        return {
            "tables_missing": sorted(set(expected) - actual_names),
            "tables_extra": sorted(actual_names - set(expected)),
            "columns_missing": columns_missing,
        }

    async with engine.connect() as conn:
        return await conn.run_sync(_sync_inspect)
