"""
应用生命周期管理
负责初始化和清理全局资源
"""
import logging
from contextlib import asynccontextmanager

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

    # 2. 初始化数据库
    if "db" not in app_config.DISABLED_EXTENSIONS:
        await init_db()

    # 3. 初始化 Redis
    if "redis" not in app_config.DISABLED_EXTENSIONS:
        await init_redis()

    # 4. 初始化 SAQ
    if "saq" not in app_config.DISABLED_EXTENSIONS:
        await init_saq()

    yield

    # 清理资源
    if "redis" not in app_config.DISABLED_EXTENSIONS:
        await close_redis()

    if "db" not in app_config.DISABLED_EXTENSIONS:
        await dispose_db()

    shutdown_logging()
