"""Worker 域请求/响应 Schema（Phase 2 端点请求体，先建骨架供 api 使用）。

字段与 docs/worker-contract.md §2 的 start/stop/weight 契约对齐：
- start 载荷结构 = server creation.py:_build_start_payload 的 spec 部分
- worker 自身端点响应不要求 BaseResponse 包装（Phase 2 再定）
"""

from pydantic import BaseModel, Field


class VllmSpec(BaseModel):
    """vLLM 实例规格（start 指令 spec 字段）。"""

    model_name: str = Field(description="模型名称/路径")
    task: str = Field(default="auto", description="产品层任务分类（auto/llm/embedding/rerank）")
    gpu_memory_utilization: float | None = Field(
        default=None, description="显存利用率 GMU（0-1），None 时 worker 用默认兜底"
    )
    tensor_parallel_size: int = Field(
        default=1, ge=1, description="张量并行度（≥1）"
    )
    args: list[str] | None = Field(
        default=None, description="vLLM 启动附加参数（用户参数优先）"
    )


class StartRequest(BaseModel):
    """启动实例指令（POST /instances/{instance_id}/start）。"""

    instance_type: str = Field(description="实例类型（固定 vllm）")
    spec: VllmSpec = Field(description="实例规格")
    gpu_indexes: list[int] = Field(description="分配的 GPU 索引列表")
    vram_claim: int = Field(description="显存需求（Bytes）")


class StopRequest(BaseModel):
    """停止实例指令（POST /instances/{instance_id}/stop）。"""

    instance_type: str = Field(default="vllm", description="实例类型（固定 vllm）")


class WeightRequest(BaseModel):
    """权重查询指令（POST /models/weight）。"""

    model_name: str = Field(description="模型名称")


class WeightResponse(BaseModel):
    """权重查询响应。"""

    weight_bytes: int = Field(description="模型权重大小（Bytes）")
