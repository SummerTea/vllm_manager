"""
基础模块
提供项目的基础抽象和工具类
"""

from .base_crud import (
    BaseCrudService,
    ConflictException,
    # 异常类
    ResourceNotExistException,
    SoftDeleteCrudService,
)
from .base_enum import (
    BaseEnum,
    BaseIntEnum,
    BaseStrEnum,
    LabeledIntEnum,
)
from .base_model import (
    AuditMixin,
    AutoIdMixin,
    # 声明式基类
    Base,
    # 基础模型
    BaseModel,
    # Mixin 类
    IdMixin,
    SoftDeleteMixin,
    StatusMixin,
    TimestampMixin,
    UniversalJSON,
    # 自定义类型
    UniversalText,
    UniversalValue,
    UserTrackMixin,
    VersionedModel,
    VersionMixin,
)

__all__ = [
    # 声明式基类
    'Base',
    # 自定义类型
    'UniversalText',
    'UniversalJSON',
    'UniversalValue',
    # 主键 Mixin
    'IdMixin',
    'AutoIdMixin',
    # 功能 Mixin
    'SoftDeleteMixin',
    'StatusMixin',
    'TimestampMixin',
    'UserTrackMixin',
    'AuditMixin',
    'VersionMixin',
    # 基础模型
    'BaseModel',
    'VersionedModel',
    # 基础服务
    'BaseCrudService',
    'SoftDeleteCrudService',
    # 异常类
    'ResourceNotExistException',
    'ConflictException',
    # 枚举基类
    'BaseEnum',
    'BaseStrEnum',
    'BaseIntEnum',
    'LabeledIntEnum',
]
