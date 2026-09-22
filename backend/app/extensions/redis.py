"""
Redis 扩展模块
提供 FastAPI + Redis 异步客户端的连接和管理

支持模式:
- 单机模式 (Standalone)
- 哨兵模式 (Sentinel)

使用方式:
    from app.extensions.redis import get_redis, init_redis, close_redis

    # FastAPI 依赖注入
    @app.get("/cache")
    async def get_cache(redis: Redis = Depends(get_redis)):
        value = await redis.get("key")
        return {"value": value}

    # 手动获取客户端
    async with get_redis_context() as redis:
        await redis.set("key", "value")

    # 应用启动时初始化
    @app.on_event("startup")
    async def startup():
        await init_redis()

    # 应用关闭时清理
    @app.on_event("shutdown")
    async def shutdown():
        await close_redis()
"""

import functools
import logging
import ssl
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

from pydantic import Field, NonNegativeInt, PositiveFloat, PositiveInt, computed_field
from redis.asyncio import Redis, Sentinel
from redis.exceptions import RedisError

from app.base.base_config import AppBaseConfig
from app.exception import RedisException

logger = logging.getLogger(__name__)


# ============ 配置 ============


class RedisConfig(AppBaseConfig):
    """Redis 配置"""

    # 基础连接配置
    REDIS_HOST: str = Field(default="localhost", description="Redis 服务器地址")
    REDIS_PORT: PositiveInt = Field(default=6379, description="Redis 端口")
    REDIS_USERNAME: str | None = Field(default=None, description="Redis 用户名（Redis 6+ ACL）")
    REDIS_PASSWORD: str | None = Field(default=None, description="Redis 密码")
    REDIS_DB: NonNegativeInt = Field(default=0, description="Redis 数据库编号 (0-15)")

    # SSL 配置
    REDIS_USE_SSL: bool = Field(default=False, description="是否启用 SSL/TLS 连接")
    REDIS_SSL_CERT_REQS: str = Field(default="CERT_NONE",
                                     description="SSL 证书要求 (CERT_NONE / CERT_OPTIONAL / CERT_REQUIRED)")
    REDIS_SSL_CA_CERTS: str | None = Field(default=None, description="CA 证书路径")
    REDIS_SSL_CERTFILE: str | None = Field(default=None, description="客户端证书路径")
    REDIS_SSL_KEYFILE: str | None = Field(default=None, description="客户端私钥路径")

    # 哨兵模式配置
    REDIS_USE_SENTINEL: bool = Field(default=False, description="是否启用哨兵模式")
    REDIS_SENTINELS: str | None = Field(default=None, description="哨兵节点列表，格式: host1:port1,host2:port2")
    REDIS_SENTINEL_SERVICE_NAME: str | None = Field(default=None, description="哨兵服务名称（主节点名称）")
    REDIS_SENTINEL_USERNAME: str | None = Field(default=None, description="哨兵认证用户名")
    REDIS_SENTINEL_PASSWORD: str | None = Field(default=None, description="哨兵认证密码")
    REDIS_SENTINEL_SOCKET_TIMEOUT: PositiveFloat = Field(default=0.5, description="哨兵连接超时时间（秒）")

    # 连接池配置
    REDIS_MAX_CONNECTIONS: PositiveInt = Field(default=50, description="连接池最大连接数")
    REDIS_SOCKET_TIMEOUT: PositiveFloat = Field(default=5.0, description="Socket 超时时间（秒）")
    REDIS_SOCKET_CONNECT_TIMEOUT: PositiveFloat = Field(default=5.0, description="连接超时时间（秒）")
    REDIS_SOCKET_KEEPALIVE: bool = Field(default=True, description="是否启用 TCP KeepAlive")

    # 编码配置
    REDIS_ENCODING: str = Field(default="utf-8", description="字符编码")
    REDIS_ENCODING_ERRORS: str = Field(default="strict",
                                       description="编码错误处理策略 (strict / ignore / replace / backslashreplace)")
    REDIS_DECODE_RESPONSES: bool = Field(default=True, description="是否自动解码响应为字符串")

    # 高级配置
    REDIS_CLIENT_NAME: str | None = Field(default=None, description="客户端名称（用于监控和调试，如: vllm_manager-api-01）")
    REDIS_HEALTH_CHECK_INTERVAL: PositiveInt = Field(default=30, description="健康检查间隔（秒），0 表示禁用")
    REDIS_RETRY_ON_TIMEOUT: bool = Field(default=False, description="连接超时时是否自动重试")

    @computed_field
    @property
    def REDIS_PASSWORD_DECRYPTED(self) -> str | None:
        """Redis 密码（原样返回，密码应使用明文或由部署层注入）"""
        return self.REDIS_PASSWORD

    @computed_field
    @property
    def REDIS_SENTINEL_PASSWORD_DECRYPTED(self) -> str | None:
        """哨兵密码（原样返回，密码应使用明文或由部署层注入）"""
        return self.REDIS_SENTINEL_PASSWORD


