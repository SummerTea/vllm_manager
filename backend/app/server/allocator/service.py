"""Server 模块 Allocator 服务层（显存分配决策，纯函数）。"""

from app.config import app_config
from app.server.allocator.schema import (
    AllocationRejected,
    AllocationResult,
    GPUResource,
    WorkerResource,
)

_GIB = 1024**3


def _fmt_gib(num: int) -> str:
    """字节数展示为可读 GiB 字符串（整数省略小数位）。"""
    gib = num / _GIB
    if gib == int(gib):
        return f"{int(gib)}GiB"
    return f"{gib:.1f}GiB"


class AllocatorService:
    """显存分配决策（纯函数，零 DB IO/零网络；数据由调用方取好传入）。"""

    @staticmethod
    def first_fit(
        candidates: list[WorkerResource],
        vram_claim: int,
        gpu_memory_utilization: float | None = None,
        tensor_parallel_size: int = 1,
    ) -> AllocationResult | AllocationRejected:
        """按候选顺序 first-fit 分配显存，首个命中即返回。"""
        gmu = (
            gpu_memory_utilization
            if gpu_memory_utilization is not None
            else app_config.INSTANCE_DEFAULT_GMU
        )
        if not 0 < gmu <= 1:
            return AllocationRejected(reason=f"显存利用率 GMU 必须位于 (0,1]，当前 {gmu}")
        if vram_claim <= 0:
            return AllocationRejected(reason=f"显存需求 vram_claim 必须大于 0，当前 {vram_claim}")
        if tensor_parallel_size < 1:
            return AllocationRejected(
                reason=f"tensor_parallel_size 必须 ≥ 1，当前 {tensor_parallel_size}"
            )

        first_failure: str | None = None
        for worker in candidates:
            result = AllocatorService._try_worker(
                worker, vram_claim, gmu, tensor_parallel_size
            )
            if isinstance(result, AllocationResult):
                return result
            if first_failure is None:
                first_failure = result.reason
        return AllocationRejected(reason=first_failure or "无可用节点")

    @staticmethod
    def _try_worker(
        worker: WorkerResource,
        vram_claim: int,
        gmu: float,
        tensor_parallel_size: int,
    ) -> AllocationResult | AllocationRejected:
        """在单个节点内尝试分配；失败返回该节点首个失败原因。"""
        if tensor_parallel_size == 1:
            return AllocatorService._try_worker_single(worker, vram_claim, gmu)
        return AllocatorService._try_worker_multi(
            worker, vram_claim, gmu, tensor_parallel_size
        )

    @staticmethod
    def _try_worker_single(
        worker: WorkerResource, vram_claim: int, gmu: float
    ) -> AllocationResult | AllocationRejected:
        """单卡（tp=1）：逐卡判定 available/total >= GMU 且 claim <= total×GMU。"""
        failure: str | None = None
        for gpu in worker.gpu_devices:
            available = AllocatorService._available(worker, gpu)
            rate_ok = available / gpu.memory_total >= gmu
            claim_ok = vram_claim <= gpu.memory_total * gmu
            if rate_ok and claim_ok:
                return AllocationResult(
                    node_id=worker.node_id,
                    gpu_indexes=[gpu.index],
                    gpu_memory_utilization=gmu,
                    vram_claim=vram_claim,
                    allocated_vram={gpu.index: int(gpu.memory_total * gmu)},
                )
            if failure is None:
                if not rate_ok:
                    pct = available / gpu.memory_total * 100
                    failure = (
                        f"节点 {worker.node_id} 卡 {gpu.index} "
                        f"可用率 {pct:.0f}% < GMU {gmu * 100:.0f}%"
                    )
                else:
                    failure = (
                        f"节点 {worker.node_id} 卡 {gpu.index} "
                        f"需求 {_fmt_gib(vram_claim)} 超单卡上限 "
                        f"{_fmt_gib(int(gpu.memory_total * gmu))}（total×GMU）"
                    )
        return AllocationRejected(reason=failure or f"节点 {worker.node_id} 无可用 GPU")

    @staticmethod
    def _try_worker_multi(
        worker: WorkerResource,
        vram_claim: int,
        gmu: float,
        tensor_parallel_size: int,
    ) -> AllocationResult | AllocationRejected:
        """多卡（tp>1）：同型号分组，组内按可用显存降序取 tp 张，Σ>=claim 命中。"""
        groups: dict[str, list[GPUResource]] = {}
        for gpu in worker.gpu_devices:
            groups.setdefault(gpu.gpu_type or "<unknown>", []).append(gpu)

        failure: str | None = None
        for gpu_type, gpus in groups.items():
            eligible: list[tuple[GPUResource, int]] = []
            for gpu in gpus:
                available = AllocatorService._available(worker, gpu)
                if available / gpu.memory_total > gmu:
                    eligible.append((gpu, available))
            if len(eligible) < tensor_parallel_size:
                if failure is None:
                    failure = (
                        f"节点 {worker.node_id} 无足够同型号卡满足 tp={tensor_parallel_size}"
                    )
                continue
            eligible.sort(key=lambda item: item[1], reverse=True)
            picked = eligible[:tensor_parallel_size]
            allocated_vram = {
                gpu.index: int(gpu.memory_total * gmu) for gpu, _ in picked
            }
            if sum(allocated_vram.values()) >= vram_claim:
                return AllocationResult(
                    node_id=worker.node_id,
                    gpu_indexes=[gpu.index for gpu, _ in picked],
                    gpu_memory_utilization=gmu,
                    vram_claim=vram_claim,
                    allocated_vram=allocated_vram,
                )
            if failure is None:
                failure = (
                    f"节点 {worker.node_id} 同型号卡（{gpu_type}）显存总和不足 "
                    f"tp={tensor_parallel_size} 需求 {_fmt_gib(vram_claim)}"
                )
        return AllocationRejected(reason=failure or f"节点 {worker.node_id} 无可用 GPU")

    @staticmethod
    def _available(worker: WorkerResource, gpu: GPUResource) -> int:
        """单卡可用显存 = total − 已分配 − 系统预留（下限 0）。"""
        return max(
            gpu.memory_total
            - worker.allocated_vram.get(gpu.index, 0)
            - worker.system_reserved_vram,
            0,
        )
