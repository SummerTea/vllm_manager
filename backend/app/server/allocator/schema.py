"""Server 模块 Allocator 输入/输出契约 Schema。"""

from pydantic import BaseModel, Field


class GPUResource(BaseModel):
    """GPU 资源描述（分配决策输入）。"""

    index: int = Field(description="GPU 索引")
    memory_total: int = Field(description="显存总量（Bytes）")
    gpu_type: str | None = Field(default=None, description="型号判据（映射 node.status.gpu_devices[].name）")


class WorkerResource(BaseModel):
    """worker 节点资源快照（分配决策输入，由调用方聚合）。"""

    node_id: str = Field(description="节点 ID")
    accelerator: str | None = Field(
        default=None,
        description="加速器类型（worker 显式声明 gpu|cpu）；仅 cpu 走 CPU 分配，None/未知按 GPU fail-closed",
    )
    gpu_devices: list[GPUResource] = Field(description="GPU 设备列表")
    system_reserved_vram: int = Field(description="系统预留显存（Bytes，整机级，每卡都扣）")
    allocated_vram: dict[int, int] = Field(
        description="已分配显存 {gpu_index: bytes}（调用方聚合）"
    )


class AllocationResult(BaseModel):
    """分配命中结果。"""

    node_id: str = Field(description="节点 ID")
    gpu_indexes: list[int] = Field(description="分配的 GPU 索引列表")
    gpu_memory_utilization: float = Field(description="显存利用率 GMU（0-1）")
    vram_claim: int = Field(description="显存需求（Bytes）")
    allocated_vram: dict[int, int] = Field(
        description="per-gpu 记账 {gpu_index: int(total×GMU)}"
    )


class AllocationRejected(BaseModel):
    """分配拒绝结果。"""

    reason: str = Field(description="拒绝原因（首个失败描述）")