# ============ 全局实例 ============

redis_config = RedisConfig()

_redis_client: Redis | None = None


# ============ 连接管理 ============


def _get_ssl_context() -> ssl.SSLContext | None:
    """构建 SSL 上下文"""
    if not redis_config.REDIS_USE_SSL:
        return None

    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    # 设置证书验证级别
    cert_reqs_map = {"CERT_NONE": ssl.CERT_NONE, "CERT_OPTIONAL": ssl.CERT_OPTIONAL,
                     "CERT_REQUIRED": ssl.CERT_REQUIRED, }
    ssl_context.check_hostname = False
    ssl_context.verify_mode = cert_reqs_map.get(redis_config.REDIS_SSL_CERT_REQS, ssl.CERT_NONE)

    # 加载 CA 证书
    if redis_config.REDIS_SSL_CA_CERTS:
        ssl_context.load_verify_locations(cafile=redis_config.REDIS_SSL_CA_CERTS)

    # 加载客户端证书
    if redis_config.REDIS_SSL_CERTFILE and redis_config.REDIS_SSL_KEYFILE:
        ssl_context.load_cert_chain(certfile=redis_config.REDIS_SSL_CERTFILE, keyfile=redis_config.REDIS_SSL_KEYFILE, )

    return ssl_context


def _get_base_connection_params() -> dict[str, Any]:
    """获取基础连接参数"""
    params = {"username": redis_config.REDIS_USERNAME, "password": redis_config.REDIS_PASSWORD_DECRYPTED,
              "db": redis_config.REDIS_DB, "encoding": redis_config.REDIS_ENCODING,
              "encoding_errors": redis_config.REDIS_ENCODING_ERRORS,
              "decode_responses": redis_config.REDIS_DECODE_RESPONSES,
              "max_connections": redis_config.REDIS_MAX_CONNECTIONS,
              "socket_timeout": redis_config.REDIS_SOCKET_TIMEOUT,
              "socket_connect_timeout": redis_config.REDIS_SOCKET_CONNECT_TIMEOUT,
              "socket_keepalive": redis_config.REDIS_SOCKET_KEEPALIVE,
              "retry_on_timeout": redis_config.REDIS_RETRY_ON_TIMEOUT, }

    # 添加客户端名称
    if redis_config.REDIS_CLIENT_NAME:
        params["client_name"] = redis_config.REDIS_CLIENT_NAME

    # 添加健康检查
    if redis_config.REDIS_HEALTH_CHECK_INTERVAL > 0:
        params["health_check_interval"] = redis_config.REDIS_HEALTH_CHECK_INTERVAL

    # 添加 SSL 配置
    if redis_config.REDIS_USE_SSL:
        params["ssl"] = True
        params["ssl_cert_reqs"] = redis_config.REDIS_SSL_CERT_REQS
        if redis_config.REDIS_SSL_CA_CERTS:
            params["ssl_ca_certs"] = redis_config.REDIS_SSL_CA_CERTS
        if redis_config.REDIS_SSL_CERTFILE:
            params["ssl_certfile"] = redis_config.REDIS_SSL_CERTFILE
        if redis_config.REDIS_SSL_KEYFILE:
            params["ssl_keyfile"] = redis_config.REDIS_SSL_KEYFILE

    return params


def _create_sentinel_client() -> Redis:
    """创建哨兵模式客户端"""
    if not redis_config.REDIS_SENTINELS:
        raise ValueError("REDIS_SENTINELS 必须设置（格式: host1:port1,host2:port2）")

    if not redis_config.REDIS_SENTINEL_SERVICE_NAME:
        raise ValueError("REDIS_SENTINEL_SERVICE_NAME 必须设置")

    # 解析哨兵节点
    sentinel_nodes = [(node.split(":")[0], int(node.split(":")[1])) for node in redis_config.REDIS_SENTINELS.split(",")]

    # 哨兵客户端配置
    sentinel_kwargs = {"socket_timeout": redis_config.REDIS_SENTINEL_SOCKET_TIMEOUT, }
    if redis_config.REDIS_SENTINEL_USERNAME:
        sentinel_kwargs["username"] = redis_config.REDIS_SENTINEL_USERNAME
    if redis_config.REDIS_SENTINEL_PASSWORD_DECRYPTED:
        sentinel_kwargs["password"] = redis_config.REDIS_SENTINEL_PASSWORD_DECRYPTED

    # 创建哨兵客户端
    sentinel = Sentinel(sentinel_nodes, sentinel_kwargs=sentinel_kwargs, )

    # 获取主节点连接参数
    connection_params = _get_base_connection_params()

    # 获取主节点客户端
    master = sentinel.master_for(redis_config.REDIS_SENTINEL_SERVICE_NAME, **connection_params, )

    logger.info(f"Redis 哨兵模式客户端已创建（连接将在首次操作时建立）: "
                f"service={redis_config.REDIS_SENTINEL_SERVICE_NAME}, sentinels={redis_config.REDIS_SENTINELS}")

    return master


