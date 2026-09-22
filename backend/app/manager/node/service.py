"""Manager 模块 Node 服务层。"""

import secrets
from datetime import datetime

from app.base.base_crud import BaseCrudService
from app.config import app_config
from app.manager.node.enum import NodeStateEnum
from app.manager.node.model import Node
from app.manager.node.schema import NodeRegisterRequest, NodeStatusReportRequest


class NodeService(BaseCrudService[Node]):
    """Node 服务（默认不提交事务，由 API 路由层统一提交）。"""

    async def register(self, data: NodeRegisterRequest) -> Node:
        """节点注册（幂等）。

        - 幂等键：machine_id 非空按 machine_id 匹配，否则按 hostname 匹配。
        - 已存在：复用旧 token（不轮换），刷新可变字段，不写 heartbeat_time（liveness 归心跳端点）。
        - 不存在：新建 pending 节点并下发新 token。
        """
        if data.machine_id:
            found = await self.get_by_field("machine_id", data.machine_id)
        else:
            found = await self.get_by_field("hostname", data.hostname)
        node = found if isinstance(found, Node) else None

        if node is not None:
            node.hostname = data.hostname
            node.ip = data.ip
            node.advertise_address = data.advertise_address
            node.agent_port = data.agent_port
        else:
            node = Node(
                machine_id=data.machine_id,
                hostname=data.hostname,
                ip=data.ip,
                advertise_address=data.advertise_address,
                agent_port=data.agent_port,
                token=secrets.token_hex(32),
                state=NodeStateEnum.PENDING.value,
            )
            self.session.add(node)

        await self.session.flush()
        return node

    async def heartbeat(self, node_id: str) -> Node | None:
        """节点心跳：刷新 heartbeat_time 并派生状态。"""
        node = await self.get_by_id(node_id)
        if node is None:
            return None
        node.heartbeat_time = datetime.now()
        node.compute_state(app_config.NODE_HEARTBEAT_GRACE_PERIOD)
        await self.session.flush()
        return node

    async def update_status(
        self, node_id: str, report: NodeStatusReportRequest
    ) -> Node | None:
        """节点状态上报：落 system_reserved/status 并刷新心跳与状态。"""
        node = await self.get_by_id(node_id)
        if node is None:
            return None
        if report.system_reserved is not None:
            node.system_reserved = report.system_reserved.model_dump()
        if report.status is not None:
            node.status = report.status.model_dump()
        node.heartbeat_time = datetime.now()
        node.compute_state(app_config.NODE_HEARTBEAT_GRACE_PERIOD)
        await self.session.flush()
        return node

    async def refresh_state(self, node: Node) -> None:
        """重算节点状态（供 prober 使用），只 flush 不提交。"""
        node.compute_state(app_config.NODE_HEARTBEAT_GRACE_PERIOD)
        await self.session.flush()
