"""Server 模块 vLLM 实例服务层。"""

import logging

from app.base.base_crud import BaseCrudService
from app.exception import (
    ConflictException,
    ExternalServiceException,
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
        """删除实例：非 stopped 先尽力转发 stop（失败仅记日志不阻断），随后物理删除。"""
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
                except ExternalServiceException:
                    logger.warning("删除实例时停止指令转发失败，忽略: %s", instance_id)
        await super().delete(instance_id)

    async def apply_report(
        self, node_id: str, report: InstanceReportRequest
    ) -> int:
        """对账：合并 worker 上报到本节点实例，并处理上报消失的实例。

        - 上报存在：state 取上报值（stopped 终态防在途旧上报复活，M2）；target 仅
          达成时清回 none（防在途上报清掉 start/stop 目标，B1 竞态）；
          state_message/port/restart 非 None 才更新
        - 上报消失：target=stopping 的实例收敛为 stopped；其余不动（容忍 worker 重启窗口）
        - 返回处理条数；未知实例 id 忽略（记 debug 日志）
        """
        instances = await self.get_list(filters={"node_id": node_id})
        by_id = {inst.id: inst for inst in instances}
        count = 0
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
            count += 1
        await self.session.flush()

        reported_ids = {item.id for item in report.items}
        for inst in instances:
            if inst.id in reported_ids:
                continue
            if inst.target_state == InstanceTargetStateEnum.STOPPING.value:
                inst.state = InstanceStateEnum.STOPPED.value
                inst.target_state = InstanceTargetStateEnum.NONE.value
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
