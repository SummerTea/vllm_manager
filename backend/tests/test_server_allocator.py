"""Server 模块 Allocator 服务层契约测试。"""

from app.server.allocator.schema import (
    AllocationRejected,
    AllocationResult,
    GPUResource,
    WorkerResource,
)
from app.server.allocator.service import AllocatorService

_GIB = 1024**3


def _res(
    node_id: str = "node-1",
    *,
    total: int = 24 * _GIB,
    count: int = 1,
    gpu_type: str | list[str] = "A100",
    reserved: int = 0,
    allocated: dict[int, int] | None = None,
) -> WorkerResource:
    """WorkerResource 测试辅助工厂。"""
    types = [gpu_type] * count if isinstance(gpu_type, str) else gpu_type
    gpus = [
        GPUResource(index=i, memory_total=total, gpu_type=types[i])
        for i in range(count)
    ]
    return WorkerResource(
        node_id=node_id,
        gpu_devices=gpus,
        system_reserved_vram=reserved,
        allocated_vram=allocated or {},
    )


def test_single_gpu_hit():
    worker = _res()
    result = AllocatorService.first_fit(
        [worker], vram_claim=20 * _GIB, gpu_memory_utilization=0.9
    )
    assert isinstance(result, AllocationResult)
    assert result.node_id == "node-1"
    assert result.gpu_indexes == [0]
    assert result.gpu_memory_utilization == 0.9
    assert result.vram_claim == 20 * _GIB
    assert result.allocated_vram == {0: int(24 * _GIB * 0.9)}


def test_single_gpu_reject_low_available():
    worker = _res(allocated={0: 22 * _GIB})
    result = AllocatorService.first_fit(
        [worker], vram_claim=1 * _GIB, gpu_memory_utilization=0.9
    )
    assert isinstance(result, AllocationRejected)
    assert "可用率" in result.reason and "< GMU 90%" in result.reason


def test_single_gpu_reject_claim_too_big():
    worker = _res()
    result = AllocatorService.first_fit(
        [worker], vram_claim=22 * _GIB, gpu_memory_utilization=0.9
    )
    assert isinstance(result, AllocationRejected)
    assert "超单卡上限" in result.reason and "total×GMU" in result.reason


def test_reserved_deducted_per_gpu():
    worker = _res(count=2, reserved=20 * _GIB)
    result = AllocatorService.first_fit(
        [worker], vram_claim=1 * _GIB, gpu_memory_utilization=0.9
    )
    assert isinstance(result, AllocationRejected)
    assert "可用率" in result.reason


def test_multi_gpu_hit_tp2():
    worker = _res(count=2)
    result = AllocatorService.first_fit(
        [worker],
        vram_claim=40 * _GIB,
        gpu_memory_utilization=0.9,
        tensor_parallel_size=2,
    )
    assert isinstance(result, AllocationResult)
    assert result.gpu_indexes == [0, 1]
    assert result.allocated_vram == {
        0: int(24 * _GIB * 0.9),
        1: int(24 * _GIB * 0.9),
    }
    assert sum(result.allocated_vram.values()) >= 40 * _GIB


def test_multi_gpu_reject_sum_insufficient():
    worker = _res(count=2)
    result = AllocatorService.first_fit(
        [worker],
        vram_claim=50 * _GIB,
        gpu_memory_utilization=0.9,
        tensor_parallel_size=2,
    )
    assert isinstance(result, AllocationRejected)


def test_multi_gpu_reject_heterogeneous():
    worker = _res(count=2, gpu_type=["A100", "H100"])
    result = AllocatorService.first_fit(
        [worker],
        vram_claim=40 * _GIB,
        gpu_memory_utilization=0.9,
        tensor_parallel_size=2,
    )
    assert isinstance(result, AllocationRejected)


def test_all_rejected_first_reason():
    workers = [
        _res(node_id="node-1", total=10 * _GIB),
        _res(node_id="node-2", total=20 * _GIB),
    ]
    result = AllocatorService.first_fit(
        workers, vram_claim=19 * _GIB, gpu_memory_utilization=0.9
    )
    assert isinstance(result, AllocationRejected)
    assert result.reason.startswith("节点 node-1")
    assert "GiB" in result.reason


def test_default_gmu():
    worker = _res(total=10 * _GIB)
    result = AllocatorService.first_fit([worker], vram_claim=int(8.9 * _GIB))
    assert isinstance(result, AllocationResult)
    assert result.gpu_memory_utilization == 0.9
    assert result.allocated_vram == {0: int(10 * _GIB * 0.9)}


def test_boundary_gmu_equal_single_hit():
    """available/total == GMU 时单卡命中（>= 边界，与多卡的 > 不对称）。"""
    worker = _res(total=10 * _GIB, allocated={0: 1 * _GIB})
    result = AllocatorService.first_fit(
        [worker], vram_claim=8 * _GIB, gpu_memory_utilization=0.9
    )
    assert isinstance(result, AllocationResult)
    assert result.gpu_indexes == [0]


def test_boundary_gmu_equal_multi_reject():
    """available/total == GMU 时多卡拒绝（> 边界，与单卡的 >= 不对称）。"""
    worker = _res(
        count=2, total=10 * _GIB, allocated={0: 1 * _GIB, 1: 1 * _GIB}
    )
    result = AllocatorService.first_fit(
        [worker],
        vram_claim=8 * _GIB,
        gpu_memory_utilization=0.9,
        tensor_parallel_size=2,
    )
    assert isinstance(result, AllocationRejected)
    assert "无足够同型号卡" in result.reason


def test_reserved_floor_zero():
    """reserved 超过卡容量时 available 归零（max(...,0) 分支）。"""
    worker = _res(total=2 * _GIB, reserved=3 * _GIB)
    result = AllocatorService.first_fit(
        [worker], vram_claim=1 * _GIB, gpu_memory_utilization=0.9
    )
    assert isinstance(result, AllocationRejected)
    assert "可用率" in result.reason


def test_invalid_args_rejected():
    worker = _res()
    cases = [
        {"vram_claim": 10 * _GIB, "gpu_memory_utilization": 0},
        {"vram_claim": 10 * _GIB, "gpu_memory_utilization": 1.5},
        {"vram_claim": 0},
        {"vram_claim": 10 * _GIB, "tensor_parallel_size": 0},
    ]
    for kwargs in cases:
        result = AllocatorService.first_fit([worker], **kwargs)
        assert isinstance(result, AllocationRejected), kwargs
