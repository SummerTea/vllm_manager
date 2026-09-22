"""Manager 模块 Node 请求/响应 Schema。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import app_config


class SystemReserved(BaseModel):
    """系统预留资源。"""

    ram: int | None = Field(default=None, description="预留内存（Bytes）")
    vram: int | None = Field(default=None, description="预留显存（Bytes）")


class GPUDeviceStatus(BaseModel):
    """GPU 设备状态。"""

    index: int | None = Field(default=None, description="GPU 索引")
    name: str = Field(description="GPU 名称")
    vendor: str | None = Field(default=None, description="厂商")
    uuid: str | None = Field(default=None, description="GPU UUID")
    memory_total: int | None = Field(default=None, description="显存总量（Bytes）")
    memory_used: int | None = Field(default=None, description="已用显存（Bytes）")
    utilization_rate: float | None = Field(default=None, description="利用率（0-100）")
    temperature: float | None = Field(default=None, description="温度（摄氏度）")
    power: float | None = Field(default=None, description="功耗（瓦）")


class MemoryInfo(BaseModel):
    """内存状态。"""

    total: int | None = Field(default=None, description="内存总量（Bytes）")
    used: int | None = Field(default=None, description="已用内存（Bytes）")
    utilization_rate: float | None = Field(default=None, description="利用率（0-100）")


class CPUInfo(BaseModel):
    """CPU 状态。"""

    total: int | None = Field(default=None, description="CPU 核数")
    utilization_rate: float | None = Field(default=None, description="利用率（0-100）")


class SwapInfo(BaseModel):
    """交换分区状态。"""

    total: int | None = Field(default=None, description="交换分区总量（Bytes）")
    used: int | None = Field(default=None, description="已用交换分区（Bytes）")


class NodeStatus(BaseModel):
    """节点状态载荷（心跳上报）。"""

    model_config = ConfigDict(extra="allow")

    cpu: CPUInfo | None = Field(default=None, description="CPU 状态")
    memory: MemoryInfo | None = Field(default=None, description="内存状态")
    swap: SwapInfo | None = Field(default=None, description="交换分区状态")
    gpu_devices: list[GPUDeviceStatus] | None = Field(default=None, description="GPU 设备列表")
    filesystem: list[dict[str, Any]] | None = Field(default=None, description="文件系统状态")
    os: dict | None = Field(default=None, description="操作系统信息")
    kernel: dict | None = Field(default=None, description="内核信息")
    uptime: dict | None = Field(default=None, description="运行时长信息")


class NodeRegisterRequest(BaseModel):
    """节点注册请求。"""

    machine_id: str | None = Field(
        default=None, max_length=64, description="机器唯一标识（幂等注册主键）"
    )
    hostname: str = Field(
        min_length=1, max_length=255, description="主机名（幂等注册回退键）"
    )
    ip: str = Field(max_length=64, description="IP 地址")
    advertise_address: str | None = Field(
        default=None,
        description="对外可达地址（host:port 或纯 host；含端口时端口优先，纯 host 自动补 agent_port）",
    )
    agent_port: int = Field(
        default=app_config.AGENT_DEFAULT_PORT,
        ge=1,
        le=65535,
        description="agent 端口",
    )


class NodeRegisterResponse(BaseModel):
    """节点注册响应。"""

    node_id: str = Field(description="节点 ID")
    token: str = Field(description="Bearer 鉴权令牌")
    agent_port: int = Field(description="agent 端口")
    heartbeat_interval: int = Field(description="建议心跳间隔（秒）")


class NodeStatusReportRequest(BaseModel):
    """节点状态上报请求。"""

    system_reserved: SystemReserved | None = Field(default=None, description="系统预留 {ram, vram}")
    status: NodeStatus | None = Field(default=None, description="节点状态载荷（心跳上报）")


class NodeUpdateRequest(BaseModel):
    """节点更新请求。"""

    labels: dict | None = Field(default=None, description="标签")
    is_active: bool | None = Field(default=None, description="是否启用")


class NodeOut(BaseModel):
    """节点响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="节点 ID")
    machine_id: str | None = Field(default=None, description="机器唯一标识（幂等注册主键）")
    hostname: str = Field(description="主机名（幂等注册回退键）")
    ip: str = Field(description="IP 地址")
    advertise_address: str | None = Field(
        default=None,
        description="对外可达地址（host:port 或纯 host；含端口时端口优先，纯 host 自动补 agent_port）",
    )
    agent_port: int = Field(description="agent 端口")
    state: str = Field(description="节点状态")
    state_message: str | None = Field(default=None, description="状态说明/失败原因")
    unreachable: bool = Field(description="主动探测不可达标志")
    heartbeat_time: datetime | None = Field(default=None, description="最后心跳时间")
    labels: dict = Field(default_factory=dict, description="标签")
    system_reserved: dict | None = Field(default=None, description="系统预留 {ram, vram}")
    status: dict | None = Field(default=None, description="节点状态载荷（心跳上报）")
    is_active: bool = Field(description="是否启用")
    created_at: datetime = Field(description="创建时间")
    updated_at: datetime | None = Field(default=None, description="更新时间")
