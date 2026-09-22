"""基础枚举模块，提供增强的枚举基类。"""

from enum import Enum, IntEnum, StrEnum
from typing import TYPE_CHECKING, Any, TypeVar

T = TypeVar("T", bound="BaseEnumMixin")


class BaseEnumMixin:
    """枚举增强 Mixin，提供常用工具方法。"""

    # 声明 Enum 成员属性，让 mypy 知道这些属性存在
    if TYPE_CHECKING:
        name: str
        value: Any
        __members__: dict[str, Any]

    @classmethod
    def from_value(cls: type[T], value: Any) -> T | None:
        """根据值获取枚举成员。"""
        for member in cls:  # type: ignore[attr-defined]
            if member.value == value:
                return member
        return None

    @classmethod
    def from_name(cls: type[T], name: str) -> T | None:
        """根据名称获取枚举成员。"""
        return cls.__members__.get(name)  # type: ignore[attr-defined]

    @classmethod
    def names(cls) -> list[str]:
        """获取所有成员名称。"""
        return [m.name for m in cls]  # type: ignore[attr-defined]

    @classmethod
    def values(cls) -> list[Any]:
        """获取所有成员值。"""
        return [m.value for m in cls]  # type: ignore[attr-defined]

    @classmethod
    def items(cls: type[T]) -> list[tuple[str, Any]]:
        """获取所有 (name, value) 元组。"""
        return [(m.name, m.value) for m in cls]  # type: ignore[attr-defined]

    @classmethod
    def has_name(cls, name: str) -> bool:
        """检查名称是否有效。"""
        return name in cls.__members__  # type: ignore[attr-defined]

    @classmethod
    def has_value(cls, value: Any) -> bool:
        """检查值是否有效。"""
        return any(m.value == value for m in cls)  # type: ignore[attr-defined]

    @classmethod
    def to_options(cls) -> list[dict[str, Any]]:
        """转换为前端下拉选项格式 [{value, label}]。"""
        return [{"value": m.value, "label": m.label if hasattr(m, "label") else m.name} for m in cls]  # type: ignore[attr-defined]

    @classmethod
    def to_key_value_pair(cls) -> list[dict[str, Any]]:
        """转换为 [ {key, value} ] 列表。"""
        return [{"key": m.name, "value": str(m.value)} for m in cls]  # type: ignore[attr-defined]

    def __str__(self) -> str:
        return str(self.value)  # type: ignore[attr-defined]

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}.{self.name}: {self.value}>"  # type: ignore[attr-defined]


class BaseEnum(BaseEnumMixin, Enum):
    """通用枚举基类。"""



class BaseIntEnum(BaseEnumMixin, IntEnum):
    """整型枚举基类，值可直接用于数值运算。"""



class BaseStrEnum(BaseEnumMixin, StrEnum):
    """字符串枚举基类，值可直接用于字符串操作。"""



class LabeledEnumMixin:
    """带标签的枚举 Mixin，支持 (value, label) 形式定义。

    Usage:
        class Status(LabeledIntEnum):
            PENDING = 0, "待处理"
            APPROVED = 1, "已通过"
            REJECTED = 2, "已拒绝"

        Status.PENDING.label  # "待处理"
        Status.to_options()   # [{"value": 0, "label": "待处理"}, ...]
    """

    label: str
    value: Any
    name: str

    @classmethod
    def labels(cls) -> list[str]:
        """获取所有标签。"""
        return [m.label for m in cls]  # type: ignore[attr-defined]

    @classmethod
    def to_options(cls) -> list[dict[str, Any]]:
        """转换为前端下拉选项格式 [{value, label}]。"""
        return [{"value": m.value, "label": m.label} for m in cls]  # type: ignore[attr-defined]


class LabeledIntEnum(int, LabeledEnumMixin, Enum):
    """带标签的整型枚举。

    Usage:
        class OrderStatus(LabeledIntEnum):
            PENDING = 0, "待支付"
            PAID = 1, "已支付"
            SHIPPED = 2, "已发货"
    """

    def __new__(cls, value: int, label: str = ""):
        obj = int.__new__(cls, value)
        obj._value_ = value
        obj.label = label or str(value)
        return obj


class LabeledStrEnum(str, LabeledEnumMixin, Enum):  # noqa: UP042
    """带标签的字符串枚举。

    Usage:
        class TaskType(LabeledStrEnum):
            EMAIL = "email", "邮件任务"
            SMS = "sms", "短信任务"
    """

    def __new__(cls, value: str, label: str = ""):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.label = label or value
        return obj


class DescribedEnumMixin:
    """带标签和描述的枚举 Mixin，支持 (value, label, description) 形式定义。

    Usage:
        class Tool(DescribedStrEnum):
            READ = "read", "读取文件", "读取文件内容"
            BASH = "bash", "执行命令", "在终端执行 shell 命令"

        Tool.READ.label        # "读取文件"
        Tool.READ.description  # "读取文件内容"
    """

    label: str
    description: str
    value: Any
    name: str

    @classmethod
    def values(cls) -> list[Any]:
        """获取所有成员值。"""
        return [m.value for m in cls]  # type: ignore[attr-defined]

    @classmethod
    def to_options(cls) -> list[dict[str, Any]]:
        """转换为前端下拉选项格式 [{value, label, description}]。"""
        return [
            {"value": m.value, "label": m.label, "description": m.description}
            for m in cls  # type: ignore[attr-defined]
        ]


class DescribedStrEnum(str, DescribedEnumMixin, Enum):  # noqa: UP042
    """带标签和描述的字符串枚举。

    Usage:
        class PiBuiltinTool(DescribedStrEnum):
            READ = "read", "读取文件", "读取指定文件的内容"
            BASH = "bash", "执行命令", "在终端执行 shell 命令"
            EDIT = "edit", "编辑文件", "编辑文件的部分内容"
    """

    def __new__(cls, value: str, label: str = "", description: str = ""):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.label = label or value
        obj.description = description or ""
        return obj


class IconedEnumMixin:
    """带图标字段的枚举 Mixin，支持 (value, label, description, icon) 形式定义。"""

    icon: str

    @classmethod
    def to_options(cls) -> list[dict[str, Any]]:
        """转换为前端下拉选项格式 [{value, label, description, icon}]。"""
        return [
            {"value": m.value, "label": m.label, "description": m.description, "icon": m.icon}
            for m in cls  # type: ignore[attr-defined]
        ]


class IconedDescribedStrEnum(str, IconedEnumMixin, DescribedEnumMixin, Enum):  # noqa: UP042
    """带标签、描述和字符图标的字符串枚举。

    Usage:
        class Tool(IconedDescribedStrEnum):
            READ_FILE = "read_file", "读取文件", "读取文件内容", "📄"
            TERMINAL = "terminal", "终端", "执行 shell 命令", "💻"
    """

    def __new__(cls, value: str, label: str = "", description: str = "", icon: str = ""):
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.label = label or value
        obj.description = description or ""
        obj.icon = icon or ""
        return obj
