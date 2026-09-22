"""
Server 模块 Instance 服务层契约测试。

模式：sqlite 内存库 session 夹具 + monkeypatch 替换 request_to_worker（创建/启停
均不真发网络）。顶部 import node/instance model 确保表注册进 Base.metadata 被
create_all 建表。
"""

from typing import Any

import pytest

import app.server.instance.model  # noqa: F401  (注册 VllmInstance 到 Base.metadata)
import app.server.node.model  # noqa: F401  (注册 Node 到 Base.metadata)
from app.exception import (
    ConflictException,
    ExternalServiceException,
    InvalidStateException,
    OperationNotAllowedException,
)
from app.server.instance.base import (
    apply_report,
    estimate_vram_claim,
    is_target_achieved,
    reconcile_unreachable,
)
from app.server.instance.enum import InstanceStateEnum, InstanceTargetStateEnum
from app.server.instance.model import VllmInstance
from app.server.instance.schema import (
    InstanceReportItem,
    InstanceReportRequest,
    VllmInstanceCreateRequest,
)
from app.server.instance.service import VllmInstanceService
from app.server.node.model import Node
from app.server.node.schema import (
    GPUDeviceStatus,
    MemoryInfo,
    NodeRegisterRequest,
    NodeStatus,
    NodeStatusReportRequest,
    SystemReserved,
)
from app.server.node.service import NodeService

_GIB = 1024**3


class FakeResponse:
    """鸭子类型响应（request_to_worker 被 patch 后返回对象需含 .json()）。"""

    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def json(self) -> dict:
        return self._data


async def _ready_node(session, *, machine_id: str = "m-001") -> Node:
    """注册并造好 GPU 数据（1 卡 24GiB、system_reserved vram=2GiB）的就绪节点。"""
    svc = NodeService(session)
    node = await svc.register(
        NodeRegisterRequest(
            machine_id=machine_id,
            hostname=f"gpu-{machine_id}",
            ip="10.0.0.1",
            advertise_address="10.0.0.1:8100",
            worker_port=8100,
        )
    )
    await svc.heartbeat(node.id)
    await svc.update_status(
        node.id,
        NodeStatusReportRequest(
            system_reserved=SystemReserved(ram=8 * _GIB, vram=2 * _GIB),
            status=NodeStatus(
                memory=MemoryInfo(
                    total=64 * _GIB, used=10 * _GIB, utilization_rate=15.6
                ),
                gpu_devices=[
                    GPUDeviceStatus(
                        index=0,
                        name="RTX 4090",
                        vendor="NVIDIA",
                        memory_total=24 * _GIB,
                        memory_used=1 * _GIB,
                        utilization_rate=12.5,
                    )
                ],
            ),
        ),
    )
    return node


async def _create_ready_instance(session, monkeypatch, **overrides) -> VllmInstance:
    """创建实例（patch creation.request_to_worker：权重广播返回 2GiB，启动转发成功）。"""
    async def fake_request_to_worker(node, method, path, **kwargs):
        return FakeResponse({"weight_bytes": 2 * _GIB})

    monkeypatch.setattr(
        "app.server.instance.service.creation.request_to_worker",
        fake_request_to_worker,
    )
    data: dict[str, Any] = {"model_name": "qwen2.5"}
    data.update(overrides)
    return await VllmInstanceService(session).create(VllmInstanceCreateRequest(**data))


# ---------- base 纯函数 ----------


def test_estimate_vram_claim():
    assert estimate_vram_claim(2 * _GIB) == int(2 * _GIB * 1.2) + 2 * _GIB


def test_apply_report_pure_clears_target_on_achieved():
    # target=stopping + 上报 stopped → 达成清 target
    result = apply_report(
        "stopping", "old", None, 0, "stopped", "ok", 8001, 1
    )
    assert result == ("none", "ok", 8001, 1)


def test_apply_report_keeps_port_and_restart_when_not_reported():
    result = apply_report(
        "none", "ok", 8001, 1, "running", "ok2", None, None
    )
    assert result == ("none", "ok2", 8001, 1)


def test_apply_report_stopping_target_kept_until_stopped():
    """B1 竞态回归：stop 在途，上报 running/starting/error 均保留 target=stopping。"""
    for reported in ("running", "starting", "error"):
        new_target, _, _, _ = apply_report(
            "stopping", None, None, 0, reported, None, None, None
        )
        assert new_target == "stopping", f"reported={reported}"
    # 上报 stopped → 达成清 target
    new_target, _, _, _ = apply_report(
        "stopping", None, None, 0, "stopped", None, None, None
    )
    assert new_target == "none"


