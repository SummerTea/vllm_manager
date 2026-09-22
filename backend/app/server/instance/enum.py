"""Instance 子域业务枚举。"""

from app.base.base_enum import LabeledStrEnum


class InstanceTypeEnum(LabeledStrEnum):
    """实例类型枚举。"""

    VLLM = "vllm", "vLLM 推理实例"


class InstanceStateEnum(LabeledStrEnum):
    """实例状态枚举。"""

    PENDING = "pending", "待启动"
    STARTING = "starting", "启动中"
    RUNNING = "running", "运行中"
    STOPPED = "stopped", "已停止"
    ERROR = "error", "错误"
    UNREACHABLE = "unreachable", "不可达"


class InstanceTargetStateEnum(LabeledStrEnum):
    """实例目标状态枚举（用户操作写入，达成后清回 none）。"""

    NONE = "none", "无"
    STARTING = "starting", "启动"
    STOPPING = "stopping", "停止"
