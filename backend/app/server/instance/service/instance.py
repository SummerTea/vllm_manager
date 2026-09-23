"""Server 模块 vLLM 实例服务层。"""

import asyncio
import logging
from types import SimpleNamespace

from app.base.base_crud import BaseCrudService
from app.exception import (
    ConflictException,
    ExternalServiceException,
    HttpClientException,
    InvalidStateException,
)
from app.server.instance.base import apply_report as apply_report_pure
from app.server.instance.enum import InstanceStateEnum, InstanceTargetStateEnum
from app.server.instance.model import VllmInstance
from app.server.instance.schema import InstanceReportRequest, VllmInstanceCreateRequest
from app.server.instance.service.creation import (
    NodeReadOnlyCrud,
    _build_start_payload,
    create_vllm_instance,
)
from app.server.node.model import Node
from app.utils.request_to_worker import request_to_worker

logger = logging.getLogger(__name__)

# stop 悬挂收敛：worker 停止未生效仍上报 running 时，对账最多重下发 stop 的次数
# （超限置人工介入告警，绝不自动收敛 stopped——防误判丢失占账/容器清理责任）。
_MAX_STOP_REDISPATCH = 3

# fire-and-forget 后台重下发超时（短超时，避免后台任务长时间占用事件循环）。
_REDISPATCH_TIMEOUT = 5

# 后台 stop 重下发任务集合：模块级持有引用防 GC 提前回收，done 回调里 discard。
# 任务协程只依赖调度时固化的字段快照，不触碰任何 ORM/session（调度后 session
# 可能已随端点返回关闭，见 apply_report 内快照注释）。
_pending_redispatch_tasks: set[asyncio.Task] = set()


def _schedule_stop_redispatch(node_snapshot, instance_id: str) -> None:
    """派发 stop 重下发后台任务（fire-and-forget，失败仅 warning 不外泄）。"""
    task = asyncio.create_task(_redispatch_stop_worker(node_snapshot, instance_id))
    _pending_redispatch_tasks.add(task)
    task.add_done_callback(_pending_redispatch_tasks.discard)


async def _redispatch_stop_worker(node_snapshot, instance_id: str) -> None:
    """后台转发 stop 指令：只依赖调用时固化的 node 字段快照，不触碰 ORM/session。"""
    try:
        await request_to_worker(
            node_snapshot,
            "post",
            f"instances/{instance_id}/stop",
            json={"instance_type": "vllm"},
            timeout=_REDISPATCH_TIMEOUT,
        )
    except ExternalServiceException as e:
        logger.warning("对账重下发 stop 失败（后台任务）: %s %s", instance_id, e)


