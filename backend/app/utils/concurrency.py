"""
并发工具模块

提供异步并发执行工具，仅依赖 anyio（同步函数线程化、异步并发限流）。
"""
import asyncio
import functools
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

import anyio.to_thread

P = ParamSpec("P")
T = TypeVar("T")


async def make_async(
    func: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs
) -> T:
    """
    将同步函数转为异步执行（在工作线程中运行）

    用于处理：
    1. CPU 密集型任务（加密、哈希、数据处理）
    2. 无法改造的第三方同步库
    3. 阻塞 I/O

    Args:
        func: 同步函数
        *args: 位置参数
        **kwargs: 关键字参数

    Returns:
        函数执行结果

    Example:
        def compute_hash(data: bytes) -> str:
            import hashlib
            return hashlib.sha256(data).hexdigest()

        hash_result = await make_async(compute_hash, b"hello")
    """
    if kwargs:
        func = functools.partial(func, **kwargs)
    return await anyio.to_thread.run_sync(func, *args)


async def async_concurrent_execute(
    fn: Callable,
    data: list[Any],
    max_workers: int = 10,
    return_exceptions: bool = False
) -> list[Any]:
    """
    异步并发执行函数

    使用 asyncio.gather 实现真正的异步并发。
    自动适配同步/异步函数。

    Args:
        fn: 要执行的函数（同步或异步均可）
        data: 输入数据列表，支持 dict/tuple/list/基础类型
        max_workers: 最大并发数（用于限流）
        return_exceptions: 是否返回异常而非抛出

    Returns:
        执行结果列表

    Example:
        async def fetch_user(user_id: int):
            return await get_user(user_id)

        user_ids = [1, 2, 3, 4, 5]
        users = await async_concurrent_execute(fetch_user, user_ids)
    """

    async def process_item(item):
        """处理单个数据项"""
        try:
            # 根据数据类型调用函数
            if isinstance(item, dict):
                result = fn(**item)
            elif isinstance(item, (tuple, list)):
                result = fn(*item)
            else:
                result = fn(item)

            # 如果返回的是协程，await 它
            if asyncio.iscoroutine(result):
                return await result
            return result

        except Exception as e:
            if return_exceptions:
                return e
            raise

    # 使用信号量限制并发数
    semaphore = asyncio.Semaphore(max_workers)

    async def limited_process(item):
        async with semaphore:
            return await process_item(item)

    # 并发执行所有任务
    tasks = [limited_process(item) for item in data]
    return await asyncio.gather(*tasks, return_exceptions=return_exceptions)
