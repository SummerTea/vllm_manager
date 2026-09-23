"""Server 模块 vLLM 实例数据模型。"""

from sqlalchemy import BigInteger, Boolean, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.base.base_model import BaseModel, IdMixin, UniversalJSON, UniversalText
from app.extensions.database import db_config
from app.server.instance.base import InstanceLifecycleMixin
from app.server.instance.enum import InstanceTaskEnum


class VllmStartTemplate(IdMixin, BaseModel):
    """vLLM 启动模板表（docker run 模板，create 时快照到实例）。"""

    __tablename__ = f"{db_config.TABLE_NAME_PREFIX}_vllm_start_template"
    __table_args__ = (
        Index("uix_vllm_start_template_key", "template_key", unique=True),
        {"comment": "vLLM 启动模板"},
    )

    template_key: Mapped[str] = mapped_column(
        String(64), comment="模板唯一键"
    )
    instance_type: Mapped[str] = mapped_column(
        String(32), default="vllm", comment="实例类型"
    )
    template: Mapped[str] = mapped_column(
        UniversalText, comment="docker 启动模板（{var} 占位）"
    )
    description: Mapped[str | None] = mapped_column(
        UniversalText, nullable=True, comment="模板说明"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="是否启用"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否默认模板"
    )
    labels: Mapped[dict] = mapped_column(
        UniversalJSON, default=dict, comment="标签"
    )


class VllmInstance(IdMixin, InstanceLifecycleMixin, BaseModel):
    """vLLM 实例表。"""

    __tablename__ = f"{db_config.TABLE_NAME_PREFIX}_vllm_instance"
    __table_args__ = (
        Index("idx_vllm_instance_node_id", "node_id"),
        {"comment": "vLLM 实例"},
    )

    model_name: Mapped[str] = mapped_column(
        String(255), comment="模型名称"
    )
    task: Mapped[str] = mapped_column(
        String(32),
        server_default="auto",  # DB 侧默认：旧表 ALTER ADD COLUMN 后存量行自动补 auto
        default=InstanceTaskEnum.AUTO.value,
        comment="vLLM 任务类型（auto/llm/embedding/rerank）",
    )
    model_weight_bytes: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="模型权重大小（Bytes）"
    )
    gpu_memory_utilization: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="显存利用率 GMU（0-1）"
    )
    tensor_parallel_size: Mapped[int] = mapped_column(
        Integer, default=1, comment="张量并行度"
    )
    args: Mapped[list] = mapped_column(
        UniversalJSON, default=list, comment="vLLM 启动附加参数"
    )
    template: Mapped[str | None] = mapped_column(
        UniversalText,
        nullable=True,
        comment="启动模板快照（docker run 模板，create 时固化）",
    )
