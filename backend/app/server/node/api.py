"""Server 模块 Node 公共 API（/vllm_manager/api/v1/nodes）。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.base.base_request_schema import PaginationParams
from app.base.base_response_schema import BaseResponse, EmptyResponse, PageResponse
from app.config import app_config
from app.exception import ForbiddenException, ResourceNotExistException
from app.extensions.database import get_session

# 唯一反向只读跨域例外：node 删除守卫消费 instance 域能力（assert_node_deletable）
from app.server.instance.service import VllmInstanceService
from app.server.node.dependencies import get_current_node
from app.server.node.enum import NodeStateEnum
from app.server.node.model import Node
from app.server.node.schema import (
    NodeOut,
    NodeRegisterRequest,
    NodeRegisterResponse,
    NodeStatusReportRequest,
    NodeUpdateRequest,
)
from app.server.node.service import NodeService

# 安全姿态（过渡期）：
# 管理端点（list/get/patch/delete）当前暂未接入会话鉴权，仅 worker 端点
# （heartbeat/status/register 走节点 token）。管理端点开放是过渡姿态，待 web
# 鉴权里程碑接入会话鉴权后收敛。部署约束：仅限内网/本机可达，禁止直接暴露公网。
node_router = APIRouter(prefix="/nodes", tags=["节点管理"])


@node_router.post("/register", response_model=BaseResponse[NodeRegisterResponse])
async def register_node(
    data: NodeRegisterRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BaseResponse[NodeRegisterResponse]:
    """节点注册（公开，幂等）。

    并发重复注册触发 uix_node_machine_id 唯一索引时，回滚后按原幂等键重查返回已存在节点。
    """
    service = NodeService(session)
    try:
        node = await service.register(data)
    except IntegrityError:
        # 当前唯一约束即 uix_node_machine_id，此冲突等价于并发重复注册；将来新增唯一约束需重新审视此分支
        await session.rollback()
        if data.machine_id:
            found = await service.get_by_field("machine_id", data.machine_id)
        else:
            # 无 machine_id 时按 hostname 回退，并发双插无法被唯一索引拦截（幂等并发保证仅在携带 machine_id 时成立）
            found = await service.get_by_field("hostname", data.hostname)
        node = found if isinstance(found, Node) else None
        if node is None:
            raise ResourceNotExistException(
                "节点注册冲突且重查失败", resource_type="node"
            ) from None

    return BaseResponse.success(
        NodeRegisterResponse(
            node_id=node.id,
            token=node.token,
            worker_port=node.worker_port,
            heartbeat_interval=app_config.NODE_HEARTBEAT_INTERVAL,
        )
    )


@node_router.post("/{node_id}/heartbeat", response_model=BaseResponse[None])
async def node_heartbeat(
    node_id: str,
    current_node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BaseResponse[None]:
    """节点心跳（worker 鉴权）。"""
    if current_node.id != node_id:
        raise ForbiddenException("节点令牌与目标节点不匹配")
    node = await NodeService(session).heartbeat(node_id)
    if node is None:
        raise ResourceNotExistException(
            "节点不存在", resource_type="node", resource_id=node_id
        )
    return BaseResponse.success()


@node_router.post("/{node_id}/status", response_model=BaseResponse[None])
async def node_status_report(
    node_id: str,
    report: NodeStatusReportRequest,
    current_node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BaseResponse[None]:
    """节点状态上报（worker 鉴权）。"""
    if current_node.id != node_id:
        raise ForbiddenException("节点令牌与目标节点不匹配")
    node = await NodeService(session).update_status(node_id, report)
    if node is None:
        raise ResourceNotExistException(
            "节点不存在", resource_type="node", resource_id=node_id
        )
    return BaseResponse.success()


@node_router.get("", response_model=PageResponse[NodeOut])
async def list_nodes(
    params: Annotated[PaginationParams, Depends()],
    session: Annotated[AsyncSession, Depends(get_session)],
    state: Annotated[NodeStateEnum | None, Query(description="按状态过滤")] = None,
    is_active: bool | None = None,
) -> PageResponse[NodeOut]:
    """节点分页列表（支持 state/is_active 过滤，非法 state 值由 FastAPI 直接 422）。"""
    filters: dict[str, Any] = {}
    if state is not None:
        filters["state"] = state.value
    if is_active is not None:
        filters["is_active"] = is_active

    result = await NodeService(session).paginate(
        page=params.page,
        page_size=params.page_size,
        filters=filters,
        order_by=["-created_at"],
    )
    items = [NodeOut.model_validate(node) for node in result["items"]]
    return PageResponse.success(
        items=items,
        page=params.page,
        page_size=params.page_size,
        total=result["total"],
    )


@node_router.get("/{node_id}", response_model=BaseResponse[NodeOut])
async def get_node(
    node_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BaseResponse[NodeOut]:
    """节点详情。"""
    node = await NodeService(session).get_by_id(node_id)
    if node is None:
        raise ResourceNotExistException(
            "节点不存在", resource_type="node", resource_id=node_id
        )
    return BaseResponse.success(NodeOut.model_validate(node))


@node_router.patch("/{node_id}", response_model=BaseResponse[NodeOut])
async def update_node(
    node_id: str,
    data: NodeUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BaseResponse[NodeOut]:
    """更新节点（labels/is_active）。"""
    node = await NodeService(session).update(
        node_id, data.model_dump(exclude_none=True)
    )
    if node is None:
        raise ResourceNotExistException(
            "节点不存在", resource_type="node", resource_id=node_id
        )
    return BaseResponse.success(NodeOut.model_validate(node))


@node_router.delete("/{node_id}", response_model=EmptyResponse)
async def delete_node(
    node_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EmptyResponse:
    """删除节点（物理删除）。

    节点存在活跃实例时必须 409 拒绝删除（跨域守卫：VllmInstanceService.assert_node_deletable）。
    """
    await VllmInstanceService(session).assert_node_deletable(node_id)
    deleted = await NodeService(session).delete(node_id)
    if not deleted:
        raise ResourceNotExistException(
            "节点不存在", resource_type="node", resource_id=node_id
        )
    return EmptyResponse.success()
