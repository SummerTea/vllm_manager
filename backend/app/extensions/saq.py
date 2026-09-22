"""
SAQ (Simple Async Queue) 扩展模块
提供基于 Redis 的轻量级异步任务队列

使用方式:
    from app.extensions.saq import queue

    # 定义任务 (普通函数)
    async def my_task(ctx, *, arg1, arg2):
        print(arg1, arg2)

    # 调用任务
    await queue.enqueue("my_task", arg1=1, arg2=2)
"""
import logging
from typing import Any

from saq.queue.redis import RedisQueue

from app.extensions.redis import get_redis_client

logger = logging.getLogger(__name__)


class SaqQueueProxy:
    """
    SAQ Queue 代理
    用于解决 import 时的单例引用问题，确保在 init_saq 后能正确访问到实例
    """
    def __init__(self):
        self._queues: dict[str, RedisQueue] = {}
        self._redis_client = None

    def initialize(self, redis_client):
        self._redis_client = redis_client
        # 初始化默认队列
        self.get_queue("default")

    def get_queue(self, name: str = "default") -> RedisQueue:
        """获取指定名称的队列实例"""
        if not self._redis_client:
            raise RuntimeError("SAQ 尚未初始化，请先调用 init_saq()")

        if name not in self._queues:
            # 队列命名约定: vllm_manager:queue:{name}
            # 如果是 "default"，为了兼容性，也可以用 "vllm_manager:queue:default"
            queue_key = f"vllm_manager:queue:{name}"
            self._queues[name] = RedisQueue(self._redis_client, name=queue_key)
        
        return self._queues[name]

    def __getattr__(self, name: str) -> Any:
        # 默认代理到 default 队列
        if "default" in self._queues:
            return getattr(self._queues["default"], name)
        raise RuntimeError(f"SAQ Queue 尚未初始化，无法访问属性 '{name}'。请确保应用已启动且 init_saq() 已被调用。")

    @property
    def is_initialized(self) -> bool:
        return self._redis_client is not None


# 全局 Queue 代理实例
queue = SaqQueueProxy()


def get_queue(name: str = "default") -> RedisQueue:
    """
    获取 SAQ 队列实例
    """
    return queue.get_queue(name)


async def init_saq() -> None:
    """
    初始化 SAQ
    需要在 init_redis 之后调用
    """
    # 获取现有的 Redis 客户端实例
    redis_client = await get_redis_client()

    # 初始化代理内部
    queue.initialize(redis_client)

    logger.info("SAQ initialized")
