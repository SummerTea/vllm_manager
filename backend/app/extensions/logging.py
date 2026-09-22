"""
日志模块
提供应用日志配置，支持文件轮转和时区设置

使用方式:
    from app.extensions.logging import init_logging

    # 应用启动时初始化
    init_logging()

    # 使用标准 logging
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Hello World")
"""

import atexit
import logging
import os
import queue
import sys
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

from pydantic import Field, PositiveInt

from app.base.base_config import BASE_PATH, AppBaseConfig

# ============ 配置 ============

class LoggingConfig(AppBaseConfig):
    """日志配置"""

    LOG_LEVEL: str = Field(default="INFO", description="日志级别 (DEBUG / INFO / WARNING / ERROR / CRITICAL)")
    LOG_FILE: str | None = Field(default=f"{BASE_PATH}/logs/app.log",
        description="日志文件路径，支持 {pid} 和 {host_name} 占位符")
    LOG_FILE_MAX_SIZE: PositiveInt = Field(default=20, description="单个日志文件最大大小（MB）")
    LOG_FILE_BACKUP_COUNT: PositiveInt = Field(default=5, description="日志文件备份数量")
    LOG_FORMAT: str = Field(
        default="%(asctime)s.%(msecs)03d %(levelname)s [%(threadName)s] [%(filename)s:%(lineno)d] - %(message)s",
        description="日志格式")
    LOG_DATEFORMAT: str | None = Field(default="%Y-%m-%d %H:%M:%S", description="日期格式")
    LOG_TZ: str | None = Field(default="Asia/Shanghai", description="日志时区")


logging_config = LoggingConfig()

# 全局日志监听器引用，防止被垃圾回收
_log_listener: QueueListener | None = None


# ============ 初始化 ============

def init_logging() -> None:
    """
    初始化日志配置（异步/非阻塞模式）

    采用 QueueHandler + QueueListener 模式：
    1. 主线程使用 QueueHandler 将日志放入队列（非阻塞）
    2. 后台线程通过 QueueListener 从队列取出日志并写入文件/控制台
    3. 彻底消除文件 I/O 对 asyncio 事件循环的阻塞影响
    """
    global _log_listener

    if _log_listener:
        return

    # 1. 准备实际的 Handler（在后台线程运行）
    handlers: list[logging.Handler] = []

    # 文件处理器
    log_file = logging_config.LOG_FILE
    if log_file:
        # 替换占位符
        log_file = log_file.format(pid=os.getpid(),
            host_name=os.environ.get("HOSTNAME") or os.environ.get("CONTAINER_ID") or "local")

        # 创建目录
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        # 添加轮转文件处理器
        file_handler = RotatingFileHandler(filename=log_file, maxBytes=logging_config.LOG_FILE_MAX_SIZE * 1024 * 1024,
            backupCount=logging_config.LOG_FILE_BACKUP_COUNT, encoding="utf-8", )
        # 设置 Formatter
        formatter = logging.Formatter(fmt=logging_config.LOG_FORMAT, datefmt=logging_config.LOG_DATEFORMAT)
        # 时区设置需要应用到具体的 Handler
        if logging_config.LOG_TZ:
            _setup_timezone_for_formatter(formatter, logging_config.LOG_TZ)

        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_formatter = logging.Formatter(fmt=logging_config.LOG_FORMAT, datefmt=logging_config.LOG_DATEFORMAT)
    if logging_config.LOG_TZ:
        _setup_timezone_for_formatter(console_formatter, logging_config.LOG_TZ)
    console_handler.setFormatter(console_formatter)
    handlers.append(console_handler)

    # 2. 创建队列和监听器
    log_queue = queue.Queue(-1)  # 无界队列
    _log_listener = QueueListener(log_queue, *handlers, respect_handler_level=True)

    # 3. 启动监听器（后台线程）
    _log_listener.start()

    # 注册退出时的清理函数
    atexit.register(shutdown_logging)

    # 4. 配置根日志器使用 QueueHandler
    queue_handler = QueueHandler(log_queue)

    logging.basicConfig(level=logging_config.LOG_LEVEL, handlers=[queue_handler], force=True, )

    logging.info(f"异步日志初始化完成，级别: {logging_config.LOG_LEVEL} [QueueListener 模式]")


def shutdown_logging():
    """停止日志监听器"""
    global _log_listener
    if _log_listener:
        _log_listener.stop()
        _log_listener = None


def _setup_timezone_for_formatter(formatter: logging.Formatter, tz: str) -> None:
    """为特定的 Formatter 设置时区"""
    try:
        from datetime import datetime

        import pytz

        timezone = pytz.timezone(tz)

        def time_converter(seconds):
            return datetime.fromtimestamp(seconds, tz=timezone).timetuple()

        formatter.converter = time_converter

    except ImportError:
        pass  # 这里的错误会在主流程中被忽略，或者应该在早期检查
    except Exception:
        pass
