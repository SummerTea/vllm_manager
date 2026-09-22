"""
日期时间格式化工具函数

提供统一的日期时间格式化方法，用于 UI 展示
"""

from datetime import datetime


def format_datetime(
    dt: datetime | None,
    fmt: str = "%Y-%m-%d %H:%M:%S",
    default: str = "-"
) -> str:
    """
    统一的日期时间格式化

    Args:
        dt: 日期时间对象，None 时返回 default
        fmt: 格式化字符串，默认 "%Y-%m-%d %H:%M:%S"
        default: 当 dt 为 None 时返回的默认值

    Returns:
        格式化后的日期时间字符串

    Examples:
        >>> format_datetime(datetime.now())
        '2026-01-20 23:30:00'
        >>> format_datetime(None)
        '-'
    """
    if dt is None:
        return default
    return dt.strftime(fmt)


def format_short_time(dt: datetime | None, default: str = "-") -> str:
    """
    短时间格式（月-日 时:分）

    适用于表格列表等空间有限的场景

    Args:
        dt: 日期时间对象
        default: 默认值

    Returns:
        格式化后的短时间字符串，如 "01-20 23:30"

    Examples:
        >>> format_short_time(datetime.now())
        '01-20 23:30'
    """
    return format_datetime(dt, "%m-%d %H:%M", default)


def format_date(dt: datetime | None, default: str = "-") -> str:
    """
    日期格式（年-月-日）

    Args:
        dt: 日期时间对象
        default: 默认值

    Returns:
        格式化后的日期字符串，如 "2026-01-20"
    """
    return format_datetime(dt, "%Y-%m-%d", default)


def format_time(dt: datetime | None, default: str = "-") -> str:
    """
    时间格式（时:分:秒）

    Args:
        dt: 日期时间对象
        default: 默认值

    Returns:
        格式化后的时间字符串，如 "23:30:00"
    """
    return format_datetime(dt, "%H:%M:%S", default)
