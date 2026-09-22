"""Manager 模块 Node 公共 API（/vllm_manager/api/v1/nodes）。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.base.base_request_schema import PaginationParams
from app.base.base_response_schema import BaseResponse, EmptyResponse, PageResponse
from app.config import app_config
from app.exception import ForbiddenException, ResourceNotExistException
from app.extensions.database import get_session
from app.manager.dependencies import get_current_node
from app.manager.model import Node
from app.manager.schema import (
    NodeOut,
    NodeRegisterRequest,
    NodeRegisterResponse,
    NodeStatusReportRequest,
    NodeUpdateRequest,
)
from app.manager.service import NodeService

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
        await session.rollback()
        if data.machine_id:
            found = await service.get_by_field("machine_id", data.machine_id)
        else:
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
            agent_port=node.agent_port,
            heartbeat_interval=app_config.NODE_HEARTBEAT_INTERVAL,
        )
    )


@node_router.post("/{node_id}/heartbeat", response_model=BaseResponse[None])
async def node_heartbeat(
    node_id: str,
    current_node: Annotated[Node, Depends(get_current_node)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> BaseResponse[None]:
    """节点心跳（agent 鉴权）。"""
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
    """节点状态上报（agent 鉴权）。"""
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
    state: str | None = None,
    is_active: bool | None = None,
) -> PageResponse[NodeOut]:
    """节点分页列表（支持 state/is_active 过滤）。"""
    filters: dict[str, Any] = {}
    if state is not None:
        filters["state"] = state
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
    """删除节点（物理删除）。"""
    deleted = await NodeService(session).delete(node_id)
    if not deleted:
        raise ResourceNotExistException(
            "节点不存在", resource_type="node", resource_id=node_id
        )
    return EmptyResponse.success()