class VllmInstanceService(BaseCrudService[VllmInstance]):
    """vLLM 实例服务（默认不提交事务，由 API 路由层统一提交）。

    覆盖实例生命周期：创建编排（creation.py）、启停指令转发、worker 上报对账、
    失联联动与节点删除守卫。
    """

    async def create(self, data: VllmInstanceCreateRequest) -> VllmInstance:
        """创建实例（编排在 creation.py，本方法为导入面入口）。"""
        return await create_vllm_instance(self.session, data)

    async def start(self, instance_id: str) -> VllmInstance:
        """启动实例：仅 stopped/error 可启动，置 target=starting 并转发 start。"""
        inst = await self.get_or_raise(instance_id)
        if inst.state not in (
            InstanceStateEnum.STOPPED.value,
            InstanceStateEnum.ERROR.value,
        ):
            raise InvalidStateException(
                "实例当前状态不允许启动",
                current_state=inst.state,
                expected_state="stopped/error",
            )
        inst.target_state = InstanceTargetStateEnum.STARTING.value
        inst.target_retry_count = 0  # 每次用户操作重置对账重下发预算
        await self.session.flush()

        node_crud = NodeReadOnlyCrud(self.session)
        node = await node_crud.get_by_id(inst.node_id)
        try:
            if node is None:
                raise ExternalServiceException(
                    "实例所在节点不存在",
                    service_name="worker",
                    details={"node_id": inst.node_id},
                )
            await request_to_worker(
                node,
                "post",
                f"instances/{instance_id}/start",
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

    async def stop(self, instance_id: str) -> VllmInstance:
        """停止实例：running/starting/unreachable 可停止，置 target=stopping 并转发。

        转发失败抛 ExternalServiceException——target=stopping 的写入随事务回滚
        （路由层对 VllmManagerException 统一 rollback），不落库，用户可重试。
        """
        inst = await self.get_or_raise(instance_id)
        if inst.state not in (
            InstanceStateEnum.RUNNING.value,
            InstanceStateEnum.STARTING.value,
            InstanceStateEnum.UNREACHABLE.value,
        ):
            raise InvalidStateException(
                "实例当前状态不允许停止",
                current_state=inst.state,
                expected_state="running/starting/unreachable",
            )
        inst.target_state = InstanceTargetStateEnum.STOPPING.value
        inst.target_retry_count = 0  # 每次用户操作重置对账重下发预算
        await self.session.flush()

        node_crud = NodeReadOnlyCrud(self.session)
        node = await node_crud.get_by_id(inst.node_id)
        if node is None:
            raise ExternalServiceException(
                "实例所在节点不存在",
                service_name="worker",
                details={"node_id": inst.node_id},
            )
        await request_to_worker(
            node,
            "post",
            f"instances/{instance_id}/stop",
            json={"instance_type": "vllm"},
        )
        return inst

    async def delete(self, instance_id: str) -> None:
        """删除实例：非 stopped 先转发 stop（失败抛异常阻断删除），随后物理删除。"""
        inst = await self.get_or_raise(instance_id)
        if inst.state != InstanceStateEnum.STOPPED.value:
            node_crud = NodeReadOnlyCrud(self.session)
            node = await node_crud.get_by_id(inst.node_id)
            if node is not None:
                try:
                    await request_to_worker(
                        node,
                        "post",
                        f"instances/{instance_id}/stop",
                        json={"instance_type": "vllm"},
                    )
                except ExternalServiceException as e:
                    raise ExternalServiceException(
                        "删除实例前停止失败，已阻断删除",
                        service_name="worker",
                        details={"instance_id": instance_id},
                    ) from e
            else:
                # node 删除守卫（assert_node_deletable）已阻止活跃实例所在节点被删，
                # 正常路径不会走到 node is None；维持原行为（记 warning 后继续删除）。
                logger.warning("删除实例时所在节点不存在，跳过停止转发: %s", instance_id)
        await super().delete(instance_id)

    async def apply_report(
        self, node_id: str, report: InstanceReportRequest
    ) -> int:
        """对账：合并 worker 上报到本节点实例，并处理上报消失的实例。

        - 上报存在：state 取上报值（stopped 终态防在途旧上报复活，M2）；target 仅
          达成时清回 none（防在途上报清掉 start/stop 目标，B1 竞态）；
          state_message/port/restart 非 None 才更新
        - 上报消失：target=stopping 的实例收敛为 stopped（同时清 state_message/
          target_retry_count，防已停实例残留告警文案，P2-1）；其余不动（容忍 worker
          重启窗口）
        - stop 悬挂收敛：target=stopping 但上报仍非 stopped 时，本方法只做记账
          （限次递增 target_retry_count/写 state_message，_MAX_STOP_REDISPATCH 超限
          置人工介入告警）；实际 stop 重下发由 fire-and-forget 后台任务执行，不阻塞
          /report 端点、DB 事务内不产生网络 IO——转发失败仅 warning 留待下轮对账
        - 返回处理条数；未知实例 id 忽略（记 debug 日志）
        """
        instances = await self.get_list(filters={"node_id": node_id})
        by_id = {inst.id: inst for inst in instances}
        count = 0
        redispatch: list[VllmInstance] = []
        for item in report.items:
            inst = by_id.get(item.id)
            if inst is None:
                logger.debug("对账忽略未知实例: %s", item.id)
                continue
            if inst.node_id != node_id:
                continue
            (
                inst.state,
                inst.target_state,
                inst.state_message,
                inst.port,
                inst.restart_count,
            ) = apply_report_pure(
                inst.state,
                inst.target_state,
                inst.state_message,
                inst.port,
                inst.restart_count,
                item.state.value,
                item.state_message,
                item.port,
                item.restart_count,
            )
            if (
                inst.target_state == InstanceTargetStateEnum.STOPPING.value
                and item.state.value != InstanceStateEnum.STOPPED.value
            ):
                # stop 在途但上报仍未停止：归入重下发记账队列（悬挂收敛）
                redispatch.append(inst)
            count += 1
        await self.session.flush()

        reported_ids = {item.id for item in report.items}
        for inst in instances:
            if inst.id in reported_ids:
                continue
            if inst.target_state == InstanceTargetStateEnum.STOPPING.value:
                inst.state = InstanceStateEnum.STOPPED.value
                inst.target_state = InstanceTargetStateEnum.NONE.value
                # P2-1：收敛 stopped 同时清残留告警文案与重下发预算
                inst.state_message = None
                inst.target_retry_count = 0
        await self.session.flush()

        # stop 悬挂收敛：只做记账，实际转发交给 fire-and-forget 后台任务——/report
        # 对账不阻塞、DB 事务内不产生网络 IO。任何转发失败仅记 warning 并留待下轮
        # 对账重试（target=stopping 保持）。
        for inst in redispatch:
            if inst.target_retry_count >= _MAX_STOP_REDISPATCH:
                inst.state_message = "停止指令多次未生效，需人工介入"
                continue
            node = await NodeReadOnlyCrud(self.session).get_by_id(inst.node_id)
            if node is None:
                # P2-3：节点已不存在 → 不计数、不调度（预算不浪费，等待用户介入）
                continue
            inst.target_retry_count += 1
            inst.state_message = (
                f"停止指令未生效，已重新下发停止（第 {inst.target_retry_count} 次）"
            )
            # 调度前把所需字段固化为普通值快照：后台任务在端点返回后才执行，此时
            # session 可能已关，协程内只依赖这些快照，不触碰任何 ORM/session
            _schedule_stop_redispatch(
                SimpleNamespace(
                    advertise_address=node.advertise_address,
                    ip=node.ip,
                    worker_port=node.worker_port,
                    token=node.token,
                ),
                inst.id,
            )
        await self.session.flush()
        return count

    async def assert_node_deletable(self, node_id: str) -> None:
        """节点删除守卫：存在活跃（非 stopped）实例时抛 ConflictException(409)。"""
        count = await self.count(
            sa_filters=[
                VllmInstance.node_id == node_id,
                VllmInstance.state != InstanceStateEnum.STOPPED.value,
            ]
        )
        if count > 0:
            raise ConflictException(f"节点 {node_id} 存在活跃实例，拒绝删除")

    async def reconcile_node_loss(self, node: Node) -> int:
        """节点失联联动：该节点下 running 实例全部置 unreachable（不删除）。"""
        instances = await self.get_list(
            filters={"node_id": node.id, "state": InstanceStateEnum.RUNNING.value}
        )
        for inst in instances:
            inst.state = InstanceStateEnum.UNREACHABLE.value
            inst.state_message = "节点失联"
        await self.session.flush()
        return len(instances)
