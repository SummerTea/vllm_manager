"""Node 主动 /healthz 探测循环（server 进程内后台任务）。

契约记录：
- /healthz 探测端点鉴权策略：按惯例豁免（liveness 探针暴露给 LB/监控，与 gpustack 一致），
  探测请求仍携带 node token 作兼容双保险。
- 部署约束：单进程（不支持 `uvicorn --workers > 1` 并发探测去重）；节点 >5 时顺序探测
  最坏耗时 N×3s 可能超周期，二期改 gather+semaphore。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import app_config
from app.extensions.database import get_session_context
from app.server.node.enum import NodeStateEnum
from app.server.node.model import Node
from app.server.node.service import NodeService

logger = logging.getLogger(__name__)


async def _probe_node(
    service: NodeService, client: httpx.AsyncClient, node: Node
) -> None:
    """单节点探测：先按 status 上报派生状态，再决定是否探测 /healthz。"""
    # 第一次 compute_state 决定是否探测（status 上报权威信号先行）
    node.compute_state(app_config.NODE_HEARTBEAT_GRACE_PERIOD)
    if node.state in (NodeStateEnum.PENDING.value, NodeStateEnum.OFFLINE.value):
        await service.refresh_state(node)  # 心跳未到/已超时，不探测（healthz 无法区分死因）
        return
    if node.advertise_address:
        host = node.advertise_address
        addr = host if ":" in host else f"{host}:{node.worker_port}"
    else:
        addr = f"{node.ip}:{node.worker_port}"
    url = f"http://{addr}/healthz"
    headers = {"Authorization": f"Bearer {node.token}"}
    try:
        resp = await client.get(url, headers=headers)
        node.unreachable = resp.status_code != 200
    except httpx.HTTPError:
        # 仅网络/超时归 unreachable；InvalidURL 等配置错误浮出表面而非伪装成网络故障
        node.unreachable = True
    # 第二次 compute_state 必需：探测结果改变 unreachable 后需重新派生最终状态，
    # 不能与第一次合并（探测前无法预知 unreachable 的新值）
    node.compute_state(app_config.NODE_HEARTBEAT_GRACE_PERIOD)
    await service.refresh_state(node)


async def probe_loop(
    on_node_lost: Callable[[AsyncSession, list[Node]], Awaitable] | None = None,
) -> None:
    """周期扫描所有节点（含 is_active=False，供管理员参考存活），异常不退出循环。

    可选回调 on_node_lost：每轮探测后把派生为 OFFLINE/UNREACHABLE 的节点批量交给
    组合根注入的联动处理（如实例域失联联动）。跨域装配只发生在 lifespan 组合根
    （node 域不 import instance 域，回调是参数注入），缺省 None 时仅探测不发联动。
    """
    while True:
        try:
            async with get_session_context() as session:
                service = NodeService(session)
                nodes = await service.get_list()
                async with httpx.AsyncClient(
                    timeout=app_config.NODE_PROBE_TIMEOUT
                ) as client:
                    for node in nodes:
                        await _probe_node(service, client, node)
                if on_node_lost is not None:
                    lost_nodes = [
                        node
                        for node in nodes
                        if node.state
                        in (NodeStateEnum.OFFLINE.value, NodeStateEnum.UNREACHABLE.value)
                    ]
                    if lost_nodes:
                        await on_node_lost(session, lost_nodes)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("探测循环异常，进入下一轮")
        await asyncio.sleep(app_config.NODE_PROBE_INTERVAL)
