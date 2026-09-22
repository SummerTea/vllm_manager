"""
应用生命周期管理
负责初始化和清理全局资源
"""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.config import app_config
from app.extensions import (
    close_redis,
    dispose_db,
    init_db,
    init_logging,
    init_redis,
    init_saq,
    shutdown_logging,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    应用生命周期管理
    负责初始化和清理全局资源
    """
    # 1. 初始化日志
    init_logging()

    # 2. 初始化数据库（开发环境自动建表，生产走迁移）
    if "db" not in app_config.DISABLED_EXTENSIONS:
        await init_db(create_tables=(app_config.DEPLOY_ENV == "DEVELOPMENT"))

    # 3. 初始化 Redis
    if "redis" not in app_config.DISABLED_EXTENSIONS:
        await init_redis()

    # 4. 初始化 SAQ
    if "saq" not in app_config.DISABLED_EXTENSIONS:
        await init_saq()

    # 5. 启动节点主动探测后台任务（依赖 DB，db 禁用时不启动）
    prober_task: asyncio.Task | None = None
    if "db" not in app_config.DISABLED_EXTENSIONS:
        from app.server.node.prober import probe_loop

        prober_task = asyncio.create_task(probe_loop())

    # 6. 启动实例失联对账后台任务（依赖 DB，db 禁用时不启动）
    reconcile_task: asyncio.Task | None = None
    if "db" not in app_config.DISABLED_EXTENSIONS:
        from app.server.instance.reconciler import reconcile_loop

        reconcile_task = asyncio.create_task(reconcile_loop())

    yield

    # 清理资源
    if prober_task is not None:
        prober_task.cancel()
        with suppress(asyncio.CancelledError):
            await prober_task

    if reconcile_task is not None:
        reconcile_task.cancel()
        with suppress(asyncio.CancelledError):
            await reconcile_task

    if "redis" not in app_config.DISABLED_EXTENSIONS:
        await close_redis()

    if "db" not in app_config.DISABLED_EXTENSIONS:
        await dispose_db()

    shutdown_logging()
