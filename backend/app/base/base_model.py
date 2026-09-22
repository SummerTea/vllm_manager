"""
SQLAlchemy 基础模型模块
提供通用的数据库模型基类、Mixin 和自定义字段类型

适用于样板工程架构（FastAPI + SQLAlchemy 2.0 声明式，PostgreSQL）
"""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
    TypeDecorator,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from uuid_utils import uuid7

# ============ 自定义类型 ============

class UniversalText(TypeDecorator[str]):
    """
    通用长文本类型（PostgreSQL: TEXT）
    """
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(Text)


class UniversalJSON(TypeDecorator[dict | list]):
    """
    跨场景 JSON 类型（PostgreSQL: JSONB）

    自动处理 Python dict/list 与 JSONB 的序列化/反序列化
    """
    impl = JSONB
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(JSONB)

    def process_bind_param(self, value: Any, dialect) -> Any:
        """Python 对象 -> 数据库存储值"""
        return value

    def process_result_value(self, value: Any, dialect) -> Any:
        """数据库存储值 -> Python 对象"""
        return value


class UniversalValue(TypeDecorator[dict | list | str]):
    """
    通用值类型，支持存储 dict、list 或 str

    存储策略:
    - dict/list: JSON 序列化后存储
    - str: 直接存储原始字符串

    读取时通过尝试 JSON 解析来判断类型
    """
    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(Text)

    def process_bind_param(self, value: Any, dialect) -> Any:
        """Python 对象 -> 数据库存储值"""
        if value is None:
            return None
        # dict/list 需要序列化为 JSON 字符串
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        # str 直接存储
        return value

    def process_result_value(self, value: Any, dialect) -> Any:
        """数据库存储值 -> Python 对象"""
        if value is None:
            return None
        if isinstance(value, str):
            # 尝试 JSON 解析，如果成功则返回 dict/list
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
            # 解析失败或结果不是 dict/list，返回原始字符串
            return value
        return value


# ============ 声明式基类 ============

ID_FIELD_NAME = "id"
COMMON_TAIL_FIELD_ORDER = (
    "is_active",
    "is_deleted",
    "deleted_at",
    "deleted_by",
    "lock_version",
    "created_at",
    "updated_at",
    "created_by",
    "updated_by",
)


def _get_ordered_column_names(column_names: list[str]) -> list[str]:
    """Return CREATE TABLE column order: id -> business -> common tail."""
    id_columns = [ID_FIELD_NAME] if ID_FIELD_NAME in column_names else []
    common_tail_columns = [
        field_name
        for field_name in COMMON_TAIL_FIELD_ORDER
        if field_name in column_names
    ]
    reserved_columns = set(id_columns) | set(common_tail_columns)
    business_columns = [
        column_name
        for column_name in column_names
        if column_name not in reserved_columns
    ]
    return id_columns + business_columns + common_tail_columns


def _ordered_table_cls(*args: Any, **kwargs: Any) -> Table:
    table_name, metadata, *table_args = args
    column_args = [arg for arg in table_args if isinstance(arg, Column)]
    other_args = [arg for arg in table_args if not isinstance(arg, Column)]

    ordered_column_names = _get_ordered_column_names(
        [column.name for column in column_args]
    )
    columns_by_name = {column.name: column for column in column_args}
    ordered_columns = [columns_by_name[name] for name in ordered_column_names]

    return Table(table_name, metadata, *ordered_columns, *other_args, **kwargs)


class Base(DeclarativeBase):
    """
    声明式基类

    统一列顺序：id -> 业务字段 -> 公共尾部字段（状态/软删/时间戳/审计）。
    业务模型应继承 BaseModel 或 Base，并定义 __tablename__。
    """
    @classmethod
    def __table_cls__(cls, *args: Any, **kwargs: Any) -> Table:
        return _ordered_table_cls(*args, **kwargs)


# ============ Mixin 类 ============

class IdMixin:
    """
    主键 Mixin
    提供 UUID7 主键字段

    使用 UUID7 的优势:
    - 时间有序：前 48 位是毫秒级时间戳，天然支持按时间排序
    - 分布式友好：无需协调即可生成唯一 ID
    - 数据库索引友好：有序性提升 B-tree 索引性能
    - 不暴露业务数据量
    """
    id: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        default=lambda: uuid7().hex,
        comment="主键",
    )


class AutoIdMixin:
    """
    自增主键 Mixin
    提供自增整数主键字段

    适用于:
    - 单机部署
    - 对 ID 连续性有要求的场景
    """
    id: Mapped[int | None] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="自增主键"
    )


class SoftDeleteMixin:
    """
    软删除 Mixin
    提供逻辑删除功能，数据不会被物理删除

    字段:
    - is_deleted: 是否已删除
    - deleted_at: 删除时间
    - deleted_by: 删除者
    """
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否已删除"
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="删除时间"
    )
    deleted_by: Mapped[str | None] = mapped_column(
        String(36), nullable=True, comment="删除者"
    )


class StatusMixin:
    """
    状态 Mixin
    提供启用/禁用状态管理

    字段:
    - is_active: 是否启用（默认 True）
    """
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")


class TimestampMixin:
    """
    时间戳 Mixin
    提供 created_at 和 updated_at 字段
    """
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, comment="创建时间"
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None, onupdate=datetime.now, comment="更新时间"
    )


class UserTrackMixin:
    """
    用户追踪 Mixin
    提供 created_by 和 updated_by 字段
    """
    created_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="创建者"
    )
    updated_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="更新者"
    )


class AuditMixin(TimestampMixin, UserTrackMixin):
    """
    审计 Mixin
    组合时间戳和用户追踪功能
    """


class VersionMixin:
    """
    乐观锁 Mixin
    提供 lock_version 字段用于并发控制

    使用整数自增方案:
    - 每次更新时 lock_version + 1
    - 更新条件: WHERE id = ? AND lock_version = ?
    - 若影响行数为 0，说明数据已被其他事务修改
    """
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, comment="乐观锁版本号"
    )

    @declared_attr
    def __mapper_args__(cls):
        return {
            "version_id_col": cls.lock_version
        }


# ============ 基础模型 ============

class BaseModel(AuditMixin, Base):
    """
    通用基础模型（抽象基类，不建表）

    包含:
    - 时间戳（created_at, updated_at）
    - 用户追踪（created_by, updated_by）

    使用示例:
        class User(IdMixin, BaseModel):
            __tablename__ = "users"
            username: Mapped[str] = mapped_column(String(100))
    """
    __abstract__ = True


class VersionedModel(BaseModel, VersionMixin):
    """
    带乐观锁的基础模型（抽象基类，不建表）

    包含 BaseModel 的所有字段，外加:
    - 乐观锁（lock_version）

    适用于需要并发控制的场景
    """
    __abstract__ = True