def test_apply_report_starting_target_kept_until_running_or_error():
    """B1 竞态回归：start 在途，上报 starting/pending 保留 target=starting。"""
    for reported in ("starting", "pending"):
        new_target, _, _, _ = apply_report(
            "starting", None, None, 0, reported, None, None, None
        )
        assert new_target == "starting", f"reported={reported}"
    for reported in ("running", "error"):
        new_target, _, _, _ = apply_report(
            "starting", None, None, 0, reported, None, None, None
        )
        assert new_target == "none", f"reported={reported}"


def test_apply_report_target_none_stays_none():
    new_target, _, _, _ = apply_report(
        "none", None, None, 0, "running", None, None, None
    )
    assert new_target == "none"


def test_apply_report_keeps_state_message_when_not_reported():
    """上报 state_message=None 不清掉已存错误信息（非 None 保护）。"""
    _, new_message, _, _ = apply_report(
        "stopping", "已有错误信息", None, 0, "running", None, None, None
    )
    assert new_message == "已有错误信息"
    # 上报非 None 时正常更新
    _, new_message, _, _ = apply_report(
        "none", "已有信息", None, 0, "running", "新信息", None, None
    )
    assert new_message == "新信息"


def test_reconcile_unreachable():
    assert reconcile_unreachable("running") == "unreachable"
    assert reconcile_unreachable("error") == "error"
    assert reconcile_unreachable("stopped") == "stopped"


def test_is_target_achieved():
    assert is_target_achieved("running", "starting") is True
    assert is_target_achieved("error", "starting") is True
    assert is_target_achieved("starting", "starting") is False
    assert is_target_achieved("stopped", "stopping") is True
    assert is_target_achieved("running", "stopping") is False
    assert is_target_achieved("anything", "none") is True


# ---------- create 编排 ----------


