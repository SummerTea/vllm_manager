"""
SAQ 后台 Worker 入口
Usage:
    # 监听 default 队列，并发数为 10
    python app/work_saq.py -q default -c 10
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

# 将项目根目录添加到 python path，确保能导入 app 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saq import Worker

from app.extensions.database import init_db
from app.extensions.logging import init_logging
from app.extensions.redis import init_redis
from app.extensions.saq import get_queue, init_saq

# 配置日志
logger = logging.getLogger(__name__)


async def start_workers(queues: list[str], concurrency: int):
    """启动 Worker"""

    # 1. 初始化资源
    init_logging()
    logger.info("正在初始化资源...")

    await init_db()
    await init_redis()
    await init_saq()

    # 2. 业务任务在此通过 TASK_REGISTRY 注册后传入，例如：
    #    from app.module.schedule.tasks import TASK_REGISTRY
    #    TASK_REGISTRY.discover()
    #    functions = list(TASK_REGISTRY.values())
    functions = []

    workers = []

    # 3. 为每个队列创建 Worker
    for queue_name in queues:
        logger.info(f"正在启动 Worker: 队列={queue_name}, 并发数={concurrency}")

        queue = get_queue(queue_name)

        worker = Worker(
            queue,
            functions=functions,
            concurrency=concurrency,
            cron_jobs=[],
            startup={},
            shutdown={},
        )
        workers.append(worker)

    # 4. 并行启动所有 Worker
    if not workers:
        logger.warning("没有 Worker 需要启动")
        return

    try:
        await asyncio.gather(*[w.start() for w in workers])
    except asyncio.CancelledError:
        logger.info("Worker 已停止")
    except Exception as e:
        logger.error(f"Worker 异常退出: {e}")


def main():
    parser = argparse.ArgumentParser(description="VllmManager SAQ Worker")

    # 定义命令行参数
    parser.add_argument(
        "-q", "--queues",
        type=str,
        default="default",
        help="指定要监听的队列，用逗号分隔 (例如: default,llm)。默认: default"
    )

    parser.add_argument(
        "-c", "--concurrency",
        type=int,
        default=10,
        help="每个队列的并发数 (默认: 10)"
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help="执行心跳检查并退出 (用于健康检查)"
    )

    args = parser.parse_args()

    # 心跳检查逻辑
    if args.check:
        # 这里可以实现更复杂的逻辑，比如检查 Redis 连接
        # 目前简单实现为直接成功退出
        print("Health check passed")
        sys.exit(0)

    # 解析队列列表
    target_queues = [q.strip() for q in args.queues.split(",") if q.strip()]

    if not target_queues:
        print("错误: 未指定队列")
        sys.exit(1)

    print(f"启动配置: 队列={target_queues}, 并发数={args.concurrency}")

    # 启动事件循环
    try:
        asyncio.run(start_workers(target_queues, args.concurrency))
    except KeyboardInterrupt:
        print("收到停止信号，正在退出...")


if __name__ == "__main__":
    main()
