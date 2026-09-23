"""
Server 模块 Instance 服务层契约测试。

模式：sqlite 内存库 session 夹具 + monkeypatch 替换 request_to_worker（创建/启停
均不真发网络）。顶部 import node/instance model 确保表注册进 Base.metadata 被
create_all 建表。
"""

from typing import Any

import asyncio
import logging

import pytest

import app.server.instance.model  # noqa: F401  (注册 VllmInstance 到 Base.metadata)
import app.server.node.model  # noqa: F401  (注册 Node 到 Base.metadata)
from app.exception import (
    ConflictException,
    ExternalServiceException,
    HttpClientException,
    InvalidStateException,
    OperationNotAllowedException,
)
from app.server.instance.base import (
    apply_report,
    estimate_vram_claim,
    is_target_achieved,
    reconcile_unreachable,
)
from app.server.instance.enum import (
    InstanceStateEnum,
    InstanceTargetStateEnum,
    InstanceTaskEnum,
)
from app.server.instance.model import VllmInstance
from app.server.instance.schema import (
    InstanceReportItem,
    InstanceReportRequest,
    VllmInstanceCreateRequest,
)
from app.server.instance.service import VllmInstanceService
from app.server.instance.service.creation import DEFAULT_VLLM_RUN_TEMPLATE
from app.server.instance.service.instance import (
    _MAX_STOP_REDISPATCH,
    _pending_redispatch_tasks,
)
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
    # update_status 刷新存活时间（heartbeat_time）并派生状态，承担节点存活语义
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


