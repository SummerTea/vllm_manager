"""Server 模块 vLLM 实例数据模型。"""

from sqlalchemy import BigInteger, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.base.base_model import BaseModel, IdMixin, UniversalJSON
from app.extensions.database import db_config
from app.server.instance.base import InstanceLifecycleMixin


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
