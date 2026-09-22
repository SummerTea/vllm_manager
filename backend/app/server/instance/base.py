"""Instance 跨类型复用：生命周期字段 Mixin 与状态机纯函数（无 DB/网络）。

多类型实例域（现阶段 vllm 表）的公共字段与状态规则集中于此：
- InstanceLifecycleMixin：实例生命周期公共列（node_id/state/target_state/port/记账/标签等）
- 纯函数：显存需求估算、worker 上报合并、失联联动、目标达成判定
"""

from sqlalchemy import BigInteger, Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.base.base_model import UniversalJSON, UniversalText
from app.server.instance.enum import InstanceStateEnum, InstanceTargetStateEnum

_GIB = 1024**3


class InstanceLifecycleMixin:
    """实例生命周期公共字段（多类型实例表共用）。"""

    node_id: Mapped[str] = mapped_column(
        String(64), index=True, comment="所属节点 ID"
    )
    state: Mapped[str] = mapped_column(
        String(32), default=InstanceStateEnum.PENDING.value, comment="实例状态"
    )
    target_state: Mapped[str] = mapped_column(
        String(32),
        default=InstanceTargetStateEnum.NONE.value,
        comment="目标状态（用户操作写入，达成后清回 none）",
    )
    state_message: Mapped[str | None] = mapped_column(
        UniversalText, nullable=True, comment="状态说明/失败原因"
    )
    port: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="服务端口"
    )
    gpu_indexes: Mapped[list] = mapped_column(
        UniversalJSON, default=list, comment="分配的 GPU 索引列表"
    )
    vram_claim: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="显存需求（Bytes）"
    )
    allocated_vram: Mapped[dict] = mapped_column(
        UniversalJSON, default=dict, comment="per-gpu 记账 {gpu_index: bytes}"
    )
    labels: Mapped[dict] = mapped_column(
        UniversalJSON, default=dict, comment="标签"
    )
    restart_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="重启次数"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否启用"
    )


def estimate_vram_claim(weight_bytes: int) -> int:
    """按模型权重估算显存需求（gpustack 实证公式：weight×1.2 + 2GiB）。"""
    return int(weight_bytes * 1.2) + 2 * _GIB


def apply_report(
    target_state: str,
    state_message: str | None,
    port: int | None,
    restart_count: int,
    reported_state: str,
    reported_state_message: str | None,
    reported_port: int | None,
    reported_restart_count: int | None,
) -> tuple[str, str | None, int | None, int]:
    """合并 worker 上报到实例字段，返回新字段元组（新 state 由调用处直接取上报值）。

    规则：
    - target_state：仅当 is_target_achieved(reported_state, 当前 target) 成立时清回 none，
      否则保留——防 stop/start 在途期间被中途上报（仍带旧 state）清掉 target，
      否则实例会卡在旧状态且不再收敛（B1 竞态）。
    - state_message：上报非 None 才更新，否则保留（防缺省上报抹掉已存错误信息）。
    - port/restart_count：仅上报非 None 时更新，否则保留原值。
    """
    new_target = (
        InstanceTargetStateEnum.NONE.value
        if is_target_achieved(reported_state, target_state)
        else target_state
    )
    return (
        new_target,
        reported_state_message
        if reported_state_message is not None
        else state_message,
        reported_port if reported_port is not None else port,
        reported_restart_count
        if reported_restart_count is not None
        else restart_count,
    )


def reconcile_unreachable(state: str) -> str:
    """节点失联联动：running 实例置为 unreachable，其余状态原样返回。"""
    if state == InstanceStateEnum.RUNNING.value:
        return InstanceStateEnum.UNREACHABLE.value
    return state


def is_target_achieved(state: str, target_state: str) -> bool:
    """目标状态是否达成（对账/完成判定用）。

    - target=starting：running 或 error 视为已达成（启动失败也是终态）
    - target=stopping：stopped 视为已达成
    - target=none：无目标，恒达成
    """
    if target_state == InstanceTargetStateEnum.STARTING.value:
        return state in (
            InstanceStateEnum.RUNNING.value,
            InstanceStateEnum.ERROR.value,
        )
    if target_state == InstanceTargetStateEnum.STOPPING.value:
        return state == InstanceStateEnum.STOPPED.value
    return True
