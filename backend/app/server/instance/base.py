"""Instance 跨类型复用：生命周期字段 Mixin 与状态机纯函数（无 DB/网络）。

多类型实例域（现阶段 vllm 表）的公共字段与状态规则集中于此：
- InstanceLifecycleMixin：实例生命周期公共列（node_id/state/target_state/port/记账/标签等）
- 纯函数：显存需求估算、worker 上报合并、失联联动、目标达成判定
"""

from sqlalchemy import BigInteger, Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.base.base_model import UniversalJSON, UniversalText
from app.server.instance.enum import (
    InstanceStateEnum,
    InstanceTargetStateEnum,
    InstanceTaskEnum,
)

_GIB = 1024**3
_MIB = 1024**2


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
    target_retry_count: Mapped[int] = mapped_column(
        Integer, default=0, comment="目标态指令重下发次数（对账收敛用）"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否启用"
    )


def estimate_vram_claim(weight_bytes: int, task: str = "auto") -> int:
    """按模型权重估算显存需求（gpustack 实证口径）。

    口径边界：仅 `embedding`/`rerank` 两个 task 值走 512MiB 小模型口径；
    其余任意值（含 vLLM 侧 `embed`、未知值）一律走 2GiB LLM 口径。
    - embedding/rerank（非 LLM 小模型）：weight×1.2 + 512MiB（上下文/批次开销小）
    - 其余（auto/llm 等）：weight×1.2 + 2GiB（LLM KV cache 与解码开销）
    task 缺省走 LLM 口径，向后兼容既有调用。
    """
    if task in (
        InstanceTaskEnum.EMBEDDING.value,
        InstanceTaskEnum.RERANK.value,
    ):
        return int(weight_bytes * 1.2) + 512 * _MIB
    return int(weight_bytes * 1.2) + 2 * _GIB


def apply_report(
    current_state: str,
    target_state: str,
    state_message: str | None,
    port: int | None,
    restart_count: int,
    reported_state: str,
    reported_state_message: str | None,
    reported_port: int | None,
    reported_restart_count: int | None,
) -> tuple[str, str, str | None, int | None, int]:
    """合并 worker 上报到实例字段，返回 (新 state, 新 target, 新 message, 新 port, 新 restart)。

    规则：
    - new_state：取上报值，但受 M2 stopped 防复活守卫拦截（见下）。
    - target_state：仅当 is_target_achieved(reported_state, 当前 target) 成立时清回 none，
      否则保留——防 stop/start 在途期间被中途上报（仍带旧 state）清掉 target（B1 竞态）。
    - state_message：上报非 None 才更新，否则保留（防缺省上报抹掉已存错误信息）。
    - port/restart_count：仅上报非 None 时更新，否则保留原值。

    M2 守卫（stopped 终态防复活）：无目标意图（target=none）时，已 stopped 实例不接受
    任何非 stopped 上报——防 stop 收敛后，在途旧 running/error 上报晚到复活实例与占账。
    有 target 意图（starting/stopping）期间不拦截（start 从 stopped 发起即依赖此路径）。
    """
    new_target = (
        InstanceTargetStateEnum.NONE.value
        if is_target_achieved(reported_state, target_state)
        else target_state
    )
    if (
        target_state == InstanceTargetStateEnum.NONE.value
        and current_state == InstanceStateEnum.STOPPED.value
        and reported_state != InstanceStateEnum.STOPPED.value
    ):
        # 守卫丢弃时返回 new_target（守卫触发时 target==none，new_target==none，
        # 与正常路径对称，消隐 is_target_achieved 对 none 语义变化的隐性不一致）
        return (
            current_state,
            new_target,
            state_message,
            port,
            restart_count,
        )
    return (
        reported_state,
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