def _create_standalone_client() -> Redis:
    """创建单机模式客户端"""
    connection_params = _get_base_connection_params()

    client = Redis(host=redis_config.REDIS_HOST, port=redis_config.REDIS_PORT, **connection_params, )

    logger.info(f"Redis 单机模式客户端已创建（连接将在首次操作时建立）: {redis_config.REDIS_HOST}:{redis_config.REDIS_PORT}")

    return client


async def get_redis_client() -> Redis:
    """
    获取 Redis 客户端（单例模式）

    Returns:
        Redis 异步客户端实例

    Raises:
        RuntimeError: 如果客户端未初始化
    """
    global _redis_client

    if _redis_client is None:
        raise RuntimeError("Redis 客户端未初始化，请先调用 init_redis()")

    return _redis_client


# ============ FastAPI 集成 ============


async def get_redis() -> AsyncGenerator[Redis, None]:
    """
    FastAPI 依赖注入用的 Redis 客户端生成器

    使用方式:
        @app.get("/cache")
        async def get_cache(redis: Redis = Depends(get_redis)):
            value = await redis.get("key")
            return {"value": value}
    """
    client = await get_redis_client()
    try:
        yield client
    except Exception as e:
        logger.error(f"Redis 操作异常: {e}", exc_info=True)
        raise RedisException(f"Redis 操作异常: {e}", details={"original_error": str(e)}) from e


@asynccontextmanager
async def get_redis_context() -> AsyncGenerator[Redis, None]:
    """
    上下文管理器方式获取 Redis 客户端

    使用方式:
        async with get_redis_context() as redis:
            await redis.set("key", "value")
    """
    client = await get_redis_client()
    try:
        yield client
    except Exception as e:
        logger.error(f"Redis 操作异常: {e}", exc_info=True)
        raise RedisException(f"Redis 操作异常: {e}", details={"original_error": str(e)}) from e


# ============ 初始化和清理 ============


async def init_redis() -> None:
    """
    初始化 Redis 客户端

    根据配置自动选择单机或哨兵模式
    """
    global _redis_client

    if _redis_client is not None:
        logger.warning("Redis 客户端已初始化，跳过重复初始化")
        return

    try:
        # 根据配置创建客户端
        if redis_config.REDIS_USE_SENTINEL:
            _redis_client = _create_sentinel_client()
        else:
            _redis_client = _create_standalone_client()

        # 测试连接
        await _redis_client.ping()
        mode = "哨兵模式" if redis_config.REDIS_USE_SENTINEL else "单机模式"
        logger.info(f"Redis {mode}连接测试成功")

    except Exception as e:
        logger.error(f"Redis 初始化失败: {e}", exc_info=True)
        raise RedisException(f"Redis 初始化失败: {e}", details={"original_error": str(e)}) from e


async def close_redis() -> None:
    """
    关闭 Redis 客户端连接

    适用于应用关闭时清理资源
    """
    global _redis_client

    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
        logger.info("Redis 连接已关闭")


# ============ 工具函数 ============


def redis_fallback(default_return: Any = None):
    """
    Redis 操作异常降级装饰器

    当 Redis 操作失败时，返回默认值而不抛出异常

    Args:
        default_return: Redis 操作失败时的返回值，默认为 None

    使用方式:
        @redis_fallback(default_return=[])
        async def get_cached_list(redis: Redis) -> list:
            data = await redis.lrange("my_list", 0, -1)
            return data
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except RedisError as e:
                func_name = getattr(func, "__name__", "Unknown")
                logger.warning(f"Redis 操作降级 [{func_name}]: {e}", exc_info=True)
                return default_return

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except RedisError as e:
                func_name = getattr(func, "__name__", "Unknown")
                logger.warning(f"Redis 操作降级 [{func_name}]: {e}", exc_info=True)
                return default_return

        # 根据函数类型返回对应的包装器
        import inspect
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator
