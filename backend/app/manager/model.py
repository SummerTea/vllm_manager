"""Manager 模块 Node 数据模型。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.base.base_model import (
    BaseModel,
    IdMixin,
    StatusMixin,
    UniversalJSON,
    UniversalText,
)
from app.config import app_config
from app.extensions.database import db_config
from app.manager.enum import NodeStateEnum


class Node(IdMixin, StatusMixin, BaseModel):
    """GPU 节点表。"""

    __tablename__ = f"{db_config.TABLE_NAME_PREFIX}_node"
    __table_args__ = (
        Index(
            "uix_node_machine_id",
            "machine_id",
            unique=True,
            postgresql_where=text("machine_id IS NOT NULL"),
        ),
        Index("uix_node_token", "token", unique=True),
        {"comment": "GPU 节点"},
    )

    machine_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="机器唯一标识（幂等注册主键）"
    )
    hostname: Mapped[str] = mapped_column(
        String(255), comment="主机名（幂等注册回退键）"
    )
    ip: Mapped[str] = mapped_column(String(64), comment="IP（可变属性）")
    advertise_address: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="对外可达地址"
    )
    agent_port: Mapped[int] = mapped_column(
        Integer, default=app_config.AGENT_DEFAULT_PORT, comment="agent 端口"
    )
    token: Mapped[str] = mapped_column(String(64), comment="Bearer 鉴权令牌")
    state: Mapped[str] = mapped_column(
        String(32), default=NodeStateEnum.PENDING.value, comment="节点状态"
    )
    unreachable: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="主动探测不可达标志"
    )
    heartbeat_time: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最后心跳时间"
    )
    state_message: Mapped[str | None] = mapped_column(
        UniversalText, nullable=True, comment="状态说明/失败原因"
    )
    labels: Mapped[dict] = mapped_column(
        UniversalJSON, default=dict, comment="标签"
    )
    system_reserved: Mapped[dict | None] = mapped_column(
        UniversalJSON, nullable=True, comment="系统预留 {ram, vram}"
    )
    status: Mapped[dict | None] = mapped_column(
        UniversalJSON, nullable=True, comment="节点状态载荷（心跳上报）"
    )

    def compute_state(self, grace_period: int) -> None:
        """根据心跳时间与探测标志派生节点状态（就地更新自身状态字段）。"""
        now = datetime.now()
        if self.heartbeat_time is None:
            self.state = NodeStateEnum.PENDING.value
            self.state_message = "尚未收到心跳"
        elif (now - self.heartbeat_time).total_seconds() > grace_period:
            self.state = NodeStateEnum.OFFLINE.value
            self.state_message = (
                f"心跳超时（最后心跳: {self.heartbeat_time.isoformat(sep=' ', timespec='seconds')}）"
            )
        elif self.unreachable:
            self.state = NodeStateEnum.UNREACHABLE.value
            self.state_message = "主动探测 /healthz 失败"
        else:
            self.state = NodeStateEnum.READY.value
            self.state_message = None
