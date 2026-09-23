"""vLLM 实例创建编排：候选节点→权重→allocator 决策→建记录→转发启动。

编排职责（请求内同步完成，无独立调度线程）：
1. 候选节点 = is_active 且重派生状态为 ready 的节点（只读 node model，不 import node service）
2. 指定 node_id 须在候选中
3. 权重：请求覆盖 > worker 广播（并发取首个成功），全部失败抛 ExternalServiceException
4. vram_claim/gmu 取值后交给 allocator 做 first-fit 决策
5. 建记录（pending + target=starting），转发失败事务回滚无残留（路由层对
   VllmManagerException 统一 rollback，不写 error 状态）

并发串行化：聚合 allocator 输入 → first_fit 决策 → 建记录并 flush 整体按事件循环
加锁（_get_create_lock），防并发 create 读到彼此未落库记录造成超卖；候选/权重/
转发等只读或网络步骤留在锁外。生产（PG）由事务级 advisory xact lock
（pg_advisory_xact_lock）保证跨请求/跨事务串行，随事务提交/回滚自动释放，覆盖
「锁内写、锁外提交」的 READ COMMITTED 可见性窗口；sqlite 测试走 asyncio.Lock。
"""

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.base.base_crud import BaseCrudService
from app.config import app_config
from app.exception import (
    ExternalServiceException,
    HttpClientException,
    OperationNotAllowedException,
)
from app.server.allocator.schema import AllocationRejected, GPUResource, WorkerResource
from app.server.allocator.service import AllocatorService
from app.server.instance.base import estimate_vram_claim
from app.server.instance.enum import InstanceStateEnum, InstanceTargetStateEnum
from app.server.instance.model import VllmInstance
from app.server.instance.schema import VllmInstanceCreateRequest
from app.server.node.enum import NodeStateEnum
from app.server.node.model import Node
from app.utils.request_to_worker import request_to_worker

# server 内置默认 docker 启动模板：**必须与 worker 侧
# app/worker/process_utils.py 的 DEFAULT_VLLM_RUN_TEMPLATE 逐字符一致**
# （防漂移注记：worker-contract §2.2；create 直落本常量快照下发，
# worker 渲染 {port}/{model_path}/{gpu_indexes} 等由 worker 侧填写；
# {net_args}/{gpus_args} 为结构性网络/GPU 差异片段，由 worker 按
# gpu_indexes 派生：GPU 节点 --network host + --gpus device=，CPU-only
# 节点 -p {port}:{port} + 空串；CPU 三参数（--enforce-eager 等）走用户 args 透传）
# A1：**镜像 ENTRYPOINT 承担 `vllm serve`**（官方 vllm-openai GPU/CPU 镜像
# ENTRYPOINT=["vllm","serve"]）——模板 `{image}` 后直接是 `{model_path} {args}`，
# 不再拼 `{vllm_bin} serve`（叠加会实执行 vllm serve vllm serve <path> → exit 2）；
# `{vllm_bin}` 键仅存量模板兼容保留（build_context 仍填充，旧格式模板渲染不 KeyError）
DEFAULT_VLLM_RUN_TEMPLATE = (
    "docker run --name {name} {net_args} --shm-size {shm_size} "
    "{gpus_args} {mount_args} {env_args} {image} {model_path} {args}"
)


class NodeReadOnlyCrud(BaseCrudService[Node]):
    """Node 只读查询（instance 域禁止 import node service，泛型子类自动绑定 model）。"""


class VllmInstanceReadOnlyCrud(BaseCrudService[VllmInstance]):
    """VllmInstance 只读查询（creation 与 VllmInstanceService 相互解耦，避免循环依赖）。"""


# 并发 create 串行化锁：按事件循环懒建（模块级 asyncio.Lock() 会绑定创建时的
# event loop，pytest 每用例新建 loop 时会报跨 loop 使用错误）。
_create_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def _get_create_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _create_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _create_locks[loop] = lock
    return lock


def _build_start_payload(inst: VllmInstance) -> dict:
    """构建 start 指令请求体（创建与手动启动复用）。"""
    return {
        "instance_type": "vllm",
        "spec": {
            "model_name": inst.model_name,
            "task": inst.task,
            "gpu_memory_utilization": inst.gpu_memory_utilization,
            "tensor_parallel_size": inst.tensor_parallel_size,
            "args": inst.args,
        },
        "gpu_indexes": inst.gpu_indexes,
        "vram_claim": inst.vram_claim,
        "template": inst.template,
    }