async def _ready_cpu_node(
    session, *, machine_id: str = "m-cpu", accelerator: str | None = "cpu"
) -> Node:
    """注册并造好无 GPU 数据（gpu_devices=[]）的就绪 CPU-only 节点
    （accelerator 默认显式声明 cpu，可传 None 模拟 worker 未上报）。"""
    svc = NodeService(session)
    node = await svc.register(
        NodeRegisterRequest(
            machine_id=machine_id,
            hostname=f"cpu-{machine_id}",
            ip="10.0.0.2",
            advertise_address="10.0.0.2:8100",
            worker_port=8100,
        )
    )
    # update_status 刷新存活时间（heartbeat_time）并派生状态，承担节点存活语义
    await svc.update_status(
        node.id,
        NodeStatusReportRequest(
            system_reserved=SystemReserved(ram=8 * _GIB, vram=0),
            status=NodeStatus(
                memory=MemoryInfo(
                    total=64 * _GIB, used=10 * _GIB, utilization_rate=15.6
                ),
                gpu_devices=[],
                accelerator=accelerator,
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


async def _drain_redispatch_tasks() -> None:
    """等待模块级 fire-and-forget stop 重下发任务全部完成（done 回调自动清空集合）。

    用于对「实际转发被调用」做同步断言：apply_report 只记账并派发后台任务，转发
    在端点返回后才执行，测试需把控制权交还事件循环直至任务完成。
    """
    while _pending_redispatch_tasks:
        await asyncio.gather(*list(_pending_redispatch_tasks))


# ---------- base 纯函数 ----------


def test_estimate_vram_claim():
    # LLM 口径：weight×1.2 + 2GiB（缺省 task 向后兼容）
    assert estimate_vram_claim(2 * _GIB) == int(2 * _GIB * 1.2) + 2 * _GIB
    assert estimate_vram_claim(2 * _GIB, "auto") == int(2 * _GIB * 1.2) + 2 * _GIB
    assert estimate_vram_claim(2 * _GIB, "llm") == int(2 * _GIB * 1.2) + 2 * _GIB


def test_estimate_vram_claim_embedding_rerank_small_footprint():
    """embedding/rerank 走 512MiB 口径（非 LLM 小模型开销小）。"""
    expected = int(2 * _GIB * 1.2) + 512 * 1024 * 1024
    assert estimate_vram_claim(2 * _GIB, "embedding") == expected
    assert estimate_vram_claim(2 * _GIB, "rerank") == expected
    assert estimate_vram_claim(
        2 * _GIB, InstanceTaskEnum.EMBEDDING.value
    ) == expected


def test_apply_report_pure_clears_target_on_achieved():
    # target=stopping + 上报 stopped → 达成清 target
    result = apply_report(
        "running", "stopping", "old", None, 0, "stopped", "ok", 8001, 1
    )
    assert result == ("stopped", "none", "ok", 8001, 1)


def test_apply_report_keeps_port_and_restart_when_not_reported():
    result = apply_report(
        "running", "none", "ok", 8001, 1, "running", "ok2", None, None
    )
    assert result == ("running", "none", "ok2", 8001, 1)


def test_apply_report_stopping_target_kept_until_stopped():
    """B1 竞态回归：stop 在途，上报 running/starting/error 均保留 target=stopping。"""
    for reported in ("running", "starting", "error"):
        _, new_target, _, _, _ = apply_report(
            "running", "stopping", None, None, 0, reported, None, None, None
        )
        assert new_target == "stopping", f"reported={reported}"
    # 上报 stopped → 达成清 target
    _, new_target, _, _, _ = apply_report(
        "running", "stopping", None, None, 0, "stopped", None, None, None
    )
    assert new_target == "none"


def test_apply_report_starting_target_kept_until_running_or_error():
    """B1 竞态回归：start 在途，上报 starting/pending 保留 target=starting。"""
    for reported in ("starting", "pending"):
        _, new_target, _, _, _ = apply_report(
            "stopped", "starting", None, None, 0, reported, None, None, None
        )
        assert new_target == "starting", f"reported={reported}"
    for reported in ("running", "error"):
        _, new_target, _, _, _ = apply_report(
            "stopped", "starting", None, None, 0, reported, None, None, None
        )
        assert new_target == "none", f"reported={reported}"


def test_apply_report_target_none_stays_none():
    _, new_target, _, _, _ = apply_report(
        "running", "none", None, None, 0, "running", None, None, None
    )
    assert new_target == "none"


def test_apply_report_keeps_state_message_when_not_reported():
    """上报 state_message=None 不清掉已存错误信息（非 None 保护）。"""
    _, _, new_message, _, _ = apply_report(
        "running", "stopping", "已有错误信息", None, 0, "running", None, None, None
    )
    assert new_message == "已有错误信息"
    # 上报非 None 时正常更新
    _, _, new_message, _, _ = apply_report(
        "running", "none", "已有信息", None, 0, "running", "新信息", None, None
    )
    assert new_message == "新信息"


# ---------- M2 stopped 防复活守卫 ----------


def test_apply_report_stopped_guard_blocks_stale_running():
    """M2 核心：stopped 终态 + target none + 在途旧 running 上报晚到 → 整体丢弃不复活。"""
    result = apply_report(
        "stopped", "none", "手动停止", 8001, 2, "running", "ok", 9001, 3
    )
    assert result == ("stopped", "none", "手动停止", 8001, 2)


def test_apply_report_stopped_guard_blocks_stale_error():
    result = apply_report(
        "stopped", "none", "手动停止", 8001, 2, "error", "启动失败", None, None
    )
    assert result == ("stopped", "none", "手动停止", 8001, 2)


def test_apply_report_stopped_accepts_reported_stopped():
    """stopped 终态下上报 stopped 属正常（幂等保持）。"""
    result = apply_report(
        "stopped", "none", None, 8001, 2, "stopped", "ok", 9001, 3
    )
    assert result == ("stopped", "none", "ok", 9001, 3)


def test_apply_report_stopped_allowed_when_target_starting():
    """start 意图期间不拦截：stopped + target=starting + 上报 running → 推进并清 target。"""
    result = apply_report(
        "stopped", "starting", None, None, 0, "running", "ok", 8001, 1
    )
    assert result == ("running", "none", "ok", 8001, 1)


def test_apply_report_unreachable_recovers_to_running():
    """恢复路径：unreachable 实例上报 running → 推进 running（非 stopped 不受守卫限制）。"""
    result = apply_report(
        "unreachable", "none", "节点失联", None, 0, "running", "ok", 8001, 1
    )
    assert result == ("running", "none", "ok", 8001, 1)


def test_apply_report_error_recovers_to_running():
    """恢复路径：error 实例上报 running → 推进 running（worker 重启恢复）。"""
    result = apply_report(
        "error", "none", "启动失败", None, 0, "running", "ok", None, None
    )
    assert result == ("running", "none", "ok", None, 0)


def test_apply_report_stopped_allowed_when_target_stopping():
    """B1 优先于 M2：current stopped + target stopping + 上报 running → 放行推进，
    但 target 保留 stopping（守卫只在 target=none 触发，stop 意图收敛不被打断）。"""
    result = apply_report(
        "stopped", "stopping", "停止中", None, 0, "running", "ok", 8001, 1
    )
    assert result == ("running", "stopping", "ok", 8001, 1)


def test_apply_report_stopped_blocks_reported_unreachable():
    """M2 拦截面覆盖 unreachable：stopped 终态 + 上报 unreachable → 丢弃不复活。"""
    result = apply_report(
        "stopped", "none", "手动停止", 8001, 2, "unreachable", "节点失联", 9001, 3
    )
    assert result == ("stopped", "none", "手动停止", 8001, 2)


def test_apply_report_error_accepts_reported_stopped():
    """error 当前态上报 stopped → 放行（占账释放路径；防未来守卫条件扩写
    误伤「非 stopped 当前态上报 stopped」）。"""
    result = apply_report(
        "error", "none", "启动失败", None, 0, "stopped", "已停止", 8001, 1
    )
    assert result == ("stopped", "none", "已停止", 8001, 1)


def test_apply_report_stopped_keeps_starting_target_on_reported_stopped():
    """B1 对称场景：start 在途（target starting）、worker 未起、上报仍 stopped
    → target 保留 starting（未达成不清，否则 start 意图丢失）。"""
    result = apply_report(
        "stopped", "starting", None, None, 0, "stopped", "ok", 8001, 1
    )
    assert result == ("stopped", "starting", "ok", 8001, 1)


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
    assert inst.task == InstanceTaskEnum.AUTO.value  # 缺省 task 落 auto

    # 权重广播 + 启动转发两次调用，校验启动转发 payload
    assert len(calls) == 2
    method, path, kwargs = calls[-1]
    assert method == "post"
    assert path == f"instances/{inst.id}/start"
    payload = kwargs["json"]
    assert payload["instance_type"] == "vllm"
    assert payload["gpu_indexes"] == [0]
    assert payload["vram_claim"] == inst.vram_claim
    assert payload["template"] == inst.template
    assert payload["spec"] == {
        "model_name": "qwen2.5",
        "task": "auto",
        "gpu_memory_utilization": 0.9,
        "tensor_parallel_size": 1,
        "args": [],
    }


async def test_create_cpu_node(session, monkeypatch):
    """CPU-only 节点（gpu_devices=[]）→ create 成功：allocator CPU 分支命中，
    记录与启动 payload 的 gpu_indexes==[]、allocated_vram=={}。"""
    node = await _ready_cpu_node(session)
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
    assert inst.gpu_indexes == []
    assert inst.allocated_vram == {}
    assert inst.vram_claim == estimate_vram_claim(2 * _GIB)
    assert inst.gpu_memory_utilization == 0.9

    # 权重广播 + 启动转发两次调用，校验启动转发 payload
    assert len(calls) == 2
    method, path, kwargs = calls[-1]
    assert method == "post"
    assert path == f"instances/{inst.id}/start"
    payload = kwargs["json"]
    assert payload["instance_type"] == "vllm"
    assert payload["gpu_indexes"] == []
    assert payload["vram_claim"] == inst.vram_claim
    assert payload["template"] == inst.template
    assert payload["spec"]["model_name"] == "qwen2.5"


async def test_create_empty_gpu_without_accelerator_rejected(session, monkeypatch):
    """fail-closed 回归：gpu_devices=[] 且未声明 accelerator（None）→ create 分配拒绝，
    不按空 GPU 列表推断为 CPU 节点（GPU 节点采集降级会误上报空列表）。"""
    await _ready_cpu_node(session, machine_id="m-cpu-und", accelerator=None)

    async def fake_request_to_worker(node, method, path, **kwargs):
        return FakeResponse({"weight_bytes": 2 * _GIB})

    monkeypatch.setattr(
        "app.server.instance.service.creation.request_to_worker",
        fake_request_to_worker,
    )

    with pytest.raises(OperationNotAllowedException) as excinfo:
        await VllmInstanceService(session).create(
            VllmInstanceCreateRequest(model_name="qwen2.5")
        )
    assert excinfo.value.details["operation"] == "create"
    assert "显存分配失败" in excinfo.value.message


async def test_create_snapshots_default_template(session, monkeypatch):
    """create 快照 server 内置 DEFAULT_VLLM_RUN_TEMPLATE 常量（无模板表/template_key）。"""
    await _ready_node(session)

    inst = await _create_ready_instance(session, monkeypatch)

    assert inst.template is not None
    assert inst.template == DEFAULT_VLLM_RUN_TEMPLATE


def test_default_template_matches_worker():
    """server 内置 DEFAULT_VLLM_RUN_TEMPLATE 与 worker 侧逐字符一致（防漂移回归锁定）。

    跨域 import 是**有意的**：漂移检测需要双方实值比对；测试级引用不破坏 worker
    运行时隔离（worker 运行不 import server 域，仅此测试做一致性格栅锁）。
    """
    from app.worker.process_utils import (
        DEFAULT_VLLM_RUN_TEMPLATE as worker_tpl,
    )

    assert DEFAULT_VLLM_RUN_TEMPLATE == worker_tpl


async def test_create_with_task_embedding(session, monkeypatch):
    """task=embedding：记录.task、payload spec.task 落 embedding，vram_claim 走 512MiB 口径。"""
    await _ready_node(session)
    calls = []

    async def fake_request_to_worker(node, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return FakeResponse({"weight_bytes": 2 * _GIB})

    monkeypatch.setattr(
        "app.server.instance.service.creation.request_to_worker",
        fake_request_to_worker,
    )

    inst = await VllmInstanceService(session).create(
        VllmInstanceCreateRequest(
            model_name="bge-m3", task=InstanceTaskEnum.EMBEDDING
        )
    )

    assert inst.task == InstanceTaskEnum.EMBEDDING.value
    assert inst.vram_claim == estimate_vram_claim(
        2 * _GIB, InstanceTaskEnum.EMBEDDING.value
    )
    method, path, kwargs = calls[-1]
    assert method == "post"
    assert path == f"instances/{inst.id}/start"
    payload = kwargs["json"]
    assert payload["spec"]["task"] == "embedding"
    assert payload["vram_claim"] == inst.vram_claim


async def test_create_with_task_llm(session, monkeypatch):
    """task=llm：记录.task、payload spec.task 落 llm，vram_claim 走 2GiB 口径（与 embedding 对称）。"""
    await _ready_node(session)
    calls = []

    async def fake_request_to_worker(node, method, path, **kwargs):
        calls.append((method, path, kwargs))
        return FakeResponse({"weight_bytes": 2 * _GIB})

    monkeypatch.setattr(
        "app.server.instance.service.creation.request_to_worker",
        fake_request_to_worker,
    )

    inst = await VllmInstanceService(session).create(
        VllmInstanceCreateRequest(
            model_name="qwen2.5", task=InstanceTaskEnum.LLM
        )
    )

    assert inst.task == InstanceTaskEnum.LLM.value
    assert inst.vram_claim == estimate_vram_claim(
        2 * _GIB, InstanceTaskEnum.LLM.value
    )
    method, path, kwargs = calls[-1]
    assert method == "post"
    assert path == f"instances/{inst.id}/start"
    payload = kwargs["json"]
    assert payload["spec"]["task"] == "llm"
    assert payload["vram_claim"] == inst.vram_claim


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


async def test_concurrent_create_serialized_no_oversell(session, monkeypatch):
    """1B-3：并发 create 串行化——同一节点单卡只有一个卡位可容纳时，
    asyncio.gather 并发两个 create 不发生超卖（恰一个成功，另一个被 allocator 拒绝）。"""
    await _ready_node(session)

    async def fake_request_to_worker(node, method, path, **kwargs):
        return FakeResponse({"weight_bytes": 2 * _GIB})

    monkeypatch.setattr(
        "app.server.instance.service.creation.request_to_worker",
        fake_request_to_worker,
    )

    async def create_one():
        return await VllmInstanceService(session).create(
            VllmInstanceCreateRequest(model_name="qwen2.5")
        )

    results = await asyncio.gather(create_one(), create_one(), return_exceptions=True)

    successes = [r for r in results if isinstance(r, VllmInstance)]
    rejected = [r for r in results if isinstance(r, OperationNotAllowedException)]
    assert len(successes) == 1, results
    assert len(rejected) == 1, results
    # 串行化后无超卖：库内恰一条实例（第二个被 allocator 拒绝，未产生占账记录）
    total = await VllmInstanceService(session).count()
    assert total == 1


async def test_create_acquires_pg_advisory_lock(session, monkeypatch):
    """P0-1 回归：PG dialect 下 create 临界区开头执行事务级 advisory xact lock，
    跨请求/跨事务串行化（覆盖「锁内写、锁外提交」的 READ COMMITTED 可见性窗口）。

    sqlite 无法执行 pg_advisory_xact_lock（函数不存在），测试伪造 PG dialect 触发
    分支，并包裹 session.execute：advisory 语句只验证调用序列，其余走真实执行。
    """
    await _ready_node(session)

    async def fake_request_to_worker(node, method, path, **kwargs):
        return FakeResponse({"weight_bytes": 2 * _GIB})

    monkeypatch.setattr(
        "app.server.instance.service.creation.request_to_worker",
        fake_request_to_worker,
    )

    # 伪造 PG dialect：触发 advisory lock 分支
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    monkeypatch.setattr(session, "bind", FakeBind())

    real_execute = session.execute
    seen_sql: list[str] = []

    async def tracking_execute(stmt, *args, **kwargs):
        sql = str(stmt)
        seen_sql.append(sql)
        if "pg_advisory_xact_lock" in sql:
            # sqlite 无该函数，仅验证调用序列，跳过真实执行
            return None
        return await real_execute(stmt, *args, **kwargs)

    monkeypatch.setattr(session, "execute", tracking_execute)

    inst = await VllmInstanceService(session).create(
        VllmInstanceCreateRequest(model_name="qwen2.5")
    )
    assert inst.id
    # create 全程恰好执行一次 advisory lock（其余查询不受影响）
    assert [s for s in seen_sql if "pg_advisory_xact_lock" in s], seen_sql


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


async def test_start_stop_reset_target_retry_count(session, monkeypatch):
    """1B-1：start()/stop() 写 target_state 时重置 target_retry_count（用户操作重置预算）。"""
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.STOPPED.value
    inst.target_state = InstanceTargetStateEnum.NONE.value
    inst.target_retry_count = _MAX_STOP_REDISPATCH  # 模拟对账已把预算耗尽
    await session.flush()

    async def fake_request_to_worker(node, method, path, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    # start 重置预算
    out = await VllmInstanceService(session).start(inst.id)
    assert out.target_retry_count == 0
    assert out.target_state == InstanceTargetStateEnum.STARTING.value

    # stop 重置预算
    out.state = InstanceStateEnum.RUNNING.value
    out.target_state = InstanceTargetStateEnum.NONE.value
    out.target_retry_count = _MAX_STOP_REDISPATCH
    await session.flush()
    out = await VllmInstanceService(session).stop(out.id)
    assert out.target_retry_count == 0
    assert out.target_state == InstanceTargetStateEnum.STOPPING.value


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


async def test_delete_active_stop_failure_blocks_delete(session, monkeypatch):
    """1B-2：删除非 stopped 实例时 stop 转发失败 → 抛异常阻断物理删除，记录仍存在。"""
    await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.RUNNING.value
    await session.flush()

    async def fake_request_to_worker(node, method, path, **kwargs):
        # worker 返回 500（HttpClientException 为 ExternalServiceException 子类，
        # 与规格一致会被 except ExternalServiceException 捕获并转成阻断异常）
        raise HttpClientException(
            "worker 拒绝停止", url="http://10.0.0.1:8100", status_code=500
        )

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    with pytest.raises(ExternalServiceException) as excinfo:
        await VllmInstanceService(session).delete(inst.id)
    assert "已阻断删除" in excinfo.value.message
    assert excinfo.value.details["instance_id"] == inst.id

    # 记录未被物理删除
    got = await VllmInstanceService(session).get_by_id(inst.id)
    assert got is not None


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

    calls = []

    async def fake_request_to_worker(node, method, path, **kwargs):
        calls.append(path)
        return FakeResponse()

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    report = InstanceReportRequest(
        items=[
            InstanceReportItem(id=inst.id, state=InstanceStateEnum.RUNNING)
        ]
    )
    count = await VllmInstanceService(session).apply_report(node.id, report)
    await _drain_redispatch_tasks()  # fire-and-forget：等待后台重下发任务完成

    assert count == 1
    assert inst.state == InstanceStateEnum.RUNNING.value
    assert inst.target_state == InstanceTargetStateEnum.STOPPING.value  # 保留，不释放占账
    assert inst.target_retry_count == 1  # 记账同步可见
    assert inst.state_message == f"停止指令未生效，已重新下发停止（第 {inst.target_retry_count} 次）"
    # 悬挂收敛：stop 未生效仍上报 running → 触发一次 stop 重下发
    assert calls == [f"instances/{inst.id}/stop"]
    # 模拟下轮上报消失 → 命中 target=stopping 收敛为 stopped（清残留告警文案与预算）
    await VllmInstanceService(session).apply_report(
        node.id, InstanceReportRequest(items=[])
    )
    assert inst.state == InstanceStateEnum.STOPPED.value
    assert inst.state_message is None
    assert inst.target_retry_count == 0


async def test_apply_report_stopping_redispatch_limited(session, monkeypatch):
    """1B-1：target=stopping + 上报 running → 对账限次重下发 stop；target 保持 stopping、
    state 不收敛为 stopped、target_retry_count 递增；超限置人工介入告警且不再转发。"""
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.RUNNING.value
    inst.target_state = InstanceTargetStateEnum.STOPPING.value
    inst.target_retry_count = 0
    await session.flush()

    calls = []

    async def fake_request_to_worker(node, method, path, **kwargs):
        calls.append(path)
        return FakeResponse()

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    report = InstanceReportRequest(
        items=[InstanceReportItem(id=inst.id, state=InstanceStateEnum.RUNNING)]
    )
    # 未超限：每轮重下发一次 stop，state 不收敛为 stopped
    for round_no in range(1, _MAX_STOP_REDISPATCH + 1):
        count = await VllmInstanceService(session).apply_report(node.id, report)
        await _drain_redispatch_tasks()  # fire-and-forget：等待后台重下发任务完成
        assert count == 1
        assert inst.target_retry_count == round_no
        assert inst.state_message == (
            f"停止指令未生效，已重新下发停止（第 {round_no} 次）"
        )
        assert inst.target_state == InstanceTargetStateEnum.STOPPING.value
        assert inst.state == InstanceStateEnum.RUNNING.value
    assert len(calls) == _MAX_STOP_REDISPATCH

    # 超限：不再转发，置人工介入告警；target/state 保持（绝不自动收敛 stopped）
    await VllmInstanceService(session).apply_report(node.id, report)
    await _drain_redispatch_tasks()
    assert inst.target_retry_count == _MAX_STOP_REDISPATCH
    assert inst.target_state == InstanceTargetStateEnum.STOPPING.value
    assert inst.state == InstanceStateEnum.RUNNING.value
    assert inst.state_message == "停止指令多次未生效，需人工介入"
    assert len(calls) == _MAX_STOP_REDISPATCH


async def test_apply_report_stopping_redispatch_failure_keeps_retrying(session, monkeypatch):
    """1B-1：重下发 stop 转发失败只记 warning 置记账 message，不打断本次对账，下轮继续重试。"""
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.RUNNING.value
    inst.target_state = InstanceTargetStateEnum.STOPPING.value
    await session.flush()

    async def fake_request_to_worker(node, method, path, **kwargs):
        raise ExternalServiceException("worker 不可达", service_name="worker")

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    report = InstanceReportRequest(
        items=[InstanceReportItem(id=inst.id, state=InstanceStateEnum.RUNNING)]
    )
    count = await VllmInstanceService(session).apply_report(node.id, report)
    await _drain_redispatch_tasks()  # fire-and-forget：等待后台任务（失败仅 warning）

    assert count == 1  # 对账本身不失败
    assert inst.target_retry_count == 1  # 记账同步可见（仍计入重试次数，下轮继续）
    assert inst.target_state == InstanceTargetStateEnum.STOPPING.value
    # 转发失败不改变记账 message（失败仅 warning，留待下轮对账重试）
    assert inst.state_message == f"停止指令未生效，已重新下发停止（第 {inst.target_retry_count} 次）"

    # 下轮对账：继续计数重试（预算未因失败而消耗到人工介入）
    count = await VllmInstanceService(session).apply_report(node.id, report)
    await _drain_redispatch_tasks()
    assert count == 1
    assert inst.target_retry_count == 2
    assert inst.state_message == "停止指令未生效，已重新下发停止（第 2 次）"


async def test_apply_report_redispatch_task_failure_logged_only(session, monkeypatch, caplog):
    """P1-1：后台 stop 重下发任务失败仅 warning——apply_report 不抛、对账不受影响、
    模块级 pending 集合正常清空（异常不外泄到事件循环）。"""
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.RUNNING.value
    inst.target_state = InstanceTargetStateEnum.STOPPING.value
    await session.flush()

    async def fake_request_to_worker(node, method, path, **kwargs):
        raise ExternalServiceException("worker 不可达", service_name="worker")

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    report = InstanceReportRequest(
        items=[InstanceReportItem(id=inst.id, state=InstanceStateEnum.RUNNING)]
    )
    with caplog.at_level(
        logging.WARNING, logger="app.server.instance.service.instance"
    ):
        count = await VllmInstanceService(session).apply_report(node.id, report)
        await _drain_redispatch_tasks()

    assert count == 1
    assert inst.target_retry_count == 1
    assert any("对账重下发 stop 失败" in r.message for r in caplog.records)
    assert not _pending_redispatch_tasks  # 任务完成并已从模块级集合清空


async def test_apply_report_redispatch_skipped_when_node_missing(session, monkeypatch):
    """P2-3：实例所在节点已不存在 → 不计数、不调度（重下发预算不浪费）。"""
    await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.RUNNING.value
    inst.target_state = InstanceTargetStateEnum.STOPPING.value
    inst.node_id = "no-such-node"  # 模拟节点已被删除
    await session.flush()

    calls = []

    async def fake_request_to_worker(node, method, path, **kwargs):
        calls.append(path)
        return FakeResponse()

    monkeypatch.setattr(
        "app.server.instance.service.instance.request_to_worker",
        fake_request_to_worker,
    )

    report = InstanceReportRequest(
        items=[InstanceReportItem(id=inst.id, state=InstanceStateEnum.RUNNING)]
    )
    count = await VllmInstanceService(session).apply_report("no-such-node", report)
    await _drain_redispatch_tasks()

    assert count == 1
    assert inst.target_retry_count == 0  # 不计数
    assert inst.state_message is None  # 不写记账 message
    assert calls == []  # 不调度转发


async def test_apply_report_stopped_not_revived_by_late_running_report(session, monkeypatch):
    """M2 竞态回归（service 层核心）：stop 收敛 stopped 后，在途旧 running 上报晚到
    不得复活实例——state/target/message 全部保持。"""
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.STOPPED.value
    inst.target_state = InstanceTargetStateEnum.NONE.value
    inst.state_message = "手动停止"
    await session.flush()

    report = InstanceReportRequest(
        items=[
            InstanceReportItem(
                id=inst.id,
                state=InstanceStateEnum.RUNNING,
                state_message="ok",
                port=9001,
                restart_count=3,
            )
        ]
    )
    count = await VllmInstanceService(session).apply_report(node.id, report)

    assert count == 1
    assert inst.state == InstanceStateEnum.STOPPED.value  # 未复活
    assert inst.target_state == InstanceTargetStateEnum.NONE.value
    assert inst.state_message == "手动停止"  # message 未覆盖
    assert inst.port is None
    assert inst.restart_count == 0


async def test_apply_report_missing_with_target_stopping(session, monkeypatch):
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.target_state = InstanceTargetStateEnum.STOPPING.value
    inst.state_message = "停止指令未生效，已重新下发停止（第 2 次）"
    inst.target_retry_count = 2
    await session.flush()

    count = await VllmInstanceService(session).apply_report(
        node.id, InstanceReportRequest(items=[])
    )

    assert count == 0
    assert inst.state == InstanceStateEnum.STOPPED.value
    assert inst.target_state == InstanceTargetStateEnum.NONE.value
    # P2-1：收敛 stopped 同时清残留告警文案与重下发预算
    assert inst.state_message is None
    assert inst.target_retry_count == 0


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


async def test_reconcile_lost_nodes_batch(session, monkeypatch):
    """C2：批量失联联动——多个失联节点的 running 实例全部置 unreachable，返回处理条数。"""
    node1 = await _ready_node(session, machine_id="m-001")
    node2 = await _ready_node(session, machine_id="m-002")
    running1 = await _create_ready_instance(session, monkeypatch)
    running1.state = InstanceStateEnum.RUNNING.value
    # node2 下再内存构造一条 running（避开 allocator 单卡占满约束）
    running2 = VllmInstance(
        node_id=node2.id,
        state=InstanceStateEnum.RUNNING.value,
        target_state="none",
        model_name="qwen2.5-7b",
        gpu_indexes=[0],
        args=[],
        labels={},
        allocated_vram={},
        restart_count=0,
    )
    session.add(running2)
    await session.flush()

    count = await VllmInstanceService(session).reconcile_lost_nodes([node1, node2])

    assert count == 2
    assert running1.state == InstanceStateEnum.UNREACHABLE.value
    assert running1.state_message == "节点失联"
    assert running2.state == InstanceStateEnum.UNREACHABLE.value
    assert running2.state_message == "节点失联"


async def test_reconcile_lost_nodes_no_running(session, monkeypatch):
    """C2：批量失联联动——节点下无 running 实例时返回 0，不误伤 error/stopped 实例。"""
    node = await _ready_node(session)
    inst = await _create_ready_instance(session, monkeypatch)
    inst.state = InstanceStateEnum.ERROR.value
    await session.flush()

    count = await VllmInstanceService(session).reconcile_lost_nodes([node])

    assert count == 0
    assert inst.state == InstanceStateEnum.ERROR.value


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
