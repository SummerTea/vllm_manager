"""
扩展模块
提供应用的核心扩展功能
"""

from .database import (
    db_config,
    dispose_db,
    get_engine,
    get_session,
    get_session_context,
    init_db,
)
from .logging import init_logging, logging_config, shutdown_logging
from .redis import (
    close_redis,
    get_redis,
    get_redis_client,
    get_redis_context,
    init_redis,
    redis_config,
    redis_fallback,
)
from .saq import (
    get_queue,
    init_saq,
    queue,
)

__all__ = [
    # Database
    "db_config",
    "get_engine",
    "get_session",
    "get_session_context",
    "init_db",
    "dispose_db",
    # Logging
    "logging_config",
    "init_logging",
    "shutdown_logging",
    # Redis
    "redis_config",
    "get_redis_client",
    "init_redis",
    "close_redis",
    "redis_fallback",
    "get_redis",
    "get_redis_context",
    # SAQ
    "get_queue",
    "init_saq",
    "queue",
]