async def create_vllm_instance(
    session: AsyncSession, data: VllmInstanceCreateRequest
) -> VllmInstance:
    """创建 vLLM 实例的请求内编排（不提交事务，由路由层统一提交）。"""
    # 1. 候选节点：is_active 且重派生状态为 ready
    node_crud = NodeReadOnlyCrud(session)
    candidates: list[Node] = []
    for node in await node_crud.get_list(filters={"is_active": True}):
        node.compute_state(app_config.NODE_HEARTBEAT_GRACE_PERIOD)
        if node.state == NodeStateEnum.READY.value:
            candidates.append(node)

    # 2. 指定节点须在候选中
    if data.node_id is not None and not any(
        n.id == data.node_id for n in candidates
    ):
        raise OperationNotAllowedException(
            "指定节点不可用",
            operation="create",
            reason=f"节点 {data.node_id} 不在就绪候选集中",
        )

    # 候选节点仅作只读派生：node.state 归 prober 独占写，立即 expunge 脱离会话，
    # 避免后续建记录 flush 把派生状态落库（expunge 后内存字段仍可读，广播/转发不受影响）
    for node in candidates:
        session.expunge(node)

    # 3. 权重：请求覆盖 > worker 广播（并发取首个成功）
    model_weight_bytes = data.model_weight_bytes
    if model_weight_bytes is None and data.vram_claim is None:
        results = await asyncio.gather(
            *(
                request_to_worker(
                    node,
                    "post",
                    "models/weight",
                    json={"model_name": data.model_name},
                    timeout=app_config.INSTANCE_WEIGHT_TIMEOUT,
                )
                for node in candidates
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                continue
            try:
                model_weight_bytes = int(result.json()["weight_bytes"])
                break
            except (KeyError, TypeError, ValueError):
                continue
        if model_weight_bytes is None:
            raise ExternalServiceException(
                f"模型权重查询失败: {data.model_name}", service_name="worker"
            )

    # 4. 需求与 GMU 取值（vram_claim/gmu 用 is not None 判定，合法 0 交给 allocator 拒绝）
    # `or 0` 为防御性兜底：estimate 仅在 vram_claim is None 时被调用，该路径下
    # 权重必已确定非 None（请求覆盖或广播成功），显式 vram_claim 覆盖时不执行 estimate
    vram_claim = (
        data.vram_claim
        if data.vram_claim is not None
        else estimate_vram_claim(model_weight_bytes or 0, data.task.value)
    )
    gmu = (
        data.gpu_memory_utilization
        if data.gpu_memory_utilization is not None
        else app_config.INSTANCE_DEFAULT_GMU
    )

    # 5-7. 分配决策与建记录需串行化：聚合 allocator 输入 → first_fit 决策 → 建记录
    # 并 flush 整体加锁，防并发 create 读到彼此尚未落库（或未提交）的记录造成超卖。
    # 步骤 1-4（候选/权重）与步骤 8（转发 start）留在锁外，避免持锁做网络 IO。
    async with _get_create_lock():
        # PG 事务级 advisory lock：跨请求/跨事务串行化，随事务提交/回滚自动释放，
        # 覆盖「锁内写、锁外提交」导致的 READ COMMITTED 可见性窗口（Gate 1 P0-1）。
        # sqlite 测试 dialect 非 postgresql → 跳过，仍走 asyncio.Lock 兜底。
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            await session.execute(
                select(func.pg_advisory_xact_lock(func.hashtext("vllm_create_alloc")))
            )
        # 5. 聚合 allocator 输入（未停止实例占账合并）
        instance_crud = VllmInstanceReadOnlyCrud(session)
        workers: list[WorkerResource] = []
        for node in candidates:
            active = await instance_crud.get_list(
                sa_filters=[
                    VllmInstance.node_id == node.id,
                    VllmInstance.state != InstanceStateEnum.STOPPED.value,
                ]
            )
            merged: dict[int, int] = {}
            for inst in active:
                for raw_index, vram in (inst.allocated_vram or {}).items():
                    merged[int(raw_index)] = merged.get(int(raw_index), 0) + int(vram)
            workers.append(
                WorkerResource(
                    node_id=node.id,
                    accelerator=(node.status or {}).get("accelerator"),
                    gpu_devices=[
                        GPUResource(
                            index=g["index"],
                            memory_total=g["memory_total"],
                            gpu_type=g.get("name"),
                        )
                        for g in (node.status or {}).get("gpu_devices", [])
                        if g.get("memory_total")
                    ],
                    system_reserved_vram=(node.system_reserved or {}).get("vram") or 0,
                    allocated_vram=merged,
                )
            )

        # 6. allocator first-fit 决策
        result = AllocatorService.first_fit(
            workers, vram_claim, gmu, data.tensor_parallel_size
        )
        if isinstance(result, AllocationRejected):
            raise OperationNotAllowedException(
                "显存分配失败", operation="create", reason=result.reason
            )

        # 7. 建记录并 flush（template 固化内置默认模板快照；生成 id 供转发指令使用）
        inst = VllmInstance(
            node_id=result.node_id,
            state=InstanceStateEnum.PENDING.value,
            target_state=InstanceTargetStateEnum.STARTING.value,
            gpu_indexes=result.gpu_indexes,
            vram_claim=result.vram_claim,
            allocated_vram=result.allocated_vram,
            gpu_memory_utilization=result.gpu_memory_utilization,
            tensor_parallel_size=data.tensor_parallel_size,
            model_name=data.model_name,
            task=data.task.value,
            model_weight_bytes=model_weight_bytes,
            args=data.args or [],
            template=DEFAULT_VLLM_RUN_TEMPLATE,
            restart_count=0,
        )
        session.add(inst)
        await session.flush()

    # 8. 转发 start；失败包装抛 ExternalServiceException（不写 error——整事务由路由层回滚）
    target_node = next(n for n in candidates if n.id == result.node_id)
    try:
        await request_to_worker(
            target_node,
            "post",
            f"instances/{inst.id}/start",
            json=_build_start_payload(inst),
        )
    except HttpClientException:
        # 1C：worker 非 2xx 错误码透传（HttpClientException 子类先行，保留语义）
        raise
    except ExternalServiceException as e:
        raise ExternalServiceException(
            "启动指令转发失败",
            service_name="worker",
            details={"instance_id": inst.id},
        ) from e
    return inst
