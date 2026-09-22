"""实例失联对账循环（server 进程内后台任务）。

职责：扫描节点派生状态，节点心跳超时（offline）或主动探测失败（unreachable）
时，把该节点下 running 实例联动置为 unreachable（不删除，人工介入）。

边界：不落 node.state——node.state 由 prober 独占派生/写入，本循环只读节点
内存状态做决策。故每个节点 compute_state 派生后立即 expunge 脱离会话，
避免其内存派生状态被后续 flush/commit 落库。
"""

import asyncio
import logging

from app.base.base_crud import BaseCrudService
from app.config import app_config
from app.extensions.database import get_session_context
from app.server.instance.service import VllmInstanceService
from app.server.node.enum import NodeStateEnum
from app.server.node.model import Node

logger = logging.getLogger(__name__)


async def reconcile_loop() -> None:
    """周期扫描节点派生状态，失联节点联动其 running 实例为 unreachable，异常不退出循环。"""
    while True:
        try:
            async with get_session_context() as session:
                node_crud = BaseCrudService[Node](session)
                node_crud.model = Node  # 泛型别名实例化不触发 __init_subclass__，需手动绑定 model
                nodes = await node_crud.get_list()
                for node in nodes:
                    node.compute_state(app_config.NODE_HEARTBEAT_GRACE_PERIOD)
                    lost = node.state in (
                        NodeStateEnum.OFFLINE.value,
                        NodeStateEnum.UNREACHABLE.value,
                    )
                    # 必须先 expunge：compute_state 的内存派生状态归 prober 独占写，
                    # 若不先脱离会话，后续 reconcile 内的 flush 会把 node.state 一起落库
                    # 注意：AsyncSession.expunge 是同步方法，不可 await
                    session.expunge(node)
                    if lost:
                        await VllmInstanceService(session).reconcile_node_loss(node)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("实例对账循环异常，进入下一轮")
        await asyncio.sleep(app_config.INSTANCE_RECONCILE_INTERVAL)
