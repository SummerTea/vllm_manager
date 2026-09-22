"""Worker 域业务枚举（worker 内存态）。

上报值必须落在 server 侧 InstanceStateEnum（pending/starting/running/stopped/
error/unreachable）；STOPPING 为本地过渡态，report 时跳过——server 无此枚举会 422。
"""

from app.base.base_enum import LabeledStrEnum


class WorkerInstanceStateEnum(LabeledStrEnum):
    """worker 内存态枚举（Phase 2 生命周期使用，Phase 1 预定义）。"""

    PENDING = "pending", "待启动"
    STARTING = "starting", "启动中"
    RUNNING = "running", "运行中"
    STOPPING = "stopping", "停止中"  # 过渡态，上报时跳过（server 无此枚举）
    ERROR = "error", "错误"
