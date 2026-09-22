"""Manager 模块业务枚举。"""

from app.base.base_enum import LabeledStrEnum


class NodeStateEnum(LabeledStrEnum):
    """节点状态枚举。"""

    PENDING = "pending", "待上线"  # 已注册、尚无心跳
    READY = "ready", "在线"
    OFFLINE = "offline", "失联"  # 心跳超时（compute_state 派生）
    UNREACHABLE = "unreachable", "不可达"  # 主动 /healthz 探测失败