async def test_create_success(session, monkeypatch):
    node = await _ready_node(session)
    calls = []

    async def fake_request_to_worker(node, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return FakeResponse({"weight_bytes": 2 * _GIB})

    monkeypatch.setattr(
        "app.server.instance.service.creation.request_to_worker",
        fake_request_to_worker,
    )

    inst = await VllmInstanceService(session).create(
        VllmInstanceCreateRequest(model_name="qwen2.5")
    )

    assert inst.id
    assert inst.node_id == node.id
    assert inst.state == InstanceStateEnum.PENDING.value
    assert inst.target_state == InstanceTargetStateEnum.STARTING.value
    assert inst.gpu_indexes == [0]
    assert inst.vram_claim == estimate_vram_claim(2 * _GIB)
    assert inst.allocated_vram == {0: int(24 * _GIB * 0.9)}
    assert inst.gpu_memory_utilization == 0.9
    assert inst.model_weight_bytes == 2 * _GIB
    assert inst.tensor_parallel_size == 1

    # 权重广播 + 启动转发两次调用，校验启动转发 payload
    assert len(calls) == 2
    method, path, kwargs = calls[-1]
    assert method == "post"
    assert path == f"instances/{inst.id}/start"
    payload = kwargs["json"]
    assert payload["instance_type"] == "vllm"
    assert payload["gpu_indexes"] == [0]
    assert payload["vram_claim"] == inst.vram_claim
    assert payload["spec"] == {
        "model_name": "qwen2.5",
        "gpu_memory_utilization": 0.9,
        "tensor_parallel_size": 1,
        "args": [],
    }


async def test_create_vram_claim_override_skips_weight_broadcast(session, monkeypatch):
    await _ready_node(session)
    called = []

    async def fake_request_to_worker(node, method, path, **kwargs):
        called.append(path)
        return FakeResponse()

    monkeypatch.setattr(
        "app.server.instance.service.creation.request_to_worker",
        fake_request_to_worker,
    )

    inst = await VllmInstanceService(session).create(
        VllmInstanceCreateRequest(model_name="qwen2.5", vram_claim=10 * _GIB)
    )

    assert inst.vram_claim == 10 * _GIB
    assert inst.model_weight_bytes is None
    # 仅启动转发一次（权重广播被跳过）
    assert called == [f"instances/{inst.id}/start"]


async def test_create_alloc_rejected(session, monkeypatch):
    await _ready_node(session)
    with pytest.raises(OperationNotAllowedException) as excinfo:
        await VllmInstanceService(session).create(
            VllmInstanceCreateRequest(model_name="qwen2.5", vram_claim=22 * _GIB)
        )
    assert excinfo.value.details["operation"] == "create"
    assert "显存分配失败" in excinfo.value.message


async def test_create_forward_failure_rolls_back(session, monkeypatch):
    await _ready_node(session)
    await session.commit()  # 固化节点前置（模拟请求前已提交状态）

    async def fake_request_to_worker(node, method, path, **kwargs):
        if path == "models/weight":
            return FakeResponse({"weight_bytes": 2 * _GIB})
        raise ExternalServiceException("worker 拒绝启动", service_name="worker")

    monkeypatch.setattr(
        "app.server.instance.service.creation.request_to_worker",
        fake_request_to_worker,
    )

    with pytest.raises(ExternalServiceException) as excinfo:
        await VllmInstanceService(session).create(
            VllmInstanceCreateRequest(model_name="qwen2.5")
        )
    assert excinfo.value.details["instance_id"]

    # 路由层对 VllmManagerException 统一 rollback：实例记录不落库（I1 选项 a）
    await session.rollback()
    found = await VllmInstanceService(session).get_by_field("model_name", "qwen2.5")
    assert found is None


async def test_create_specified_node_unavailable(session, monkeypatch):
    await _ready_node(session)
    with pytest.raises(OperationNotAllowedException) as excinfo:
        await VllmInstanceService(session).create(
            VllmInstanceCreateRequest(model_name="qwen2.5", node_id="no-such-node")
        )
    assert "指定节点不可用" in excinfo.value.message


async def test_start_forward_failure_keeps_state(session, monkeypatch):
    """I1 选项 a：start 转发失败时 target 写入随事务回滚，实例保持 stopped 且 target 未落库。"""
    await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.STOPPED.value
    inst.target_state = InstanceTargetStateEnum.NONE.value
    await session.commit()  # 固化前置（模拟请求前已提交状态）

    async def fake_request_to_worker(node, method, path, **kwargs):
        raise ExternalServiceException("worker 拒绝启动", service_name="worker")

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    with pytest.raises(ExternalServiceException):
        await VllmInstanceService(session).start(inst.id)

    # 路由层 rollback：target=starting 写入回滚，实例仍 stopped、target=none
    # （rollback 使 persistent 属性过期，先缓存 id 再查询）
    instance_id = inst.id
    await session.rollback()
    reloaded = await VllmInstanceService(session).get_by_id(instance_id)
    assert reloaded is not None
    assert reloaded.state == InstanceStateEnum.STOPPED.value
    assert reloaded.target_state == InstanceTargetStateEnum.NONE.value


# ---------- start / stop ----------


async def test_start_stopped_instance(session, monkeypatch):
    await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.STOPPED.value
    inst.target_state = InstanceTargetStateEnum.NONE.value
    await session.flush()

    calls = []

    async def fake_request_to_worker(node, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return FakeResponse()

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    out = await VllmInstanceService(session).start(inst.id)

    assert out.target_state == InstanceTargetStateEnum.STARTING.value
    method, path, kwargs = calls[-1]
    assert method == "post"
    assert path == f"instances/{inst.id}/start"
    assert kwargs["json"]["spec"]["model_name"] == "qwen2.5"


async def test_start_running_instance_rejected(session, monkeypatch):
    await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.RUNNING.value
    await session.flush()

    with pytest.raises(InvalidStateException):
        await VllmInstanceService(session).start(inst.id)


async def test_stop_running_instance(session, monkeypatch):
    await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.RUNNING.value
    inst.target_state = InstanceTargetStateEnum.NONE.value
    await session.flush()

    calls = []

    async def fake_request_to_worker(node, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return FakeResponse()

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    out = await VllmInstanceService(session).stop(inst.id)

    assert out.target_state == InstanceTargetStateEnum.STOPPING.value
    method, path, kwargs = calls[-1]
    assert method == "post"
    assert path == f"instances/{inst.id}/stop"
    assert kwargs["json"] == {"instance_type": "vllm"}


async def test_stop_stopped_instance_rejected(session, monkeypatch):
    await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.STOPPED.value
    await session.flush()

    with pytest.raises(InvalidStateException):
        await VllmInstanceService(session).stop(inst.id)


# ---------- delete ----------


async def test_delete_active_instance_stops_then_deletes(session, monkeypatch):
    await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.RUNNING.value
    await session.flush()

    calls = []

    async def fake_request_to_worker(node, method, path, **kwargs):
        calls.append(path)
        return FakeResponse()

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    await VllmInstanceService(session).delete(inst.id)

    assert calls == [f"instances/{inst.id}/stop"]
    got = await VllmInstanceService(session).get_by_id(inst.id)
    assert got is None


# ---------- apply_report 对账 ----------


async def test_apply_report_updates_and_clears_target(session, monkeypatch):
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.STARTING.value
    inst.target_state = InstanceTargetStateEnum.STARTING.value
    await session.flush()

    report = InstanceReportRequest(
        items=[
            InstanceReportItem(
                id=inst.id,
                state=InstanceStateEnum.RUNNING,
                state_message="ok",
                port=8001,
                restart_count=1,
            )
        ]
    )
    count = await VllmInstanceService(session).apply_report(node.id, report)

    assert count == 1
    assert inst.state == "running"
    assert inst.target_state == InstanceTargetStateEnum.NONE.value
    assert inst.state_message == "ok"
    assert inst.port == 8001
    assert inst.restart_count == 1


async def test_apply_report_keeps_stopping_target_on_running_report(session, monkeypatch):
    """B1 竞态回归（service 层核心）：stop 在途期间在途上报仍带 running，
    本次对账必须保留 target=stopping，否则下轮上报消失时实例永卡 running 占账。"""
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.RUNNING.value
    inst.target_state = InstanceTargetStateEnum.STOPPING.value
    await session.flush()

    report = InstanceReportRequest(
        items=[
            InstanceReportItem(id=inst.id, state=InstanceStateEnum.RUNNING)
        ]
    )
    count = await VllmInstanceService(session).apply_report(node.id, report)

    assert count == 1
    assert inst.state == InstanceStateEnum.RUNNING.value
    assert inst.target_state == InstanceTargetStateEnum.STOPPING.value  # 保留，不释放占账
    # 模拟下轮上报消失 → 命中 target=stopping 收敛为 stopped
    await VllmInstanceService(session).apply_report(
        node.id, InstanceReportRequest(items=[])
    )
    assert inst.state == InstanceStateEnum.STOPPED.value


async def test_apply_report_missing_with_target_stopping(session, monkeypatch):
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.target_state = InstanceTargetStateEnum.STOPPING.value
    await session.flush()

    count = await VllmInstanceService(session).apply_report(
        node.id, InstanceReportRequest(items=[])
    )

    assert count == 0
    assert inst.state == InstanceStateEnum.STOPPED.value
    assert inst.target_state == InstanceTargetStateEnum.NONE.value


async def test_apply_report_missing_without_stopping_target(session, monkeypatch):
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.STARTING.value
    inst.target_state = InstanceTargetStateEnum.STARTING.value
    await session.flush()

    count = await VllmInstanceService(session).apply_report(
        node.id, InstanceReportRequest(items=[])
    )

    assert count == 0
    assert inst.state == InstanceStateEnum.STARTING.value
    assert inst.target_state == InstanceTargetStateEnum.STARTING.value


async def test_apply_report_ignores_foreign_instance_id(session, monkeypatch):
    node1 = await _ready_node(session, machine_id="m-001")
    inst = await _create_ready_instance(session, monkeypatch)
    node2 = await _ready_node(session, machine_id="m-002")

    report = InstanceReportRequest(
        items=[
            InstanceReportItem(
                id=inst.id, state=InstanceStateEnum.RUNNING, state_message="x"
            )
        ]
    )
    count = await VllmInstanceService(session).apply_report(node2.id, report)

    assert count == 0
    assert inst.state == InstanceStateEnum.PENDING.value
    assert inst.node_id == node1.id


# ---------- 失联联动 / 删除守卫 ----------


async def test_reconcile_node_loss(session, monkeypatch):
    node = await _ready_node(session)
    running = await _create_ready_instance(session, monkeypatch)
    running.state = InstanceStateEnum.RUNNING.value
    # 单卡已被 running 实例占满，第二个实例直接内存构造（不经 allocator）
    error = VllmInstance(
        node_id=node.id,
        state=InstanceStateEnum.ERROR.value,
        target_state="none",
        model_name="qwen2.5-7b",
        gpu_indexes=[0],
        args=[],
        labels={},
        allocated_vram={},
        restart_count=0,
    )
    session.add(error)
    await session.flush()

    count = await VllmInstanceService(session).reconcile_node_loss(node)

    assert count == 1
    assert running.state == InstanceStateEnum.UNREACHABLE.value
    assert running.state_message == "节点失联"
    assert error.state == InstanceStateEnum.ERROR.value


async def test_assert_node_deletable(session, monkeypatch):
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.RUNNING.value
    await session.flush()

    with pytest.raises(ConflictException):
        await VllmInstanceService(session).assert_node_deletable(node.id)

    inst.state = InstanceStateEnum.STOPPED.value
    await session.flush()
    await VllmInstanceService(session).assert_node_deletable(node.id)  # 不抛
