"""Server 模块 Instance 请求/响应 Schema。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.server.instance.enum import InstanceStateEnum, InstanceTaskEnum


class VllmInstanceCreateRequest(BaseModel):
    """vLLM 实例创建请求。"""

    model_name: str = Field(min_length=1, description="模型名称")
    task: InstanceTaskEnum = Field(
        default=InstanceTaskEnum.AUTO,
        description="vLLM 任务类型（auto 自动推断，按模型配置映射 --task）",
    )
    vram_claim: int | None = Field(
        default=None, description="显存需求（Bytes），覆盖权重估算值"
    )
    model_weight_bytes: int | None = Field(
        default=None, description="模型权重大小（Bytes），覆盖 worker 广播值"
    )
    gpu_memory_utilization: float | None = Field(
        default=None, description="显存利用率 GMU（0-1），未指定用默认值"
    )
    tensor_parallel_size: int = Field(
        default=1, ge=1, description="张量并行度"
    )
    args: list[str] | None = Field(
        default=None, description="vLLM 启动附加参数"
    )
    node_id: str | None = Field(
        default=None, description="指定节点 ID（须为就绪节点，否则拒绝）"
    )


class VllmInstanceOut(BaseModel):
    """vLLM 实例响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="实例 ID")
    node_id: str = Field(description="所属节点 ID")
    state: str = Field(description="实例状态")
    target_state: str = Field(description="目标状态（none/starting/stopping）")
    state_message: str | None = Field(
        default=None, description="状态说明/失败原因"
    )
    port: int | None = Field(default=None, description="服务端口")
    gpu_indexes: list[int] = Field(
        default_factory=list, description="分配的 GPU 索引列表"
    )
    vram_claim: int | None = Field(
        default=None, description="显存需求（Bytes）"
    )
    allocated_vram: dict[int, int] = Field(
        default_factory=dict, description="per-gpu 记账 {gpu_index: bytes}"
    )
    restart_count: int = Field(description="重启次数")
    labels: dict = Field(default_factory=dict, description="标签")
    model_name: str = Field(description="模型名称")
    task: str = Field(description="vLLM 任务类型（auto/llm/embedding/rerank）")
    model_weight_bytes: int | None = Field(
        default=None, description="模型权重大小（Bytes）"
    )
    gpu_memory_utilization: float | None = Field(
        default=None, description="显存利用率 GMU（0-1）"
    )
    tensor_parallel_size: int = Field(description="张量并行度")
    args: list[str] = Field(
        default_factory=list, description="vLLM 启动附加参数"
    )
    is_active: bool = Field(description="是否启用")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime | None = Field(default=None, description="更新时间")


class InstanceReportItem(BaseModel):
    """实例状态上报条目（worker → server 对账）。"""

    id: str = Field(description="实例 ID")
    state: InstanceStateEnum = Field(description="实例状态（非法值由 FastAPI 直接 422）")
    state_message: str | None = Field(
        default=None, description="状态说明/失败原因"
    )
    port: int | None = Field(default=None, description="服务端口")
    restart_count: int | None = Field(default=None, description="重启次数")


class InstanceReportRequest(BaseModel):
    """实例状态对账请求。"""

    items: list[InstanceReportItem] = Field(description="实例状态条目列表")
