"""
Server 模块 Node 服务层契约测试。

使用 sqlite 内存库（conftest session 夹具）验证注册/心跳/状态上报/状态派生逻辑。
顶部 import app.server.node.model 确保 Node 注册进 Base.metadata 被 create_all 建表。
"""

from datetime import datetime, timedelta
from typing import Any

import app.server.node.model  # noqa: F401  (注册 Node 到 Base.metadata)
from app.config import app_config
from app.server.node.enum import NodeStateEnum
from app.server.node.schema import (
    GPUDeviceStatus,
    MemoryInfo,
    NodeRegisterRequest,
    NodeStatus,
    NodeStatusReportRequest,
    SystemReserved,
)
from app.server.node.service import NodeService


def _req(**overrides: Any) -> NodeRegisterRequest:
    base: dict[str, Any] = {
        "machine_id": "m-001",
        "hostname": "gpu-01",
        "ip": "10.0.0.1",
        "advertise_address": "10.0.0.1:8100",
        "worker_port": 8100,
    }
    base.update(overrides)
    return NodeRegisterRequest(**base)


async def test_register_creates_with_token(session):
    svc = NodeService(session)
    node = await svc.register(_req())

    assert node.id
    assert len(node.token) == 64
    assert all(c in "0123456789abcdef" for c in node.token)
    assert node.state == NodeStateEnum.PENDING.value
    assert node.heartbeat_time is None


async def test_register_idempotent_reuse_token_by_machine_id(session):
    svc = NodeService(session)
    first = await svc.register(_req(hostname="gpu-01", ip="10.0.0.1"))
    second = await svc.register(_req(hostname="gpu-01-new", ip="10.0.0.2"))

    assert second.id == first.id
    assert second.token == first.token
    assert second.hostname == "gpu-01-new"
    assert second.ip == "10.0.0.2"


async def test_register_hostname_fallback(session):
    svc = NodeService(session)
    first = await svc.register(_req(machine_id=None, hostname="gpu-fallback"))
    second = await svc.register(_req(machine_id=None, hostname="gpu-fallback"))

    assert second.id == first.id
    assert second.token == first.token


async def test_compute_state_offline_after_grace(session):
    svc = NodeService(session)
    node = await svc.register(_req())
    grace = app_config.NODE_HEARTBEAT_GRACE_PERIOD

    node.heartbeat_time = datetime.now() - timedelta(seconds=grace + 5)
    node.compute_state(grace)

    assert node.state == NodeStateEnum.OFFLINE.value
    assert "心跳超时" in (node.state_message or "")


async def test_update_status_persists_json(session):
    svc = NodeService(session)
    node = await svc.register(_req())

    report = NodeStatusReportRequest(
        system_reserved=SystemReserved(ram=8 * 1024**3, vram=2 * 1024**3),
        status=NodeStatus(
            memory=MemoryInfo(
                total=64 * 1024**3, used=10 * 1024**3, utilization_rate=15.6
            ),
            gpu_devices=[
                GPUDeviceStatus(
                    index=0,
                    name="RTX 4090",
                    vendor="NVIDIA",
                    memory_total=24 * 1024**3,
                    memory_used=1 * 1024**3,
                    utilization_rate=12.5,
                )
            ],
            accelerator="gpu",
        ),
    )
    updated = await svc.update_status(node.id, report)

    assert updated is not None
    assert updated.system_reserved is not None
    assert updated.status is not None
    assert updated.system_reserved["ram"] == 8 * 1024**3
    assert updated.status["gpu_devices"][0]["name"] == "RTX 4090"
    assert updated.status["memory"]["used"] == 10 * 1024**3
    assert updated.status["accelerator"] == "gpu"  # 显式字段经 model_dump 全量落库保留
    assert updated.heartbeat_time is not None
    assert updated.state == NodeStateEnum.READY.value
    # node 级 state_message 由 compute_state 独占（心跳新鲜且非 unreachable → 无 message）
    assert updated.state_message is None


async def test_register_not_set_heartbeat(session):
    svc = NodeService(session)
    node = await svc.register(_req())
    await svc.update_status(node.id, NodeStatusReportRequest())
    assert node.heartbeat_time is not None
    hb_time = node.heartbeat_time

    again = await svc.register(_req(ip="10.0.0.9"))
    assert again.id == node.id
    # liveness 归状态上报端点，注册不刷新存活时间
    assert again.heartbeat_time == hb_time
